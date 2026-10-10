import os
import io
import json
import hmac
import base64
import re
import unicodedata
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse, parse_qs, quote
from collections import Counter
from functools import wraps
import pandas as pd
from dotenv import load_dotenv
from flask import Flask, render_template_string, request, redirect, url_for, send_file, session
from markupsafe import Markup
import registro_bots

load_dotenv()

app = Flask(__name__)

FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD")
if not FLASK_SECRET_KEY or not DASHBOARD_PASSWORD:
    raise RuntimeError(
        "Faltan variables de entorno obligatorias: FLASK_SECRET_KEY y/o DASHBOARD_PASSWORD."
    )
app.secret_key = FLASK_SECRET_KEY

# Las claves de Product Owner y Administrador son opcionales: si no están configuradas
# ese usuario no puede entrar, en vez de tumbar la app.
USUARIOS = {
    'mercadeo': {'nombre': 'Mercadeo', 'password': DASHBOARD_PASSWORD},
    'product_owner': {'nombre': 'Product Owner', 'password': os.environ.get("DASHBOARD_PASSWORD_PO")},
    'administrador': {'nombre': 'Administrador', 'password': os.environ.get("DASHBOARD_PASSWORD_ADMIN")},
    'funciones_beta': {'nombre': 'Funciones Beta', 'password': os.environ.get("DASHBOARD_PASSWORD_BETA")},
}
USUARIO_ADMIN = 'administrador'
# Las funciones nuevas se muestran primero solo a este usuario (con {% if es_beta %} en las
# plantillas); al liberarlas para todos se quita esa condición.
USUARIO_BETA = 'funciones_beta'

# Funciones ya liberadas para los demás usuarios: clave -> fecha de liberación (AAAA-MM-DD, hora de
# Venezuela). En la plantilla se marcan con {{ nuevo('clave') }}, que muestra una etiqueta "NEW"
# durante DIAS_ETIQUETA_NUEVO días. Funciones Beta no la ve: allí ya tiene la etiqueta BETA.
DIAS_ETIQUETA_NUEVO = 10
FUNCIONES_NUEVAS = {
    'cambiar_usuario': '2026-10-10',
}

def hoy_venezuela():
    return (datetime.now(timezone.utc) - timedelta(hours=4)).date()

def es_funcion_nueva(clave):
    try:
        liberada = datetime.strptime(FUNCIONES_NUEVAS[clave], '%Y-%m-%d').date()
    except (KeyError, ValueError):
        return False
    return 0 <= (hoy_venezuela() - liberada).days < DIAS_ETIQUETA_NUEVO

DATABASE_URL = os.environ.get("DATABASE_URL")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")
WORKFLOW_FILE = "scraper.yml"
WORKFLOW_GOOGLE_FILE = "scraper_google.yml"
URLS_FILE_PATH = "urls.txt"
URLS_GOOGLE_FILE_PATH = "urls_google.txt"
PRODUCTOS_FILE_PATH = "productos.txt"
SIN_CLASIFICAR = "Sin clasificar"

DIAS_WINNING_AD = 30

MESES_DICT = {
    'ene': '01', 'feb': '02', 'mar': '03', 'abr': '04', 'may': '05', 'jun': '06',
    'jul': '07', 'ago': '08', 'sep': '09', 'oct': '10', 'nov': '11', 'dic': '12',
    'enero': '01', 'febrero': '02', 'marzo': '03', 'abril': '04', 'mayo': '05', 'junio': '06',
    'julio': '07', 'agosto': '08', 'septiembre': '09', 'octubre': '10', 'noviembre': '11', 'diciembre': '12',
    'jan': '01', 'apr': '04', 'aug': '08', 'dec': '12'
}

STOPWORDS_ES = {
    'de', 'la', 'que', 'el', 'en', 'y', 'a', 'los', 'del', 'se', 'las', 'por', 'un', 'para', 'con', 
    'no', 'una', 'su', 'al', 'lo', 'como', 'más', 'pero', 'sus', 'le', 'ya', 'o', 'este', 'sí', 
    'porque', 'esta', 'son', 'entre', 'está', 'cuando', 'muy', 'sin', 'sobre', 'ser', 'tiene', 
    'también', 'me', 'hasta', 'hay', 'donde', 'quien', 'desde', 'todo', 'nos', 'durante', 'todos', 
    'uno', 'les', 'ni', 'contra', 'otros', 'ese', 'eso', 'ante', 'ellos', 'e', 'esto', 'mí', 'antes', 
    'algunos', 'qué', 'unos', 'yo', 'otro', 'otras', 'otra', 'él', 'tanto', 'esa', 'estos', 'mucho', 
    'quienes', 'nada', 'muchos', 'cual', 'sea', 'poco', 'ella', 'estar', 'estas', 'estás', 'algunas', 'algo', 
    'nosotros', 'mi', 'mis', 'tu', 'tus', 'te', 'ti', 'aquí', 'solo', 'cada', 'ahora', 'mas', 'si',
    'http', 'https', 'com', 'www', 'meta', 'ads', 'click', 'link',
    'fina', 'orden', 'venezuela', 'andrea'
}

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def usuario_actual():
    # Las sesiones abiertas antes de existir los usuarios eran de la clave de Mercadeo.
    return session.get('usuario', 'mercadeo')

# Usuario con el que se inició sesión con contraseña. Se conserva al cambiar de usuario
# para que el Administrador o Funciones Beta puedan volver sin cerrar sesión.
def usuario_origen():
    return session.get('usuario_origen', usuario_actual())

def puede_cambiar_usuario():
    return usuario_origen() in (USUARIO_ADMIN, USUARIO_BETA)

# Solo pasar a Administrador desde una sesión que no empezó como Administrador pide su
# contraseña; los demás cambios no dan más permisos de los que ya tiene la sesión.
def cambio_requiere_password(destino):
    return destino == USUARIO_ADMIN and usuario_origen() != USUARIO_ADMIN

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login', next=request.url))
        if usuario_actual() != USUARIO_ADMIN:
            return redirect(url_for('index', msg="⛔ Solo el Administrador puede ver esa sección."))
        return f(*args, **kwargs)
    return decorated_function

USUARIOS_EDITAN_URLS = (USUARIO_ADMIN, USUARIO_BETA)

def editor_urls_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login', next=request.url))
        if usuario_actual() not in USUARIOS_EDITAN_URLS:
            return redirect(url_for('index', msg="⛔ Solo el Administrador y Funciones Beta pueden modificar la configuración."))
        return f(*args, **kwargs)
    return decorated_function

# Panel "Salud de los Bots": por ahora solo Funciones Beta. Al liberarlo se agrega USUARIO_ADMIN
# (y la entrada en FUNCIONES_NUEVAS para que el Administrador vea la etiqueta NEW).
USUARIOS_SALUD = (USUARIO_BETA,)

def salud_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login', next=request.url))
        if usuario_actual() not in USUARIOS_SALUD:
            return redirect(url_for('index', msg="⛔ Esa sección todavía no está disponible para tu usuario."))
        return f(*args, **kwargs)
    return decorated_function

ESTADOS_CORRIDA = {
    'ok': ('Correcta', 'success'),
    'con_errores': ('Con errores', 'warning'),
    'vacia': ('Sin anuncios', 'warning'),
    'fallo': ('Falló', 'danger'),
    'atrasada': ('Atrasada', 'warning'),
    'sin_datos': ('Sin registros', 'secondary'),
}
DIAS_CORRIDA_ATRASADA = 3  # los bots corren cada 2 días
WORKFLOWS_BOTS = {'Meta': '.github/workflows/scraper.yml', 'Google': '.github/workflows/scraper_google.yml'}

def proxima_ejecucion(ruta_workflow, ahora=None):
    # Lee el cron del workflow ("M H */N * *" o "M H * * *") y calcula la próxima ejecución en UTC.
    # GitHub puede atrasar las ejecuciones programadas; es la hora prevista, no garantizada.
    try:
        with open(ruta_workflow, encoding='utf-8') as f:
            m = re.search(r"cron:\s*['\"](\d+)\s+(\d+)\s+(\*|\*/\d+)\s+\*\s+\*['\"]", f.read())
    except OSError:
        return None
    if not m:
        return None
    minuto, hora = int(m.group(1)), int(m.group(2))
    cada = int(m.group(3)[2:]) if m.group(3).startswith('*/') else 1
    ahora = ahora or datetime.now(timezone.utc)
    dia = ahora.date()
    for _ in range(64):
        candidato = datetime(dia.year, dia.month, dia.day, hora, minuto, tzinfo=timezone.utc)
        # */N en el día del mes = días 1, 1+N, 1+2N... (se reinicia cada mes)
        if candidato > ahora and (dia.day - 1) % cada == 0:
            return candidato
        dia += timedelta(days=1)
    return None

DIAS_SEMANA = ['lun', 'mar', 'mié', 'jue', 'vie', 'sáb', 'dom']

def proximas_busquedas():
    proximas = []
    for fuente, ruta in WORKFLOWS_BOTS.items():
        fecha = proxima_ejecucion(ruta)
        if fecha:
            local = fecha - timedelta(hours=4)
            proximas.append({'fuente': fuente, 'iso': fecha.isoformat(),
                             'local': f"{DIAS_SEMANA[local.weekday()]} {local.strftime('%d/%m')} a las {local.strftime('%I:%M %p').lower().lstrip('0')}"})
    return proximas

def ultimas_corridas(cur):
    # Última corrida de cada bot y si hay algo que revisar (falló, vino vacía o no corre hace días).
    cur.execute("""
        SELECT DISTINCT ON (fuente) fuente, estado, anuncios, empresas, errores, url_ejecucion,
               to_char(fin AT TIME ZONE 'America/Caracas', 'YYYY-MM-DD HH24:MI') AS fin_local,
               (NOW() - fin) > INTERVAL '%s days' AS atrasada
        FROM corridas_scraper ORDER BY fuente, inicio DESC
    """ % DIAS_CORRIDA_ATRASADA)
    filas = {r['fuente']: dict(r) for r in cur.fetchall()}
    resumen = []
    for fuente in ('Meta', 'Google'):
        r = filas.get(fuente) or {'fuente': fuente, 'estado': 'sin_datos'}
        if r.get('atrasada') and r['estado'] == 'ok':
            r['estado'] = 'atrasada'
        r['etiqueta'], r['color'] = ESTADOS_CORRIDA.get(r['estado'], (r['estado'], 'secondary'))
        r['problema'] = r['estado'] not in ('ok', 'sin_datos')
        resumen.append(r)
    return resumen

def url_con_filtros(**cambios):
    # Enlace al panel que conserva los filtros actuales (búsqueda, fuente, productos...) y solo
    # reemplaza los parámetros indicados; por ejemplo, otra compañía en "Ver Anuncios".
    pares = [(k, v) for k, v in request.args.items(multi=True) if k not in cambios and k != 'msg']
    for clave, valor in cambios.items():
        valores = valor if isinstance(valor, (list, tuple)) else [valor]
        pares.extend((clave, v) for v in valores if v not in (None, ''))
    return '/?' + urlencode(pares)

@app.context_processor
def inyectar_usuario():
    clave = usuario_actual()
    return {
        'usuario_nombre': USUARIOS.get(clave, {}).get('nombre', clave),
        'es_admin': clave == USUARIO_ADMIN,
        'es_beta': clave == USUARIO_BETA,
        'puede_editar_urls': clave in USUARIOS_EDITAN_URLS,
        'usa_formulario_empresas': clave in USUARIOS_FORMULARIO_EMPRESAS,
        'url_con_filtros': url_con_filtros,
        've_salud': clave in USUARIOS_SALUD,
        'nuevo': lambda funcion: Markup('<span class="badge-nuevo" title="Función nueva">NEW</span>')
                 if clave != USUARIO_BETA and es_funcion_nueva(funcion) else '',
        'puede_cambiar_usuario': puede_cambiar_usuario(),
        'usuario_origen_nombre': USUARIOS.get(usuario_origen(), {}).get('nombre', usuario_origen()),
        'cambio_pide_password': {u: cambio_requiere_password(u) for u in USUARIOS},
        'usuarios_disponibles': {u: d['nombre'] for u, d in USUARIOS.items() if d['password']},
    }

def get_db_connection():
    if not DATABASE_URL:
        return None
    url = DATABASE_URL
    if "sslmode=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode=require"
    try:
        return psycopg2.connect(url, connect_timeout=8)
    except Exception as e:
        print(f"Error conectando a la base de datos: {e}")
        return None

def obtener_ip_cliente():
    forwarded = request.headers.get('X-Forwarded-For', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.remote_addr or 'desconocida'

def registrar_acceso(usuario):
    conn = get_db_connection()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO accesos_dashboard (usuario, ip_address, user_agent) VALUES (%s, %s, %s)",
                (usuario, obtener_ip_cliente(), request.headers.get('User-Agent', ''))
            )
            conn.commit()
    except Exception as e:
        print(f"Error registrando acceso: {e}")
    finally:
        conn.close()

# Nombres del panel antes -> después del cambio a nombres cortos (10-oct-2026). Solo lo usa la
# migración de una sola vez de init_config_tables.
RENOMBRES_PANEL_2026_10 = {
    'SAINT CASA DE SOFTWARE': 'SAINT',
    'MiProfit de Softech Consultores': 'Profit',
    'A2Venezuela': 'A2',
    'PSKloud by Premium Soft': 'Premium Soft',
    'Valery Software Empresarial': 'Valery',
    'Fina Partner': 'Fina',
    'SOFI - Sistema administrativo con IA': 'SOFI',
    'Soluciones Lopnet, C.A.': 'Lopnet',
    'Gálac Software': 'Gálac',
    'Saul Casanova': 'Lealty Group',
}

def init_config_tables():
    conn = get_db_connection()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS companias_bloqueadas (
                        id SERIAL PRIMARY KEY,
                        compania VARCHAR(255) UNIQUE NOT NULL,
                        fecha_bloqueo TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("ALTER TABLE anuncios ADD COLUMN IF NOT EXISTS presente_en_meta BOOLEAN DEFAULT TRUE;")
                cur.execute("ALTER TABLE anuncios ADD COLUMN IF NOT EXISTS fuente VARCHAR(20) DEFAULT 'Meta';")
                cur.execute("ALTER TABLE anuncios ADD COLUMN IF NOT EXISTS fecha_ultima_vista VARCHAR(10);")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS migraciones (
                        nombre VARCHAR(100) PRIMARY KEY,
                        fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                # Una sola vez: el bot de Meta marcaba como retirados anuncios activos más viejos
                # que su ventana de búsqueda; se restablecen y la siguiente corrida los re-evalúa bien.
                cur.execute("""
                    INSERT INTO migraciones (nombre) VALUES ('reparar_presente_en_meta_2026_09')
                    ON CONFLICT DO NOTHING RETURNING nombre;
                """)
                if cur.fetchone():
                    cur.execute("UPDATE anuncios SET presente_en_meta = TRUE WHERE COALESCE(fuente, 'Meta') = 'Meta';")
                    print(f"Migración aplicada: {cur.rowcount} anuncios de Meta restablecidos como presentes.")
                # Una sola vez: el panel pasó a nombres cortos (urls.txt, 10-oct-2026) y la página de
                # Saul Casanova se muestra como Lealty Group; se renombran los anuncios ya guardados y
                # las compañías bloqueadas para que ninguna empresa quede partida en dos.
                cur.execute("""
                    INSERT INTO migraciones (nombre) VALUES ('nombres_cortos_panel_2026_10')
                    ON CONFLICT DO NOTHING RETURNING nombre;
                """)
                if cur.fetchone():
                    renombrados = 0
                    for anterior, nuevo in RENOMBRES_PANEL_2026_10.items():
                        cur.execute("UPDATE anuncios SET compania = %s WHERE compania = %s;", (nuevo, anterior))
                        renombrados += cur.rowcount
                        cur.execute("""
                            UPDATE companias_bloqueadas SET compania = %s
                            WHERE compania = %s AND NOT EXISTS (SELECT 1 FROM companias_bloqueadas WHERE compania = %s);
                        """, (nuevo, anterior, nuevo))
                    print(f"Migración aplicada: {renombrados} anuncios pasan a los nombres cortos del panel.")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS accesos_dashboard (
                        id SERIAL PRIMARY KEY,
                        fecha_acceso TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        ip_address VARCHAR(100),
                        user_agent TEXT
                    );
                """)
                cur.execute("ALTER TABLE accesos_dashboard ADD COLUMN IF NOT EXISTS usuario VARCHAR(50);")
                # Columnas de vista previa y destino, historial diario y corridas de los bots.
                registro_bots.crear_tablas(cur)
                conn.commit()
        except Exception as e:
            print(f"Error inicializando tablas: {e}")
        finally:
            conn.close()

init_config_tables()

def get_github_urls_file(path=URLS_FILE_PATH):
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return "", None
    url_api = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    try:
        r = requests.get(url_api, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            content = base64.b64decode(data['content']).decode('utf-8')
            return content, data.get('sha')
        return "", None
    except Exception as e:
        print(f"Error leyendo {path} en GitHub: {e}")
        return "", None

def update_github_urls_file(new_content, path=URLS_FILE_PATH):
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return False, "Falta configurar GITHUB_TOKEN o GITHUB_REPO."

    current_content, sha = get_github_urls_file(path)
    url_api = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    encoded_content = base64.b64encode(new_content.encode('utf-8')).decode('utf-8')
    
    payload = {
        "message": f"Actualizar {path} desde Dashboard",
        "content": encoded_content
    }
    if sha:
        payload["sha"] = sha

    try:
        r = requests.put(url_api, json=payload, headers=headers, timeout=10)
        if r.status_code in [200, 201]:
            return True, f"Archivo {path} actualizado en GitHub exitosamente."
        else:
            return False, f"GitHub respondió con error {r.status_code}: {r.text}"
    except Exception as e:
        return False, f"Error conectando con GitHub: {e}"

def leer_config(path):
    # Se lee desde GitHub para reflejar al instante lo que guarda el admin; sin token
    # (por ejemplo en local) se usa la copia del repositorio.
    contenido, _ = get_github_urls_file(path)
    if not contenido and os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            contenido = f.read()
    return contenido

def normalizar(texto):
    return unicodedata.normalize('NFKD', texto or '').encode('ascii', 'ignore').decode('ascii').lower()

def parse_productos(raw_text):
    reglas = []
    for line in (raw_text or "").splitlines():
        line_clean = line.strip()
        if not line_clean or line_clean.startswith('#') or '|' not in line_clean:
            continue
        nombre, claves = [p.strip() for p in line_clean.split('|', 1)]
        patrones = []
        for clave in claves.split(','):
            clave = normalizar(clave.strip())
            if not clave:
                continue
            prefijo = clave.endswith('*')
            patrones.append(r'\b' + re.escape(clave.rstrip('*')) + ('' if prefijo else r'\b'))
        if nombre and patrones:
            reglas.append((nombre, re.compile('|'.join(patrones))))
    return reglas

def clasificar_productos(ad, reglas):
    # Los títulos genéricos ("Anuncio de <empresa>", también los de Google sin título de YouTube)
    # no traen texto real: clasificarlos por el nombre de la empresa daría falsos positivos.
    titulo = ad.get('titulo') or ''
    if titulo.startswith('Anuncio de '):
        titulo = ''
    texto = normalizar(f"{ad.get('texto') or ''} {titulo}")
    return [nombre for nombre, patron in reglas if patron.search(texto)]

ICONOS_DESTINO = {
    'WhatsApp': 'bi-whatsapp', 'Sitio web': 'bi-globe2', 'Messenger': 'bi-messenger',
    'Instagram (mensaje)': 'bi-instagram', 'Instagram (perfil)': 'bi-instagram', 'Formulario': 'bi-ui-checks',
    'Facebook': 'bi-facebook', 'Llamada': 'bi-telephone', 'Sin enlace': 'bi-dash-circle',
}

def destinos_por_empresa(anuncios, maximo=10):
    # Gráfico "¿A dónde llevan los anuncios?": anuncios filtrados por empresa y destino.
    conteo = {}
    for a in anuncios:
        compania = (a.get('compania') or '').strip()
        if compania:
            fila = conteo.setdefault(compania, Counter())
            fila[a.get('destino') or 'Sin dato'] += 1
    if not any(d != 'Sin dato' for fila in conteo.values() for d in fila):
        return {'companias': [], 'destinos': [], 'series': {}}
    companias = sorted(conteo, key=lambda c: -sum(conteo[c].values()))[:maximo]
    totales = Counter()
    for c in companias:
        totales.update(conteo[c])
    destinos = [d for d, _ in totales.most_common() if d != 'Sin dato'] + (['Sin dato'] if totales['Sin dato'] else [])
    return {'companias': companias, 'destinos': destinos,
            'series': {d: [conteo[c].get(d, 0) for c in companias] for d in destinos}}

def historial_activos(companias_sel, fuente, companias_bloqueadas, dias=90, maximo=None):
    # Gráfico "Anuncios activos en el tiempo": suma Meta + Google salvo que se filtre por fuente.
    vacio = {'fechas': [], 'series': []}
    conn = get_db_connection()
    if not conn:
        return vacio
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT fecha, compania, SUM(activos) FROM historial_activos
                WHERE fecha >= CURRENT_DATE - %s AND (%s = '' OR fuente = %s)
                GROUP BY fecha, compania ORDER BY fecha
            """, (dias, fuente or '', fuente or ''))
            filas = cur.fetchall()
    except Exception as e:
        print(f"Error leyendo el historial de anuncios activos: {e}")
        return vacio
    finally:
        conn.close()
    bloqueadas = {c.lower() for c in companias_bloqueadas}
    filas = [(f, c, int(n)) for f, c, n in filas
             if c.lower() not in bloqueadas and (not companias_sel or c in companias_sel)]
    fechas = sorted({f for f, _, _ in filas})
    if not fechas:
        return vacio
    valores = {(f, c): n for f, c, n in filas}
    ultima = fechas[-1]
    companias = sorted({c for _, c, _ in filas}, key=lambda c: (-valores.get((ultima, c), 0), c))[:maximo]
    return {'fechas': [f.strftime('%d/%m') for f in fechas],
            'series': [{'label': c, 'data': [valores.get((f, c)) for f in fechas]} for c in companias]}

def comparativa_empresas(anuncios, palabras_excluidas):
    # Pestaña "Comparar" (Beta): métricas por empresa sobre los anuncios ya filtrados. El navegador
    # muestra solo las empresas elegidas, sin recargar la página.
    datos = {}
    for a in anuncios:
        compania = (a.get('compania') or '').strip()
        if not compania:
            continue
        d = datos.setdefault(compania, {
            'compania': compania, 'total': 0, 'vigentes': 0, 'videos': 0, 'fotos': 0, 'textos': 0, 'otros': 0,
            'meta': 0, 'google': 0, 'winning': 0, 'nuevos7': 0, 'suma_dias': 0, 'con_dias': 0,
            'productos': Counter(), 'destinos': Counter(), 'plataformas': Counter(), 'palabras': Counter(),
            'anuncios': [],
        })
        d['total'] += 1
        d['vigentes'] += 1 if a.get('presente_en_meta') else 0
        formato = str(a.get('formato') or '').lower()
        if 'video' in formato or (a.get('duracion_segundos') or 0) > 0:
            d['videos'] += 1
            categoria = 'video'
        elif 'foto' in formato or 'imagen' in formato:
            d['fotos'] += 1
            categoria = 'foto'
        elif 'texto' in formato:
            d['textos'] += 1
            categoria = 'texto'
        else:
            d['otros'] += 1
            categoria = 'otro'
        con_fecha = a.get('fecha_display') not in (None, 'N/A')
        # Lo justo para listar el anuncio al desplegar una fila de la tabla comparativa.
        d['anuncios'].append({
            'enlace': a.get('link_individual') or '',
            'imagen': a.get('imagen_url') or '',
            'texto': str(a.get('texto') or a.get('titulo') or '')[:160],
            'formato': categoria,
            'fuente': a.get('fuente') or 'Meta',
            'vigente': bool(a.get('presente_en_meta')),
            'winning': bool(a.get('es_winning')),
            'nuevo': con_fecha and (a.get('dias_activo') or 0) <= 7,
            'dias': a.get('dias_activo') if con_fecha else None,
            'productos': a.get('productos') or [],
            'destino': a.get('destino') or 'Sin dato',
            'plataformas': [x.strip() for x in str(a.get('plataformas') or '').split(',') if x.strip()],
        })
        d['google' if a.get('fuente') == 'Google' else 'meta'] += 1
        d['winning'] += 1 if a.get('es_winning') else 0
        if a.get('fecha_display') not in (None, 'N/A'):
            d['suma_dias'] += a.get('dias_activo') or 0
            d['con_dias'] += 1
            d['nuevos7'] += 1 if (a.get('dias_activo') or 0) <= 7 else 0
        d['productos'].update(a.get('productos') or [SIN_CLASIFICAR])
        d['destinos'][a.get('destino') or 'Sin dato'] += 1
        for plataforma in str(a.get('plataformas') or '').split(','):
            if plataforma.strip():
                d['plataformas'][plataforma.strip()] += 1
        titulo = str(a.get('titulo') or '')
        if not titulo.startswith('Anuncio de '):
            for palabra in re.findall(r'[a-záéíóúñ]{4,}', f"{a.get('texto') or ''} {titulo}".lower()):
                if palabra not in STOPWORDS_ES and palabra not in palabras_excluidas:
                    d['palabras'][palabra] += 1
    resultado = []
    for d in sorted(datos.values(), key=lambda x: (-x['total'], x['compania'])):
        d['dias_promedio'] = round(d.pop('suma_dias') / d['con_dias']) if d['con_dias'] else None
        d.pop('con_dias')
        d['productos'] = dict(d['productos'])
        d['destinos'] = dict(d['destinos'])
        d['plataformas'] = dict(d['plataformas'].most_common(8))
        d['palabras'] = d['palabras'].most_common(6)
        resultado.append(d)
    return resultado

def estadisticas_por_empresa(anuncios):
    # Tabla "Empresas Monitoreadas": se cuenta sobre los anuncios ya filtrados para que
    # responda a los mismos filtros que el resto del panel.
    stats = {}
    for a in anuncios:
        compania = (a.get('compania') or '').strip()
        if not compania:
            continue
        fila = stats.setdefault(compania, {'compania': compania, 'total_anuncios': 0,
                                           'total_videos': 0, 'total_fotos': 0, 'total_textos': 0})
        formato = str(a.get('formato') or '').lower()
        fila['total_anuncios'] += 1
        if 'video' in formato or (a.get('duracion_segundos') or 0) > 0:
            fila['total_videos'] += 1
        elif 'foto' in formato or 'imagen' in formato:
            fila['total_fotos'] += 1
        elif 'texto' in formato:
            fila['total_textos'] += 1
    return sorted(stats.values(), key=lambda f: (-f['total_anuncios'], f['compania']))

def coincide_producto(productos, seleccion):
    # El anuncio pasa si tiene al menos uno de los productos elegidos.
    return any(not productos if p == SIN_CLASIFICAR else p in productos for p in seleccion)

def parse_urls_google(raw_text):
    items = []
    for line in (raw_text or "").strip().splitlines():
        line_clean = line.strip()
        if not line_clean or line_clean.startswith('#'):
            continue
        parts = [p.strip() for p in line_clean.split('|')]
        if len(parts) >= 2 and parts[1]:
            items.append({'nombre_flask': parts[0], 'dominio': parts[1].lower()})
    return items

# ---------- Formulario de empresas (Beta): agregar / editar / eliminar sin escribir el formato a mano ----------
# Por ahora solo Funciones Beta; al liberarlo, todos los usuarios podrán usarlo (pedido del 10-oct-2026).
USUARIOS_FORMULARIO_EMPRESAS = (USUARIO_BETA,)

URL_PAGINA_META = ("https://www.facebook.com/ads/library/?active_status=all&ad_type=all&country=ALL"
                   "&is_targeted_country=false&media_type=all&search_type=page&view_all_page_id={}")
URL_BUSQUEDA_META = ("https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=VE"
                     "&is_targeted_country=false&media_type=all&q={}&search_type=keyword_unordered")
AYUDA_ENLACE_META = ('Abre la Biblioteca de Anuncios de Meta, busca la empresa, entra a su página '
                     '(clic en su nombre o en "Ver todos los anuncios") y copia el enlace de la barra del navegador.')

def tipo_entrada_meta(url):
    m = re.search(r'view_all_page_id=(\d+)', url or '')
    return ('pagina', m.group(1)) if m else ('palabra', None)

def normalizar_entrada_meta(texto, nombre):
    """Convierte lo que pega el usuario en una línea válida de urls.txt.
    Acepta el enlace de la página en la Biblioteca de Anuncios (lo más preciso), un enlace de búsqueda
    o simplemente un nombre (búsqueda por palabra clave). Devuelve (entrada, error)."""
    t = (texto or '').strip()
    if not t:
        return None, None
    if '|' in t:
        return None, 'El enlace no puede contener el carácter "|".'
    es_enlace = re.match(r'^(https?://)?([a-z0-9-]+\.)*facebook\.com/', t, re.I) or t.lower().startswith(('http://', 'https://', 'www.'))
    if not es_enlace:
        if len(t) < 2:
            return None, 'Escribe al menos 2 letras para buscar por nombre.'
        return {'url': URL_BUSQUEDA_META.format(quote(t)), 'bot': t, 'tipo': 'palabra'}, None
    if not re.match(r'^https?://', t, re.I):
        t = 'https://' + t
    partes = urlparse(t)
    host = partes.netloc.lower().split(':')[0]
    for prefijo in ('www.', 'm.', 'web.', 'es-la.', 'es-es.'):
        if host.startswith(prefijo):
            host = host[len(prefijo):]
    if host != 'facebook.com':
        return None, 'El enlace de Meta debe ser de facebook.com (Biblioteca de Anuncios). ' + AYUDA_ENLACE_META
    consulta = parse_qs(partes.query)
    pagina = (consulta.get('view_all_page_id') or [''])[0]
    if pagina.isdigit():
        return {'url': URL_PAGINA_META.format(pagina), 'bot': nombre, 'tipo': 'pagina', 'id': pagina}, None
    termino = (consulta.get('q') or [''])[0].strip()
    if partes.path.startswith('/ads/library') and termino:
        return {'url': URL_BUSQUEDA_META.format(quote(termino)), 'bot': termino, 'tipo': 'palabra'}, None
    if partes.path.startswith('/ads/library') and consulta.get('id'):
        return None, 'Ese enlace es de un solo anuncio, no de la empresa. ' + AYUDA_ENLACE_META
    return None, 'No se encontró la página de la empresa en ese enlace. ' + AYUDA_ENLACE_META

def normalizar_dominio(texto):
    t = (texto or '').strip().lower()
    if not t:
        return None, None
    if '://' not in t:
        t = 'http://' + t
    host = urlparse(t).netloc.split('@')[-1].split(':')[0]
    if host.startswith('www.'):
        host = host[4:]
    if not re.fullmatch(r'(?:[a-z0-9-]+\.)+[a-z]{2,}', host):
        return None, f'"{texto.strip()}" no parece un sitio web válido (ejemplo: galac.com).'
    return host, None

def empresas_configuradas(raw_meta, raw_google):
    # Une urls.txt y urls_google.txt por "Nombre en Panel", en el orden en que aparecen.
    empresas = {}
    for item in parse_urls_txt(raw_meta):
        tipo, pagina = tipo_entrada_meta(item['url'])
        e = empresas.setdefault(item['nombre_flask'], {'nombre': item['nombre_flask'], 'meta': [], 'google': []})
        e['meta'].append({'url': item['url'], 'bot': item['nombre_bot'], 'tipo': tipo, 'id': pagina})
    for item in parse_urls_google(raw_google):
        e = empresas.setdefault(item['nombre_flask'], {'nombre': item['nombre_flask'], 'meta': [], 'google': []})
        e['google'].append(item['dominio'])
    return list(empresas.values())

def reemplazar_lineas_empresa(contenido, nombre_original, nuevas):
    # Quita las líneas de esa empresa y pone las nuevas en el mismo lugar (o al final si es nueva),
    # sin tocar el resto del archivo ni sus comentarios.
    salto = '\r\n' if '\r\n' in (contenido or '') else '\n'
    salida, posicion = [], None
    for linea in (contenido or '').splitlines():
        limpia = linea.strip()
        es_de_empresa = (nombre_original is not None and limpia and not limpia.startswith('#') and '|' in limpia
                         and limpia.split('|')[0].strip() == nombre_original)
        if es_de_empresa:
            if posicion is None:
                posicion = len(salida)
            continue
        salida.append(linea)
    if posicion is None:
        while salida and not salida[-1].strip():
            salida.pop()
        posicion = len(salida)
    salida[posicion:posicion] = nuevas
    return salto.join(salida) + salto if salida else ''

def renombrar_empresa_en_bd(anterior, nuevo):
    # Al cambiar el nombre en el panel, los anuncios ya guardados pasan al nombre nuevo (si no, la
    # empresa quedaría partida en dos).
    conn = get_db_connection()
    if not conn:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE anuncios SET compania = %s WHERE compania = %s", (nuevo, anterior))
            total = cur.rowcount
            cur.execute("""UPDATE companias_bloqueadas SET compania = %s
                           WHERE compania = %s AND NOT EXISTS (SELECT 1 FROM companias_bloqueadas WHERE compania = %s)""",
                        (nuevo, anterior, nuevo))
            cur.execute("""UPDATE historial_activos h SET compania = %s WHERE compania = %s
                           AND NOT EXISTS (SELECT 1 FROM historial_activos x
                                           WHERE x.fecha = h.fecha AND x.fuente = h.fuente AND x.compania = %s)""",
                        (nuevo, anterior, nuevo))
        conn.commit()
        return total
    except Exception as e:
        print(f"Error renombrando {anterior} -> {nuevo}: {e}")
        return 0
    finally:
        conn.close()

def parse_urls_txt(raw_text):
    items = []
    if not raw_text:
        return items
    for idx, line in enumerate(raw_text.strip().splitlines()):
        line_clean = line.strip()
        if not line_clean or line_clean.startswith('#'):
            continue
        parts = [p.strip() for p in line_clean.split('|')]
        
        if len(parts) >= 3:
            items.append({
                'id': idx,
                'nombre_flask': parts[0],
                'nombre_bot': parts[1],
                'url': parts[2]
            })
        elif len(parts) == 2:
            items.append({
                'id': idx,
                'nombre_flask': parts[0],
                'nombre_bot': parts[0],
                'url': parts[1]
            })
        else:
            items.append({
                'id': idx,
                'nombre_flask': 'Empresa Monitoreada',
                'nombre_bot': 'Empresa Monitoreada',
                'url': line_clean
            })
    return items

def extraer_fecha_anuncio(ad):
    posibles_claves = ['fecha_subida', 'fecha_inicio', 'fecha', 'fecha_publicacion', 'fecha_comienzo', 'start_date']
    for clave in posibles_claves:
        val = ad.get(clave)
        if val and str(val).strip() and str(val).strip().lower() not in ['none', 'null', 'n/a', '']:
            return str(val).strip()
    return 'N/A'

def parse_date_str(val):
    if not val or str(val).strip().lower() in ['none', 'null', 'n/a', '']:
        return None
    s = str(val).lower().strip()

    m_txt = re.search(r'(\d{1,2})\s+(?:de\s+)?([a-z]{3,10})\s+(?:de\s+)?(\d{4})', s)
    if m_txt:
        d, mes_str, y = m_txt.groups()
        mes_num = MESES_DICT.get(mes_str[:3], MESES_DICT.get(mes_str, '01'))
        return f"{y}-{mes_num}-{int(d):02d}"

    m_iso = re.search(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})', s)
    if m_iso:
        y, m, d = m_iso.groups()
        return f"{y}-{int(m):02d}-{int(d):02d}"

    m_lat = re.search(r'(\d{1,2})[-/](\d{1,2})[-/](\d{4})', s)
    if m_lat:
        d, m, y = m_lat.groups()
        return f"{y}-{int(m):02d}-{int(d):02d}"

    return s[:10] if len(s) >= 10 else None

def fecha_ultima_vista(ad):
    # Solo Google informa cuándo se mostró un anuncio por última vez. Las filas guardadas antes
    # de existir la columna lo tienen en el título: "(visto por última vez AAAA-MM-DD)".
    if (ad.get('fuente') or 'Meta') != 'Google':
        return None
    valor = ad.get('fecha_ultima_vista')
    if not valor or str(valor).strip().lower() in ('none', 'nan', 'nat', ''):
        m = re.search(r'visto por última vez (\d{4}-\d{2}-\d{2})', str(ad.get('titulo') or ''))
        valor = m.group(1) if m else None
    return str(valor).strip()[:10] if valor else None

def calcular_dias_activo(fecha_val, hasta=None):
    # "hasta" es la última vez que se vio el anuncio: uno que ya no circula deja de sumar días.
    f_norm = parse_date_str(fecha_val)
    if not f_norm:
        return 0
    try:
        dt = datetime.strptime(f_norm, '%Y-%m-%d')
        fin = datetime.now()
        if hasta:
            fin = min(fin, datetime.strptime(hasta, '%Y-%m-%d'))
        return max(0, (fin - dt).days)
    except Exception:
        return 0

def render_plataformas_badges(val):
    if not val:
        return '<span class="text-muted small">-</span>'
    s = str(val).lower()
    badges = []
    if 'facebook' in s or 'fb' in s:
        badges.append('<span class="badge bg-primary text-light" style="font-size: 0.68rem;"><i class="bi bi-facebook"></i> Facebook</span>')
    if 'instagram' in s or 'ig' in s:
        badges.append('<span class="badge text-light" style="background: linear-gradient(45deg, #f09433, #dc2743, #bc1888); font-size: 0.68rem;"><i class="bi bi-instagram"></i> Instagram</span>')
    if 'threads' in s or 'hilos' in s:
        badges.append('<span class="badge bg-dark text-light border border-secondary" style="font-size: 0.68rem;"><i class="bi bi-threads"></i> Threads</span>')
    if 'whatsapp' in s:
        badges.append('<span class="badge text-light" style="background-color: #25d366; font-size: 0.68rem;"><i class="bi bi-whatsapp"></i> WhatsApp</span>')
    if 'messenger' in s:
        badges.append('<span class="badge bg-info text-dark" style="font-size: 0.68rem;"><i class="bi bi-messenger"></i> Messenger</span>')
    if 'audience' in s or 'network' in s:
        badges.append('<span class="badge bg-secondary text-light" style="font-size: 0.68rem;"><i class="bi bi-globe"></i> Audience Network</span>')
    if 'búsqueda' in s:
        badges.append('<span class="badge bg-success text-light" style="font-size: 0.68rem;"><i class="bi bi-google"></i> Búsqueda</span>')
    if 'youtube' in s:
        badges.append('<span class="badge bg-danger text-light" style="font-size: 0.68rem;"><i class="bi bi-youtube"></i> YouTube</span>')
    if 'display' in s:
        badges.append('<span class="badge bg-warning text-dark" style="font-size: 0.68rem;"><i class="bi bi-window"></i> Red de Display</span>')
    if 'maps' in s:
        badges.append('<span class="badge bg-success-subtle text-success" style="font-size: 0.68rem;"><i class="bi bi-geo-alt"></i> Maps</span>')
    if 'google play' in s:
        badges.append('<span class="badge bg-success-subtle text-success" style="font-size: 0.68rem;"><i class="bi bi-google-play"></i> Play</span>')
    if 'shopping' in s:
        badges.append('<span class="badge bg-success-subtle text-success" style="font-size: 0.68rem;"><i class="bi bi-bag"></i> Shopping</span>')
    
    if not badges:
        return f'<span class="badge bg-secondary-subtle text-secondary" style="font-size: 0.68rem;">{val}</span>'
    return ' '.join(badges)

def construir_timeline(lista_anuncios, top_companias):
    timeline_dict = {}
    for a in lista_anuncios:
        f_norm = parse_date_str(a.get('fecha_display'))
        if f_norm:
            comp = a.get('compania', 'Otras')
            if f_norm not in timeline_dict:
                timeline_dict[f_norm] = {}
            timeline_dict[f_norm][comp] = timeline_dict[f_norm].get(comp, 0) + 1

    sorted_dates = sorted(timeline_dict.keys())
    palette = ['#0284c7', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#ec4899', '#06b6d4']
    shapes = ['circle', 'triangle', 'rect', 'rectRot', 'star', 'cross', 'crossRot']

    datasets = []
    for idx, comp in enumerate(top_companias):
        data = [timeline_dict[d].get(comp, 0) for d in sorted_dates]
        color = palette[idx % len(palette)]
        shape = shapes[idx % len(shapes)]
        datasets.append({
            "label": comp,
            "data": data,
            "borderColor": color,
            "backgroundColor": color,
            "pointStyle": shape,
            "pointRadius": 6,
            "pointHoverRadius": 8,
            "tension": 0.25
        })

    return {"labels": sorted_dates, "datasets": datasets}

LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="es" data-bs-theme="light">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Acceso | Gálac Ads Intelligence</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">
    <style>
        :root { --bg-body: #0f172a; --card-bg: #1e293b; --border-color: #334155; --text-main: #f8fafc; }
        body {
            background-color: var(--bg-body);
            color: var(--text-main);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        }
        .login-card {
            background-color: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            padding: 32px;
            width: 100%;
            max-width: 400px;
            box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.3);
        }
    </style>
</head>
<body>

<div class="login-card">
    <div class="text-center mb-4">
        <div class="d-inline-flex p-3 bg-primary bg-opacity-10 text-primary rounded-circle mb-3">
            <i class="bi bi-shield-lock-fill fs-2"></i>
        </div>
        <h4 class="fw-bold mb-1">Acceso Protegido</h4>
        <p class="text-secondary small">Gálac Ads Intelligence Dashboard</p>
    </div>

    {% if error %}
    <div class="alert alert-danger py-2 small border-0 d-flex align-items-center gap-2 mb-3" role="alert">
        <i class="bi bi-exclamation-triangle-fill"></i>
        <span>{{ error }}</span>
    </div>
    {% endif %}

    <form method="POST" action="/login">
        <div class="mb-3">
            <label class="form-label small fw-semibold text-secondary">Usuario</label>
            <select name="usuario" class="form-select bg-dark text-light border-secondary">
                {% for clave, u in usuarios.items() %}
                <option value="{{ clave }}" {% if clave == usuario_sel %}selected{% endif %}>{{ u.nombre }}</option>
                {% endfor %}
            </select>
        </div>
        <div class="mb-3">
            <label class="form-label small fw-semibold text-secondary">Contraseña de Acceso</label>
            <input type="password" name="password" class="form-control bg-dark text-light border-secondary" placeholder="Ingresa la clave..." required autofocus>
        </div>
        <button type="submit" class="btn btn-primary w-100 fw-semibold py-2">
            Ingresar al Dashboard
        </button>
    </form>
</div>

</body>
</html>
"""

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="es" data-bs-theme="light">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Gálac Ads Intelligence</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root {
            --bg-body: #f8fafc;
            --card-bg: #ffffff;
            --border-color: #e2e8f0;
            --text-main: #0f172a;
            --text-muted: #64748b;
        }
        [data-bs-theme="dark"] {
            --bg-body: #0b1120;
            --card-bg: #1e293b;
            --border-color: #334155;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
        }
        body {
            background-color: var(--bg-body);
            color: var(--text-main);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            transition: background-color 0.3s ease, color 0.3s ease;
        }
        .navbar { background-color: #0f172a !important; border-bottom: 1px solid #1e293b; }
        .card-custom {
            background-color: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            transition: transform 0.2s ease, box-shadow 0.2s ease;
        }
        .card-custom:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1);
        }
        .stat-value { font-size: 1.85rem; font-weight: 700; color: var(--text-main); }
        .stat-label { font-size: 0.75rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-muted); }
        .table { color: var(--text-main); }
        .badge-active { background-color: #10b981; color: #ffffff; }
        .badge-inactive { background-color: #64748b; color: #ffffff; }
        .badge-new { background-color: #6366f1; color: #ffffff; animation: pulse 2s infinite; }
        .badge-winning { background: linear-gradient(135deg, #f59e0b 0%, #ea580c 100%); color: #ffffff; font-weight: 700; border: none; }
        .badge-retirado { background-color: #dc2626; color: #ffffff; font-weight: 600; }
        @keyframes pulse {
            0% { opacity: 1; }
            50% { opacity: 0.6; }
            100% { opacity: 1; }
        }
        .card-filter-container {
            position: relative;
            z-index: 50;
            overflow: visible !important;
        }
        .dropdown {
            position: relative;
            overflow: visible !important;
        }
        .dropdown-menu {
            position: absolute !important;
            z-index: 9999 !important;
            background-color: var(--card-bg) !important;
            color: var(--text-main) !important;
            border: 1px solid var(--border-color) !important;
            box-shadow: 0 15px 35px rgba(0, 0, 0, 0.3) !important;
        }
        .dropdown-menu-scroll { max-height: 250px; overflow-y: auto; }
        .sync-container { min-width: 230px; }
        .progress-inline {
            height: 5px;
            border-radius: 3px;
            overflow: hidden;
            background-color: rgba(255, 255, 255, 0.15);
        }

        /* ===== Diseño v2: solo para Funciones Beta (clase en <body>) ===== */
        .diseno-v2 {
            --radius: 14px;
            --ease-out: cubic-bezier(.22, 1, .36, 1);
            --shadow-sm: 0 1px 2px rgba(15, 23, 42, .05), 0 2px 6px rgba(15, 23, 42, .06);
            --shadow-md: 0 14px 30px -12px rgba(15, 23, 42, .25);
            font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background-image:
                radial-gradient(1100px 380px at 8% -8%, rgba(59, 130, 246, .09), transparent 60%),
                radial-gradient(900px 320px at 100% 0%, rgba(99, 102, 241, .07), transparent 60%);
            background-attachment: fixed;
        }
        .diseno-v2 .navbar {
            background-color: rgba(15, 23, 42, .94) !important;
            box-shadow: 0 6px 24px rgba(2, 6, 23, .25);
        }
        .diseno-v2 .navbar-brand i {
            background: linear-gradient(135deg, #3b82f6, #8b5cf6);
            -webkit-background-clip: text;
            background-clip: text;
            color: transparent !important;
        }
        .diseno-v2 .card-custom {
            border-radius: var(--radius);
            box-shadow: var(--shadow-sm);
            transition: translate .35s var(--ease-out), box-shadow .35s var(--ease-out), border-color .35s ease;
        }
        /* En la ventana de Comparar las tarjetas no se mueven al pasar el mouse: el movimiento (transform)
           encerraba el desplegable de compañías en su tarjeta y la tabla de abajo lo tapaba. */
        #modalComparar .card-custom:hover { transform: none; translate: none; }
        #modalComparar .comparar-selector { position: relative; z-index: 5; }
        .diseno-v2 .modal-content.card-custom:hover { translate: none; box-shadow: var(--shadow-md); border-color: var(--border-color); }
        .diseno-v2 .card-custom:hover {
            transform: none;
            translate: 0 -3px;
            box-shadow: var(--shadow-md);
            border-color: rgba(59, 130, 246, .35);
        }
        .diseno-v2 .card-filter-container:hover { translate: none; }
        .diseno-v2 .stat-value {
            font-size: 2rem;
            letter-spacing: -.02em;
            font-variant-numeric: tabular-nums;
        }
        .diseno-v2 .stat-label { letter-spacing: .07em; }
        .diseno-v2 .kpi-row .card-custom i {
            display: inline-grid;
            place-items: center;
            width: 2.3rem;
            height: 2.3rem;
            border-radius: 11px;
            background: color-mix(in srgb, currentColor 13%, transparent);
            transition: scale .35s var(--ease-out), rotate .35s var(--ease-out);
        }
        .diseno-v2 .kpi-row .card-custom:hover i { scale: 1.12; rotate: -6deg; }
        .diseno-v2 .btn {
            border-radius: 9px;
            transition: translate .2s var(--ease-out), box-shadow .2s ease, background-color .2s ease, border-color .2s ease, color .2s ease;
        }
        .diseno-v2 .btn:hover { translate: 0 -1px; }
        .diseno-v2 .btn:active { translate: 0 0; }
        .diseno-v2 .btn-primary:hover { box-shadow: 0 8px 18px -8px rgba(37, 99, 235, .7); }
        .diseno-v2 .form-control, .diseno-v2 .form-select {
            border-radius: 9px;
            transition: border-color .2s ease, box-shadow .2s ease;
        }
        .diseno-v2 .nav-tabs { border-bottom-color: var(--border-color); gap: .15rem; }
        /* Pestañas algo más compactas para que quepan en una sola línea en pantallas de ~1280 px */
        .diseno-v2 #mainTab .nav-link { font-size: .9rem; padding: .5rem .8rem; white-space: nowrap; }
        .diseno-v2 .nav-tabs .nav-link {
            position: relative;
            border: 0;
            background: transparent;
            border-radius: 10px 10px 0 0;
            transition: background-color .2s ease, color .2s ease;
        }
        .diseno-v2 .nav-tabs .nav-link:hover { background: color-mix(in srgb, currentColor 7%, transparent); }
        .diseno-v2 .nav-tabs .nav-link::after {
            content: "";
            position: absolute;
            inset: auto 12px 0 12px;
            height: 3px;
            border-radius: 3px 3px 0 0;
            background: currentColor;
            scale: 0 1;
            transition: scale .35s var(--ease-out);
        }
        .diseno-v2 .nav-tabs .nav-link.active::after { scale: 1 1; }
        .diseno-v2 .table > :not(caption) > * > * { padding-block: .7rem; }
        .diseno-v2 .table-hover > tbody > tr > * { transition: background-color .2s ease; }
        .diseno-v2 .badge { border-radius: 999px; font-weight: 600; }
        [data-bs-theme="dark"] .diseno-v2 .table-light {
            --bs-table-bg: #172033;
            --bs-table-color: var(--text-muted);
            --bs-table-border-color: var(--border-color);
        }
        .diseno-v2 .modal-content { border-radius: 18px; box-shadow: 0 30px 60px -20px rgba(2, 6, 23, .45); }

        @keyframes aparecer {
            from { opacity: 0; translate: 0 14px; }
        }
        @media (prefers-reduced-motion: no-preference) {
            /* Las animaciones se repiten solas al mostrar una pestaña: el contenido pasa de display:none a visible. */
            .diseno-v2 .kpi-row > *,
            .diseno-v2 .card-filter-container,
            .diseno-v2 .nav-tabs,
            .diseno-v2 .tab-pane.active .card-custom,
            .diseno-v2 .alert {
                animation: aparecer .6s var(--ease-out) backwards;
            }
            .diseno-v2 .kpi-row > :nth-child(2) { animation-delay: 60ms; }
            .diseno-v2 .kpi-row > :nth-child(3) { animation-delay: 120ms; }
            .diseno-v2 .kpi-row > :nth-child(4) { animation-delay: 180ms; }
            .diseno-v2 .kpi-row > :nth-child(5) { animation-delay: 240ms; }
            .diseno-v2 .card-filter-container { animation-delay: 200ms; }
            .diseno-v2 .nav-tabs { animation-delay: 260ms; }
            .diseno-v2 .tab-pane.active tbody tr { animation: aparecer .45s var(--ease-out) backwards; }
            .diseno-v2 .tab-pane.active tbody tr:nth-child(2) { animation-delay: 30ms; }
            .diseno-v2 .tab-pane.active tbody tr:nth-child(3) { animation-delay: 60ms; }
            .diseno-v2 .tab-pane.active tbody tr:nth-child(4) { animation-delay: 90ms; }
            .diseno-v2 .tab-pane.active tbody tr:nth-child(5) { animation-delay: 120ms; }
            .diseno-v2 .tab-pane.active tbody tr:nth-child(6) { animation-delay: 150ms; }
            .diseno-v2 .tab-pane.active tbody tr:nth-child(7) { animation-delay: 180ms; }
            .diseno-v2 .tab-pane.active tbody tr:nth-child(8) { animation-delay: 210ms; }
            .diseno-v2 .tab-pane.active tbody tr:nth-child(n+9) { animation-delay: 240ms; }

            /* Mejora progresiva: la tabla de empresas aparece al hacer scroll donde el navegador lo soporta. */
            @supports ((animation-timeline: view()) and (animation-range: entry)) {
                .diseno-v2 .tab-pane.active .reveal-scroll {
                    animation: aparecer linear backwards;
                    animation-timeline: view();
                    animation-range: entry 0% entry 60%;
                }
            }
        }
        /* Filtros automáticos (Beta): tras recargar por un filtro solo se animan los resultados,
           no la cabecera, y mientras carga se atenúa el contenido. */
        .diseno-v2.filtro-recargado .kpi-row > *,
        .diseno-v2.filtro-recargado .card-filter-container,
        .diseno-v2.filtro-recargado .nav-tabs { animation: none; }
        .diseno-v2 .tab-content { transition: opacity .2s ease; }
        .celda-miniatura { width: 72px; }
        #modalComparar .fila-comparar { cursor: pointer; }
        #modalComparar .fila-comparar .flecha-comparar { display: inline-block; transition: transform .2s ease; color: var(--bs-secondary-color); }
        #modalComparar .fila-comparar.abierta .flecha-comparar { transform: rotate(90deg); }
        #modalComparar .fila-comparar.abierta > td { background: color-mix(in srgb, var(--bs-primary) 8%, transparent); }
        #modalComparar .detalle-fila > td { background: color-mix(in srgb, var(--bs-primary) 4%, transparent); }
        .detalle-comparar { max-height: 340px; overflow-y: auto; padding-right: 4px; }
        .miniatura.miniatura-chica, .miniatura.miniatura-chica img { width: 40px; height: 40px; flex-shrink: 0; border-radius: 8px; }
        .miniatura.miniatura-chica:hover img { transform: scale(2.6); }
        .rejilla-detalle { display: grid; gap: 1rem; padding: .75rem .5rem .75rem; overflow-x: auto; }
        /* Animaciones de la comparación (se desactivan con "reducir movimiento") */
        #modalComparar .detalle-fila > td { padding: 0 !important; }
        .desplegable {
            display: grid; grid-template-rows: 0fr; opacity: 0;
            transition: grid-template-rows .38s cubic-bezier(.22, 1, .36, 1), opacity .25s ease;
        }
        .desplegable.abierto { grid-template-rows: 1fr; opacity: 1; }
        .desplegable-interior { overflow: hidden; min-height: 0; }
        @keyframes entrar-suave { from { opacity: 0; translate: 0 6px; } }
        .item-detalle { animation: entrar-suave .35s cubic-bezier(.22, 1, .36, 1) backwards; }
        #compararTabla .fila-comparar { animation: entrar-suave .3s cubic-bezier(.22, 1, .36, 1) backwards; transition: background-color .2s ease; }
        #compararTabla .fila-comparar:hover > td { background: color-mix(in srgb, var(--bs-primary) 5%, transparent); }
        @media (prefers-reduced-motion: reduce) {
            .desplegable { transition: none; }
            .item-detalle, #compararTabla .fila-comparar { animation: none; }
        }
        .text-truncate-2 { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
        .miniatura {
            position: relative; display: block; width: 56px; height: 56px; border-radius: 10px; overflow: visible;
            background: var(--border-color);
        }
        .miniatura img {
            width: 56px; height: 56px; object-fit: cover; border-radius: 10px; border: 1px solid var(--border-color);
            transition: transform .25s cubic-bezier(.22, 1, .36, 1) .25s, box-shadow .25s ease .25s; transform-origin: left center;
            position: relative; z-index: 1; background: var(--card-bg);
        }
        .miniatura:hover img { transform: scale(3.4); box-shadow: 0 18px 40px rgba(15, 23, 42, .35); z-index: 20; }
        .miniatura.sin-imagen { display: grid; place-items: center; color: var(--bs-secondary-color); font-size: 1.2rem; }
        @media (prefers-reduced-motion: reduce) { .miniatura img { transition: none; } }
        .badge-nuevo {
            display: inline-block; margin-left: .35rem; padding: .12rem .42rem; border-radius: 999px;
            font-size: .6rem; font-weight: 700; letter-spacing: .04em; line-height: 1.3; vertical-align: middle;
            color: #fff; background: linear-gradient(135deg, #10b981, #059669);
            box-shadow: 0 0 0 0 rgba(16, 185, 129, .5); animation: brillo-nuevo 2.4s ease-out infinite;
        }
        @keyframes brillo-nuevo { 0% { box-shadow: 0 0 0 0 rgba(16, 185, 129, .5); } 70%, 100% { box-shadow: 0 0 0 6px rgba(16, 185, 129, 0); } }
        @media (prefers-reduced-motion: reduce) { .badge-nuevo { animation: none; } }
        .texto-completo { cursor: help; text-decoration: underline dotted rgba(127, 127, 127, .5); text-underline-offset: 3px; }
        /* Texto completo del anuncio: mismo fondo, borde y letra que las tarjetas, en modo claro y oscuro */
        .tooltip.tooltip-texto {
            --bs-tooltip-bg: var(--card-bg);
            --bs-tooltip-color: var(--text-main);
            --bs-tooltip-opacity: 1;
            --bs-tooltip-max-width: 440px;
            --bs-tooltip-font-size: .8rem;
            font-family: inherit;
            filter: drop-shadow(0 12px 24px rgba(15, 23, 42, .22));
        }
        .diseno-v2 .tooltip.tooltip-texto { font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
        .tooltip-texto .tooltip-inner {
            text-align: left; white-space: pre-line; line-height: 1.5;
            padding: .65rem .85rem; border: 1px solid var(--border-color); border-radius: 12px;
        }
        .tooltip-texto.fade { transition: opacity .2s ease, translate .2s cubic-bezier(.22, 1, .36, 1); }
        .tooltip-texto.fade:not(.show) { translate: 0 4px; }
        @media (prefers-reduced-motion: reduce) { .tooltip-texto.fade:not(.show) { translate: none; } }
        .diseno-v2.aplicando-filtros .tab-content { opacity: .45; pointer-events: none; }
        @media (prefers-reduced-motion: reduce) {
            .diseno-v2 .badge-new { animation: none; }
            .diseno-v2 .card-custom:hover, .diseno-v2 .btn:hover { translate: none; }
        }
    </style>
    {% if es_beta %}
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    {% endif %}
    <script>
        // Vista previa caducada o rota (las URLs de Meta vencen en unos días): se muestra un ícono.
        function sinImagen(img) {
            const enlace = img.parentElement;
            enlace.classList.add('sin-imagen');
            enlace.title = 'La vista previa ya no está disponible; clic para abrir el anuncio';
            enlace.innerHTML = '<i class="bi bi-image"></i>';
        }
    </script>
</head>
<body class="{% if es_beta %}diseno-v2{% endif %}">
{% if es_beta %}<script>try { if (sessionStorage.getItem('filtroAuto')) document.body.classList.add('filtro-recargado'); } catch (e) {}</script>{% endif %}

<nav class="navbar navbar-expand-lg navbar-dark px-3 py-2 sticky-top">
    <div class="container-fluid">
        <a class="navbar-brand d-flex align-items-center gap-2" href="/">
            <i class="bi bi-graph-up-arrow text-primary fs-4"></i>
            <span class="fw-bold tracking-tight">Gálac Ads Intelligence</span>
            {% if es_beta %}<span class="badge bg-warning text-dark" title="Estás viendo funciones que aún no están disponibles para los demás usuarios">BETA</span>{% endif %}
        </a>
        <div class="d-flex align-items-center gap-2 ms-auto">
            
            <button class="btn btn-sm btn-outline-light d-flex align-items-center gap-1" data-bs-toggle="modal" data-bs-target="#modalConfigUrls">
                <i class="bi bi-file-earmark-code fs-6"></i> URLs ({{ config_urls|length + config_google|length }})
            </button>

            <button class="btn btn-sm btn-outline-danger d-flex align-items-center gap-1" data-bs-toggle="modal" data-bs-target="#modalBlockedCompanies">
                <i class="bi bi-slash-circle"></i> Bloqueadas ({{ companias_bloqueadas|length }})
            </button>
            {% if es_beta %}
            <button class="btn btn-sm btn-outline-info d-flex align-items-center gap-1" data-bs-toggle="modal" data-bs-target="#modalComparar" title="Comparar dos o más compañías">
                <i class="bi bi-layout-split"></i> Comparar
            </button>
            {% endif %}

            <div class="sync-container d-flex flex-column gap-1 ms-1">
                <form action="/lanzar_scraper" method="POST" id="scraperForm" onsubmit="startInlineScraping(event)" class="d-flex align-items-center gap-2 m-0">
                    <select name="dias_scraping" id="selectDiasScraping" class="form-select form-select-sm bg-dark text-light border-secondary" style="width: 100px;">
                        <option value="7">7 días</option>
                        <option value="15">15 días</option>
                        <option value="30" selected>30 días</option>
                        <option value="60">60 días</option>
                    </select>
                    <button type="submit" id="btnSyncScraper" class="btn btn-sm btn-primary text-nowrap d-flex align-items-center gap-1">
                        <i class="bi bi-arrow-repeat" id="syncIcon"></i> <span id="syncText">Sincronizar</span>
                    </button>
                </form>

                <div id="inlineProgressWrapper" class="d-none">
                    <div class="progress progress-inline">
                        <div id="inlineProgressBar" class="progress-bar progress-bar-striped progress-bar-animated bg-info" style="width: 0%;"></div>
                    </div>
                    <div class="d-flex justify-content-between align-items-center mt-1" style="font-size: 0.68rem;">
                        <span id="inlineProgressStatus" class="text-info text-truncate" style="max-width: 130px;">Iniciando...</span>
                        <span id="inlineProgressETA" class="text-warning fw-semibold">0s restantes</span>
                    </div>
                </div>
            </div>

            <div class="dropdown ms-1">
                <button class="btn btn-sm btn-outline-secondary dropdown-toggle text-light border-0" type="button" data-bs-toggle="dropdown" aria-expanded="false" title="Configuración">
                    <i class="bi bi-gear-fill fs-5"></i>
                </button>
                <ul class="dropdown-menu dropdown-menu-end shadow-sm">
                    <li><h6 class="dropdown-header"><i class="bi bi-person-circle me-1"></i> Sesión: {{ usuario_nombre }}{% if usuario_origen_nombre != usuario_nombre %} <span class="fw-normal">(desde {{ usuario_origen_nombre }})</span>{% endif %}</h6></li>
                    {% if puede_cambiar_usuario %}
                    <li>
                        <button class="dropdown-item d-flex align-items-center gap-2" type="button" data-bs-toggle="modal" data-bs-target="#modalCambiarUsuario">
                            <i class="bi bi-people"></i> Cambiar de Usuario {{ nuevo('cambiar_usuario') }}
                        </button>
                    </li>
                    {% endif %}
                    {% if ve_salud %}
                    <li>
                        <a class="dropdown-item d-flex align-items-center gap-2" href="/salud">
                            <i class="bi bi-heart-pulse"></i> Salud de los Bots
                            {% if alerta_salud %}<span class="badge rounded-pill bg-warning text-dark ms-auto" title="Hay una corrida que revisar">!</span>{% endif %}
                        </a>
                    </li>
                    {% endif %}
                    {% if es_admin %}
                    <li>
                        <a class="dropdown-item d-flex align-items-center gap-2" href="/accesos">
                            <i class="bi bi-clock-history"></i> Historial de Accesos
                        </a>
                    </li>
                    {% endif %}
                    <li><hr class="dropdown-divider"></li>
                    <li><h6 class="dropdown-header">Apariencia</h6></li>
                    <li>
                        <button class="dropdown-item d-flex align-items-center justify-content-between" onclick="toggleTheme()">
                            <span id="themeTextLabel"><i class="bi bi-moon-stars me-2"></i>Modo Oscuro</span>
                        </button>
                    </li>
                    <li><hr class="dropdown-divider"></li>
                    <li>
                        <a class="dropdown-item text-danger d-flex align-items-center gap-2" href="/logout">
                            <i class="bi bi-box-arrow-right"></i> Cerrar Sesión
                        </a>
                    </li>
                </ul>
            </div>
        </div>
    </div>
</nav>

{% if puede_cambiar_usuario %}
<!-- Modal: Cambiar de usuario (solo sesiones iniciadas como Administrador o Funciones Beta) -->
<div class="modal fade" id="modalCambiarUsuario" tabindex="-1">
    <div class="modal-dialog modal-sm modal-dialog-centered">
        <form class="modal-content card-custom" method="POST" action="/cambiar_usuario">
            <div class="modal-header border-secondary border-opacity-25">
                <h5 class="modal-title fw-bold"><i class="bi bi-people text-primary"></i> Cambiar de Usuario</h5>
                <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Cerrar"></button>
            </div>
            <div class="modal-body">
                <label for="cambioUsuarioSelect" class="form-label small fw-semibold">Ver el panel como</label>
                <select id="cambioUsuarioSelect" name="usuario" class="form-select form-select-sm mb-3">
                    {% for clave, nombre in usuarios_disponibles.items() %}
                    <option value="{{ clave }}" data-pide-password="{{ '1' if cambio_pide_password[clave] else '0' }}" {% if nombre == usuario_nombre %}selected{% endif %}>{{ nombre }}</option>
                    {% endfor %}
                </select>
                <div id="cambioUsuarioPassword" class="d-none">
                    <label for="cambioUsuarioPasswordInput" class="form-label small fw-semibold">Contraseña del Administrador</label>
                    <input id="cambioUsuarioPasswordInput" type="password" name="password" class="form-control form-control-sm" autocomplete="current-password">
                </div>
                <p class="small text-muted mb-0 mt-2">Puedes volver a {{ usuario_origen_nombre }} desde este mismo menú sin cerrar sesión.</p>
            </div>
            <div class="modal-footer border-secondary border-opacity-25">
                <button type="button" class="btn btn-sm btn-outline-secondary" data-bs-dismiss="modal">Cancelar</button>
                <button type="submit" class="btn btn-sm btn-primary">Cambiar</button>
            </div>
        </form>
    </div>
</div>
<script>
(function () {
    const select = document.getElementById('cambioUsuarioSelect');
    const bloque = document.getElementById('cambioUsuarioPassword');
    const input = document.getElementById('cambioUsuarioPasswordInput');
    function actualizar() {
        const pide = select.selectedOptions[0]?.dataset.pidePassword === '1';
        bloque.classList.toggle('d-none', !pide);
        input.required = pide;
        if (!pide) input.value = '';
    }
    select.addEventListener('change', actualizar);
    actualizar();
})();
</script>
{% endif %}

{% if es_beta %}
<!-- Modal: imagen completa de la vista previa de un anuncio -->
<div class="modal fade" id="modalVistaPrevia" tabindex="-1" aria-labelledby="vistaPreviaTitulo">
    <div class="modal-dialog modal-lg modal-dialog-centered">
        <div class="modal-content card-custom">
            <div class="modal-header border-secondary border-opacity-25">
                <h5 class="modal-title fw-bold" id="vistaPreviaTitulo"><i class="bi bi-image text-primary"></i> <span id="vistaPreviaCompania"></span></h5>
                <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Cerrar"></button>
            </div>
            <div class="modal-body text-center">
                <img id="vistaPreviaImagen" src="" alt="Vista previa del anuncio" referrerpolicy="no-referrer"
                     class="img-fluid rounded-3 shadow-sm" style="max-height: 70vh; object-fit: contain;">
                <p id="vistaPreviaTexto" class="small text-muted text-start mt-3 mb-0"></p>
            </div>
            <div class="modal-footer border-secondary border-opacity-25">
                <button type="button" class="btn btn-sm btn-outline-secondary" data-bs-dismiss="modal">Cerrar</button>
                <a id="vistaPreviaEnlace" href="#" target="_blank" rel="noopener" class="btn btn-sm btn-primary"><i class="bi bi-box-arrow-up-right"></i> Abrir anuncio</a>
            </div>
        </div>
    </div>
</div>
{% endif %}

{% if es_beta %}
<!-- Modal: Comparar compañías (Beta). Las compañías se eligen dentro de la ventana. -->
<div class="modal fade" id="modalComparar" tabindex="-1" aria-labelledby="compararTitulo">
    <div class="modal-dialog modal-xl modal-fullscreen-lg-down">
        <div class="modal-content card-custom">
            <div class="modal-header border-secondary border-opacity-25">
                <h5 class="modal-title fw-bold" id="compararTitulo"><i class="bi bi-layout-split text-primary"></i> Comparar Compañías <span class="badge bg-warning text-dark fs-6 align-middle">BETA</span></h5>
                <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Cerrar"></button>
            </div>
            <div class="modal-body">
                <div class="card-custom p-3 mb-3 comparar-selector">
                    <div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-2">
                        <span class="small fw-semibold">Elige 2 o más compañías</span>
                        <span class="small text-muted">Se usan los filtros del panel (fuente, tiempo, producto, búsqueda…).</span>
                    </div>
                    <div class="d-flex flex-wrap align-items-center gap-2">
                        <div class="dropdown flex-grow-1" style="max-width: 460px;">
                            <button class="form-select form-select-sm text-start d-flex justify-content-between align-items-center" type="button" data-bs-toggle="dropdown" data-bs-auto-close="outside">
                                <span class="text-truncate" id="compararEtiqueta">Elegir compañías</span>
                            </button>
                            <div class="dropdown-menu dropdown-menu-scroll p-2 w-100 shadow-lg" id="compararSelector"></div>
                        </div>
                        <button type="button" id="compararExportar" class="btn btn-sm btn-success d-flex align-items-center gap-1" title="Descargar la comparación en Excel">
                            <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><rect x="1" y="1" width="14" height="14" rx="2.5" fill="#fff"/><path d="M5.3 4.6l5.4 6.8M10.7 4.6l-5.4 6.8" stroke="#107C41" stroke-width="1.9" stroke-linecap="round"/></svg>
                            Exportar a Excel
                        </button>
                    </div>
                    <div id="compararAviso" class="small text-warning mt-2 d-none"></div>
                </div>
                <div id="compararContenido" class="d-none">
                    <div class="card-custom overflow-hidden mb-3">
                        <div class="table-responsive">
                            <table class="table table-hover align-middle mb-0" id="compararTabla"></table>
                        </div>
                    </div>
                    <div class="row g-3 mb-3">
                        <div class="col-lg-6">
                            <div class="card-custom p-3 h-100">
                                <h6 class="fw-bold mb-3"><i class="bi bi-collection-play"></i> Formatos</h6>
                                <div style="height: 280px;"><canvas id="compararFormatos"></canvas></div>
                            </div>
                        </div>
                        <div class="col-lg-6">
                            <div class="card-custom p-3 h-100">
                                <h6 class="fw-bold mb-3"><i class="bi bi-box-seam"></i> Productos que anuncian</h6>
                                <div style="height: 280px;"><canvas id="compararProductos"></canvas></div>
                            </div>
                        </div>
                        <div class="col-lg-6">
                            <div class="card-custom p-3 h-100">
                                <h6 class="fw-bold mb-3"><i class="bi bi-activity"></i> Anuncios activos en el tiempo</h6>
                                <div id="compararHistorialVacio" class="text-center text-muted py-5 small d-none"><i class="bi bi-hourglass-split fs-3 d-block mb-2"></i>Se irá llenando con cada corrida de los bots.</div>
                                <div style="height: 280px;"><canvas id="compararHistorial"></canvas></div>
                            </div>
                        </div>
                        <div class="col-lg-6">
                            <div class="card-custom p-3 h-100">
                                <h6 class="fw-bold mb-3"><i class="bi bi-signpost-split"></i> ¿A dónde llevan los anuncios?</h6>
                                <div style="height: 280px;"><canvas id="compararDestinos"></canvas></div>
                            </div>
                        </div>
                    </div>
                    <div class="card-custom p-3">
                        <h6 class="fw-bold mb-3"><i class="bi bi-chat-square-quote"></i> Palabras más usadas por cada una</h6>
                        <div class="row g-3" id="compararPalabras"></div>
                    </div>
                </div>
            </div>
        </div>
    </div>
</div>
{% endif %}

<!-- Modal: Editor urls.txt en GitHub -->
<div class="modal fade" id="modalConfigUrls" tabindex="-1">
    <div class="modal-dialog modal-lg modal-dialog-centered">
        <div class="modal-content card-custom">
            <div class="modal-header border-secondary border-opacity-25">
                <h5 class="modal-title fw-bold"><i class="bi bi-file-earmark-text text-primary"></i> Editor de URLs (GitHub)</h5>
                <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
            </div>
            <div class="modal-body p-4">
                <ul class="nav nav-tabs mb-3" role="tablist">
                    {% if usa_formulario_empresas %}
                    <li class="nav-item">
                        <button class="nav-link active fw-semibold" data-bs-toggle="tab" data-bs-target="#urls-tab-empresas" type="button">
                            <i class="bi bi-buildings"></i> Empresas ({{ empresas_config|length }}) <span class="badge bg-warning text-dark">BETA</span>
                        </button>
                    </li>
                    {% endif %}
                    <li class="nav-item">
                        <button class="nav-link {% if not usa_formulario_empresas %}active {% endif %}fw-semibold" data-bs-toggle="tab" data-bs-target="#urls-tab-meta" type="button">
                            <i class="bi bi-meta"></i> Meta ({{ config_urls|length }}){% if usa_formulario_empresas %} <span class="small text-muted fw-normal">avanzado</span>{% endif %}
                        </button>
                    </li>
                    <li class="nav-item">
                        <button class="nav-link fw-semibold" data-bs-toggle="tab" data-bs-target="#urls-tab-google" type="button">
                            <i class="bi bi-google"></i> Google ({{ config_google|length }})
                        </button>
                    </li>
                    {% if es_admin or es_beta %}
                    <li class="nav-item">
                        <button class="nav-link fw-semibold" data-bs-toggle="tab" data-bs-target="#urls-tab-productos" type="button">
                            <i class="bi bi-box-seam"></i> Productos ({{ lista_productos|length - 1 }}) <span class="badge bg-warning text-dark">BETA</span>
                        </button>
                    </li>
                    {% endif %}
                </ul>
                <div class="tab-content">
                {% if usa_formulario_empresas %}
                <div class="tab-pane fade show active" id="urls-tab-empresas">
                    <div id="empresasLista">
                        <div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-2">
                            <span class="small text-muted">Empresas que vigilan los bots. Agrega o edita sin escribir el formato a mano.</span>
                            <button type="button" class="btn btn-sm btn-primary" id="empresaNueva"><i class="bi bi-plus-lg"></i> Agregar empresa</button>
                        </div>
                        <div class="table-responsive">
                            <table class="table table-sm table-hover align-middle mb-0">
                                <thead class="table-light"><tr class="small text-muted"><th>Empresa</th><th>Meta</th><th>Google</th><th class="text-end">Acciones</th></tr></thead>
                                <tbody>
                                    {% for e in empresas_config %}
                                    <tr>
                                        <td class="fw-semibold">{{ e.nombre }}</td>
                                        <td class="small">
                                            {% for m in e.meta %}<a href="{{ m.url }}" target="_blank" rel="noopener" class="badge text-decoration-none {{ 'bg-success-subtle text-success-emphasis' if m.tipo == 'pagina' else 'bg-warning-subtle text-warning-emphasis' }} me-1" title="{{ m.url }}">{{ 'Página' if m.tipo == 'pagina' else 'Búsqueda: ' ~ m.bot }}</a>{% else %}<span class="text-muted">—</span>{% endfor %}
                                        </td>
                                        <td class="small">{% for g in e.google %}<span class="badge bg-info-subtle text-info-emphasis me-1">{{ g }}</span>{% else %}<span class="text-muted">—</span>{% endfor %}</td>
                                        <td class="text-end text-nowrap">
                                            <button type="button" class="btn btn-sm btn-outline-primary py-0 px-2 empresa-editar" data-indice="{{ loop.index0 }}" title="Editar"><i class="bi bi-pencil"></i></button>
                                            <button type="button" class="btn btn-sm btn-outline-danger py-0 px-2 empresa-eliminar" data-nombre="{{ e.nombre }}" title="Dejar de monitorear"><i class="bi bi-trash3"></i></button>
                                        </td>
                                    </tr>
                                    {% else %}
                                    <tr><td colspan="4" class="text-center text-muted py-4 small">Todavía no hay empresas configuradas.</td></tr>
                                    {% endfor %}
                                </tbody>
                            </table>
                        </div>
                        <p class="small text-muted mt-2 mb-0"><span class="badge bg-success-subtle text-success-emphasis">Página</span> = búsqueda por página del anunciante (la más precisa). <span class="badge bg-warning-subtle text-warning-emphasis">Búsqueda</span> = por palabra clave (puede traer anuncios de otros).</p>
                    </div>

                    <form id="empresaFormulario" class="d-none" autocomplete="off">
                        <h6 class="fw-bold mb-3" id="empresaTitulo">Agregar empresa</h6>
                        <div class="mb-3">
                            <label class="form-label small fw-semibold" for="empresaNombre">Nombre en el panel</label>
                            <input type="text" class="form-control form-control-sm" id="empresaNombre" maxlength="60" placeholder="Ej.: Fina" required>
                            <div class="form-text">Así aparecerá en el dashboard. Úsalo igual en Meta y Google para que se agrupen.</div>
                        </div>
                        <div class="mb-3">
                            <label class="form-label small fw-semibold"><i class="bi bi-meta"></i> Páginas de Meta (Facebook / Instagram)</label>
                            <div id="empresaMeta" class="d-flex flex-column gap-2"></div>
                            <button type="button" class="btn btn-link btn-sm px-0" id="empresaMasMeta"><i class="bi bi-plus"></i> Agregar otra página</button>
                            <details class="small text-muted">
                                <summary>¿Cómo consigo el enlace de la página?</summary>
                                <ol class="mb-0 mt-1 ps-3">
                                    <li>Abre la <a href="https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=VE&media_type=all" target="_blank" rel="noopener">Biblioteca de Anuncios de Meta</a>.</li>
                                    <li>Escribe el nombre de la empresa y elígela en la lista (con su logo), no la búsqueda por palabra.</li>
                                    <li>Copia el enlace de la barra del navegador (debe tener <code>view_all_page_id</code>) y pégalo aquí.</li>
                                </ol>
                                <div class="mt-1">Si no tiene página, escribe solo el nombre y se buscará por palabra clave.</div>
                            </details>
                        </div>
                        <div class="mb-3">
                            <label class="form-label small fw-semibold"><i class="bi bi-google"></i> Sitios web para Google</label>
                            <div id="empresaGoogle" class="d-flex flex-column gap-2"></div>
                            <button type="button" class="btn btn-link btn-sm px-0" id="empresaMasGoogle"><i class="bi bi-plus"></i> Agregar otro sitio</button>
                            <div class="form-text">Pega la dirección de su web (sirve cualquier página del sitio); se guarda solo el dominio, por ejemplo <code>galac.com</code>.</div>
                        </div>
                        <div class="alert alert-danger small py-2 d-none" id="empresaError"></div>
                        <div class="d-flex justify-content-end gap-2">
                            <button type="button" class="btn btn-sm btn-outline-secondary" id="empresaCancelar">Cancelar</button>
                            <button type="submit" class="btn btn-sm btn-primary" id="empresaGuardar"><i class="bi bi-cloud-arrow-up"></i> Guardar</button>
                        </div>
                    </form>
                </div>
                {% endif %}
                <div class="tab-pane fade{% if not usa_formulario_empresas %} show active{% endif %}" id="urls-tab-meta">
                <form action="/guardar_urls_txt" method="POST">
                    <div class="d-flex justify-content-between align-items-center mb-2">
                        <span class="small text-muted">Formato: <code>Nombre en Panel | Nombre Búsqueda Bot | URL</code><br>Recomendado: URL de la página del anunciante (con <code>view_all_page_id</code>), así los anuncios retirados se detectan con precisión.</span>
                        <span class="badge bg-secondary-subtle text-secondary">{{ config_urls|length }} enlaces detectados</span>
                    </div>
                    
                    <textarea name="raw_urls" class="form-control form-control-sm font-monospace mb-3 bg-dark text-light border-secondary" rows="10" placeholder="Nombre en Panel | Nombre en Meta | https://www.facebook.com/ads/library/?..." {% if not puede_editar_urls %}readonly{% endif %}>{{ raw_urls_content }}</textarea>
                    
                    <div class="d-flex justify-content-between align-items-center">
                        <small class="text-secondary"><i class="bi bi-github"></i> Se sincronizará directamente con el archivo <code>urls.txt</code> de tu repositorio.</small>
                        {% if puede_editar_urls %}
                        <button type="submit" class="btn btn-sm btn-primary px-3"><i class="bi bi-cloud-arrow-up"></i> Guardar en GitHub</button>
                        {% else %}
                        <span class="small text-warning"><i class="bi bi-lock-fill"></i> Solo el Administrador y Funciones Beta pueden modificar las URLs</span>
                        {% endif %}
                    </div>
                </form>

                <h6 class="fw-bold mt-4 mb-2 small text-uppercase text-muted">Vista Previa de Configuración</h6>
                <div class="table-responsive" style="max-height: 200px; overflow-y: auto;">
                    <table class="table table-sm table-hover align-middle mb-0">
                        <thead class="table-light">
                            <tr class="small text-muted">
                                <th>#</th>
                                <th>Nombre en Panel</th>
                                <th>Búsqueda Bot</th>
                                <th>URL de Meta Ads</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for u in config_urls %}
                            <tr>
                                <td class="text-muted small">{{ loop.index }}</td>
                                <td class="fw-bold text-primary">{{ u.nombre_flask }}</td>
                                <td class="fw-semibold text-secondary">{{ u.nombre_bot }}</td>
                                <td class="small text-truncate" style="max-width: 320px;">
                                    <a href="{{ u.url }}" target="_blank" class="text-decoration-none text-info">{{ u.url }}</a>
                                </td>
                            </tr>
                            {% else %}
                            <tr>
                                <td colspan="4" class="text-center py-3 text-muted small">No hay URLs configuradas en urls.txt.</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
                </div>

                {% if es_admin or es_beta %}
                <div class="tab-pane fade" id="urls-tab-productos">
                <form action="/guardar_productos" method="POST">
                    <div class="d-flex justify-content-between align-items-center mb-2">
                        <span class="small text-muted">Formato: <code>Categoría | palabra1, palabra2, prefijo*</code></span>
                        <span class="badge bg-secondary-subtle text-secondary">{{ lista_productos|length - 1 }} categorías</span>
                    </div>
                    <textarea name="raw_productos" class="form-control form-control-sm font-monospace mb-2 bg-dark text-light border-secondary" rows="10" {% if not puede_editar_urls %}readonly{% endif %}>{{ raw_productos_content }}</textarea>
                    <p class="small text-muted mb-3">Cada anuncio se clasifica según las palabras de su texto (sin importar mayúsculas ni tildes); puede tener varias categorías. Con <code>*</code> al final se incluyen variantes: <code>factur*</code> = factura, facturación, facturar. Los anuncios de Google no traen texto, por eso quedan "Sin clasificar".</p>
                    <div class="d-flex justify-content-between align-items-center">
                        <small class="text-secondary"><i class="bi bi-github"></i> Se sincronizará con el archivo <code>productos.txt</code> de tu repositorio.</small>
                        {% if puede_editar_urls %}
                        <button type="submit" class="btn btn-sm btn-primary px-3"><i class="bi bi-cloud-arrow-up"></i> Guardar en GitHub</button>
                        {% else %}
                        <span class="small text-warning"><i class="bi bi-lock-fill"></i> Solo el Administrador y Funciones Beta pueden modificar las categorías</span>
                        {% endif %}
                    </div>
                </form>
                </div>
                {% endif %}

                <div class="tab-pane fade" id="urls-tab-google">
                <form action="/guardar_urls_google" method="POST">
                    <div class="d-flex justify-content-between align-items-center mb-2">
                        <span class="small text-muted">Formato: <code>Nombre en Panel | dominio.com</code></span>
                        <span class="badge bg-secondary-subtle text-secondary">{{ config_google|length }} dominios detectados</span>
                    </div>

                    <textarea name="raw_urls" class="form-control form-control-sm font-monospace mb-2 bg-dark text-light border-secondary" rows="8" placeholder="Nombre en Panel | galac.com" {% if not puede_editar_urls %}readonly{% endif %}>{{ raw_google_content }}</textarea>
                    <p class="small text-muted mb-3">Usa el mismo "Nombre en Panel" que en la pestaña Meta para que los anuncios de ambas fuentes se agrupen en la misma empresa. Se buscan los anuncios mostrados en Venezuela en el Centro de Transparencia de Anuncios de Google.</p>

                    <div class="d-flex justify-content-between align-items-center">
                        <small class="text-secondary"><i class="bi bi-github"></i> Se sincronizará con el archivo <code>urls_google.txt</code> de tu repositorio.</small>
                        {% if puede_editar_urls %}
                        <button type="submit" class="btn btn-sm btn-primary px-3"><i class="bi bi-cloud-arrow-up"></i> Guardar en GitHub</button>
                        {% else %}
                        <span class="small text-warning"><i class="bi bi-lock-fill"></i> Solo el Administrador y Funciones Beta pueden modificar las URLs</span>
                        {% endif %}
                    </div>
                </form>

                <h6 class="fw-bold mt-4 mb-2 small text-uppercase text-muted">Vista Previa de Configuración</h6>
                <div class="table-responsive" style="max-height: 200px; overflow-y: auto;">
                    <table class="table table-sm table-hover align-middle mb-0">
                        <thead class="table-light">
                            <tr class="small text-muted">
                                <th>#</th>
                                <th>Nombre en Panel</th>
                                <th>Dominio</th>
                                <th>Centro de Transparencia</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for g in config_google %}
                            <tr>
                                <td class="text-muted small">{{ loop.index }}</td>
                                <td class="fw-bold text-primary">{{ g.nombre_flask }}</td>
                                <td class="fw-semibold text-secondary">{{ g.dominio }}</td>
                                <td class="small">
                                    <a href="https://adstransparency.google.com/?region=VE&domain={{ g.dominio|urlencode }}" target="_blank" class="text-decoration-none text-info">Ver en Google <i class="bi bi-box-arrow-up-right"></i></a>
                                </td>
                            </tr>
                            {% else %}
                            <tr>
                                <td colspan="4" class="text-center py-3 text-muted small">No hay dominios configurados en urls_google.txt.</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
                </div>
                </div>
            </div>
        </div>
    </div>
</div>

<!-- Modal: Gestión de Compañías Bloqueadas -->
<div class="modal fade" id="modalBlockedCompanies" tabindex="-1">
    <div class="modal-dialog modal-dialog-centered">
        <div class="modal-content card-custom">
            <div class="modal-header border-secondary border-opacity-25">
                <h5 class="modal-title fw-bold text-danger"><i class="bi bi-slash-circle"></i> Compañías Bloqueadas</h5>
                <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
            </div>
            <div class="modal-body p-4">
                <p class="small text-muted mb-3">Las empresas en esta lista quedan ocultas en todas las gráficas, filtros y métricas.</p>
                <div class="table-responsive" style="max-height: 280px; overflow-y: auto;">
                    <table class="table table-sm table-hover align-middle mb-0">
                        <thead class="table-light">
                            <tr class="small text-muted">
                                <th>Compañía</th>
                                <th class="text-end">Desbloquear</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for b in companias_bloqueadas %}
                            <tr>
                                <td class="fw-bold text-danger">{{ b }}</td>
                                <td class="text-end">
                                    <form action="/desbloquear_compania" method="POST" class="d-inline">
                                        <input type="hidden" name="compania" value="{{ b }}">
                                        <button type="submit" class="btn btn-sm btn-outline-success py-0 px-2">
                                            <i class="bi bi-unlock"></i> Desbloquear
                                        </button>
                                    </form>
                                </td>
                            </tr>
                            {% else %}
                            <tr>
                                <td colspan="2" class="text-center py-4 text-muted small">No hay ninguna compañía bloqueada.</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>
</div>

<div class="container-fluid px-4 py-4">

    {% if msg %}
    <div class="alert alert-info alert-dismissible fade show border-0 shadow-sm" role="alert">
        {{ msg }}
        <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
    </div>
    {% endif %}

    <!-- KPIs -->
    <div class="row g-3 mb-4 kpi-row">
        <div class="col-6 col-md-4 col-xl">
            <div class="card-custom p-3 h-100">
                <div class="d-flex justify-content-between align-items-center">
                    <span class="stat-label">Total Anuncios</span>
                    <i class="bi bi-collection-play text-primary fs-5"></i>
                </div>
                <div class="stat-value">{{ total_anuncios }}</div>
            </div>
        </div>
        <div class="col-6 col-md-4 col-xl">
            <div class="card-custom p-3 h-100">
                <div class="d-flex justify-content-between align-items-center">
                    <span class="stat-label">Winning Ads</span>
                    <i class="bi bi-fire text-danger fs-5"></i>
                </div>
                <div class="stat-value text-danger">{{ total_winning }}</div>
            </div>
        </div>
        <div class="col-6 col-md-4 col-xl">
            <div class="card-custom p-3 h-100">
                <div class="d-flex justify-content-between align-items-center">
                    <span class="stat-label">Videos</span>
                    <i class="bi bi-camera-video text-warning fs-5"></i>
                </div>
                <div class="stat-value text-warning">{{ total_videos }}</div>
            </div>
        </div>
        <div class="col-6 col-md-4 col-xl">
            <div class="card-custom p-3 h-100">
                <div class="d-flex justify-content-between align-items-center">
                    <span class="stat-label">Retirados / Inactivos</span>
                    <i class="bi bi-eye-slash text-danger fs-5"></i>
                </div>
                <div class="stat-value text-danger">{{ total_retirados }}</div>
            </div>
        </div>
        <div class="col-6 col-md-4 col-xl">
            <div class="card-custom p-3 h-100">
                <div class="d-flex justify-content-between align-items-center">
                    <span class="stat-label">Nuevos (48h)</span>
                    <i class="bi bi-stars text-info fs-5"></i>
                </div>
                <div class="stat-value text-info" id="kpiNuevosCount">{{ total_nuevos }}</div>
            </div>
        </div>
    </div>

    {% set pestanas_validas = ['tab-charts', 'tab-ads', 'tab-winning', 'tab-retirados', 'tab-new', 'tab-keywords'] %}
    {% set pestana_activa = request.args.get('pestana') if es_beta and request.args.get('pestana') in pestanas_validas else 'tab-charts' %}
    {# Miniatura del anuncio (Beta): clic = imagen completa en una ventana; se usa en todas las tablas de anuncios. #}
    {% macro celda_miniatura(ad) %}
    <td class="celda-miniatura">
        {% if ad.imagen_url %}
        <button type="button" class="miniatura border-0 p-0" title="Ver imagen completa"
                data-imagen="{{ ad.imagen_url }}" data-enlace="{{ ad.link_individual }}"
                data-compania="{{ ad.compania or '' }}" data-texto="{{ (ad.texto or ad.titulo or '')[:300] }}">
            <img src="{{ ad.imagen_url }}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="sinImagen(this)">
        </button>
        {% else %}
        <span class="miniatura sin-imagen" title="Sin vista previa todavía"><i class="bi bi-image"></i></span>
        {% endif %}
    </td>
    {% endmacro %}
    <!-- Panel de Filtros -->
    <div class="card-custom p-3 mb-4 card-filter-container">
        <form method="GET" action="/" id="filterForm" class="row g-2 align-items-end">
            {% if es_beta %}<input type="hidden" name="pestana" id="pestanaActual" value="{{ pestana_activa }}">{% endif %}
            <!-- 1. Buscar -->
            <div class="col-md-3 col-lg-2">
                <label class="form-label small fw-semibold text-muted mb-1"><i class="bi bi-search"></i> Buscar</label>
                <input type="text" name="q" class="form-control form-control-sm" placeholder="Texto, link..." value="{{ request.args.get('q', '') }}">
            </div>

            <!-- 2. Selección Múltiple Compañías -->
            <div class="col-md-3 col-lg-2">
                {% set todas_companias = es_beta and lista_companias and lista_companias|reject('in', companias_sel)|list|length == 0 %}
                <label class="form-label small fw-semibold text-muted mb-1"><i class="bi bi-building"></i> Compañías ({% if companias_sel and not todas_companias %}{{ companias_sel|length }}{% else %}Todas{% endif %})</label>
                <div class="dropdown">
                    <button class="form-select form-select-sm text-start d-flex justify-content-between align-items-center" type="button" data-bs-toggle="dropdown" data-bs-auto-close="outside">
                        <span class="text-truncate">
                            {% if companias_sel and not todas_companias %}
                                {{ companias_sel|join(', ') }}
                            {% else %}
                                Todas ({{ lista_companias|length }})
                            {% endif %}
                        </span>
                    </button>
                    <div class="dropdown-menu dropdown-menu-scroll p-2 w-100 shadow-lg">
                        <div class="form-check pb-1 mb-1 border-bottom">
                            {% if es_beta %}
                            <input class="form-check-input" type="checkbox" id="selectAllCompanies" {% if not companias_sel or todas_companias %}checked{% endif %}>
                            <label class="form-check-label small fw-bold" for="selectAllCompanies">Todas</label>
                            {% else %}
                            <input class="form-check-input" type="checkbox" id="selectAllCompanies" onchange="toggleAllCompanies(this)">
                            <label class="form-check-label small fw-bold" for="selectAllCompanies">Seleccionar Todo</label>
                            {% endif %}
                        </div>
                        {% for comp in lista_companias %}
                        <div class="form-check">
                            <input class="form-check-input comp-checkbox" type="checkbox" name="compania" value="{{ comp }}" id="comp_{{ loop.index }}" {% if comp in companias_sel and not todas_companias %}checked{% endif %}>
                            <label class="form-check-label small" for="comp_{{ loop.index }}">{{ comp }}</label>
                        </div>
                        {% endfor %}
                    </div>
                </div>
            </div>

            <!-- 3. Filtro Tiempo / Fecha -->
            <div class="col-6 col-md-2 col-lg-1">
                <label class="form-label small fw-semibold text-muted mb-1"><i class="bi bi-calendar-range"></i> Tiempo</label>
                <select name="tiempo" class="form-select form-select-sm">
                    <option value="todo" {% if request.args.get('tiempo', 'todo') == 'todo' %}selected{% endif %}>Todo</option>
                    <option value="7d" {% if request.args.get('tiempo') == '7d' %}selected{% endif %}>7 días</option>
                    <option value="15d" {% if request.args.get('tiempo') == '15d' %}selected{% endif %}>15 días</option>
                    <option value="30d" {% if request.args.get('tiempo') == '30d' %}selected{% endif %}>30 días</option>
                    <option value="90d" {% if request.args.get('tiempo') == '90d' %}selected{% endif %}>90 días</option>
                </select>
            </div>

            <!-- 4. Filtro Duración del Video -->
            <div class="col-6 col-md-2 col-lg-1">
                <label class="form-label small fw-semibold text-muted mb-1"><i class="bi bi-stopwatch"></i> Duración</label>
                <select name="duracion" class="form-select form-select-sm">
                    <option value="todas" {% if request.args.get('duracion', 'todas') == 'todas' %}selected{% endif %}>Todas</option>
                    <option value="corta" {% if request.args.get('duracion') == 'corta' %}selected{% endif %}>&lt; 15s</option>
                    <option value="media" {% if request.args.get('duracion') == 'media' %}selected{% endif %}>15s - 60s</option>
                    <option value="larga" {% if request.args.get('duracion') == 'larga' %}selected{% endif %}>&gt; 60s</option>
                    <option value="sin_video" {% if request.args.get('duracion') == 'sin_video' %}selected{% endif %}>Estático</option>
                </select>
            </div>

            <!-- 5. Filtro Estado -->
            <div class="col-6 col-md-2 col-lg-1">
                <label class="form-label small fw-semibold text-muted mb-1"><i class="bi bi-toggle-on"></i> Estado</label>
                <select name="estado" class="form-select form-select-sm">
                    <option value="">Todos</option>
                    <option value="Activo" {% if request.args.get('estado') == 'Activo' %}selected{% endif %}>Activo</option>
                    <option value="Inactivo" {% if request.args.get('estado') == 'Inactivo' %}selected{% endif %}>Inactivo</option>
                </select>
            </div>

            <!-- 6. Filtro Plataforma -->
            <div class="col-6 col-md-3 col-lg-2">
                <label class="form-label small fw-semibold text-muted mb-1"><i class="bi bi-share"></i> Plataforma</label>
                <select name="plataforma" class="form-select form-select-sm">
                    <option value="">Todas</option>
                    <optgroup label="Meta">
                        <option value="facebook" {% if request.args.get('plataforma') == 'facebook' %}selected{% endif %}>Facebook</option>
                        <option value="instagram" {% if request.args.get('plataforma') == 'instagram' %}selected{% endif %}>Instagram</option>
                        <option value="threads" {% if request.args.get('plataforma') == 'threads' %}selected{% endif %}>Threads</option>
                        <option value="messenger" {% if request.args.get('plataforma') == 'messenger' %}selected{% endif %}>Messenger</option>
                        <option value="audience" {% if request.args.get('plataforma') == 'audience' %}selected{% endif %}>Audience Net.</option>
                        <option value="whatsapp" {% if request.args.get('plataforma') == 'whatsapp' %}selected{% endif %}>WhatsApp</option>
                    </optgroup>
                    <optgroup label="Google">
                        <option value="búsqueda de google" {% if request.args.get('plataforma') == 'búsqueda de google' %}selected{% endif %}>Búsqueda</option>
                        <option value="youtube" {% if request.args.get('plataforma') == 'youtube' %}selected{% endif %}>YouTube</option>
                        <option value="red de display" {% if request.args.get('plataforma') == 'red de display' %}selected{% endif %}>Red de Display</option>
                        <option value="google maps" {% if request.args.get('plataforma') == 'google maps' %}selected{% endif %}>Maps</option>
                        <option value="google play" {% if request.args.get('plataforma') == 'google play' %}selected{% endif %}>Play</option>
                        <option value="google shopping" {% if request.args.get('plataforma') == 'google shopping' %}selected{% endif %}>Shopping</option>
                    </optgroup>
                </select>
            </div>

            <!-- 7. Filtro Formato -->
            <div class="col-6 col-md-3 col-lg-1">
                <label class="form-label small fw-semibold text-muted mb-1"><i class="bi bi-play-circle"></i> Formato</label>
                <select name="formato" class="form-select form-select-sm">
                    <option value="">Todos</option>
                    <option value="video" {% if request.args.get('formato') == 'video' %}selected{% endif %}>Video</option>
                    <option value="imagen" {% if request.args.get('formato') == 'imagen' %}selected{% endif %}>Imagen</option>
                    <option value="texto" {% if request.args.get('formato') == 'texto' %}selected{% endif %}>Texto</option>
                    <option value="carrusel" {% if request.args.get('formato') == 'carrusel' %}selected{% endif %}>Carrusel</option>
                    <option value="dinámico" {% if request.args.get('formato') == 'dinámico' %}selected{% endif %}>Dinámico</option>
                </select>
            </div>

            <div class="col-6 col-md-3 col-lg-1">
                <label class="form-label small fw-semibold text-muted mb-1"><i class="bi bi-diagram-3"></i> Fuente</label>
                <select name="fuente" class="form-select form-select-sm">
                    <option value="">Todas</option>
                    <option value="Meta" {% if request.args.get('fuente') == 'Meta' %}selected{% endif %}>Meta</option>
                    <option value="Google" {% if request.args.get('fuente') == 'Google' %}selected{% endif %}>Google</option>
                </select>
            </div>

            {% if es_beta %}
            <div class="col-6 col-md-3 col-lg-2">
                <label class="form-label small fw-semibold text-muted mb-1"><i class="bi bi-box-seam"></i> Producto <span class="badge bg-warning text-dark">BETA</span></label>
                <div class="dropdown">
                    <button class="form-select form-select-sm text-start d-flex justify-content-between align-items-center" type="button" data-bs-toggle="dropdown" data-bs-auto-close="outside">
                        {% set todos_productos = lista_productos|reject('in', productos_sel)|list|length == 0 %}
                        <span class="text-truncate">{% if productos_sel and not todos_productos %}{{ productos_sel|join(', ') }}{% else %}Todos ({{ lista_productos|length }}){% endif %}</span>
                    </button>
                    <div class="dropdown-menu dropdown-menu-scroll p-2 w-100 shadow-lg" style="min-width: 240px;">
                        <div class="form-check pb-1 mb-1 border-bottom">
                            <input class="form-check-input" type="checkbox" id="selectAllProductos" {% if not productos_sel or todos_productos %}checked{% endif %}>
                            <label class="form-check-label small fw-bold" for="selectAllProductos">Todos</label>
                        </div>
                        {% for p in lista_productos %}
                        <div class="form-check">
                            <input class="form-check-input prod-checkbox" type="checkbox" name="producto" value="{{ p }}" id="prod_{{ loop.index }}" {% if p in productos_sel and not todos_productos %}checked{% endif %}>
                            <label class="form-check-label small" for="prod_{{ loop.index }}">{{ p }}</label>
                        </div>
                        {% endfor %}
                    </div>
                </div>
            </div>
            {% endif %}

            <!-- 8. Botones de Acción -->
            <div class="col-12 {{ 'col-lg-auto' if es_beta else 'col-lg-2' }} d-flex gap-1">
                {% if es_beta %}
                <span id="estadoFiltros" class="small text-muted d-flex align-items-center justify-content-center gap-1 flex-grow-1 px-1 text-nowrap" title="Los filtros se aplican solos al cambiarlos">
                    <i class="bi bi-lightning-charge"></i> Automáticos
                </span>
                {% else %}
                <button type="submit" class="btn btn-sm btn-primary w-100"><i class="bi bi-funnel"></i> Filtrar</button>
                {% endif %}
                {% if es_beta %}
                <a href="/?pestana={{ pestana_activa }}" id="limpiarFiltros" class="btn btn-sm btn-outline-secondary" title="Limpiar filtros"><i class="bi bi-trash3"></i></a>
                <a href="/descargar_excel?{{ request.query_string.decode() }}" class="btn btn-sm btn-success text-nowrap d-flex align-items-center gap-1" title="Descargar los anuncios filtrados en Excel">
                    <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><rect x="1" y="1" width="14" height="14" rx="2.5" fill="#fff"/><path d="M5.3 4.6l5.4 6.8M10.7 4.6l-5.4 6.8" stroke="#107C41" stroke-width="1.9" stroke-linecap="round"/></svg>
                    Exportar a Excel
                </a>
                {% else %}
                <a href="/" class="btn btn-sm btn-outline-secondary" title="Limpiar filtros"><i class="bi bi-arrow-counterclockwise"></i></a>
                <a href="/descargar_excel?{{ request.query_string.decode() }}" class="btn btn-sm btn-success text-nowrap" title="Descargar Excel"><i class="bi bi-file-earmark-excel"></i></a>
                {% endif %}
            </div>
        </form>
    </div>

    <!-- Pestañas -->
    <ul class="nav nav-tabs mb-3" id="mainTab" role="tablist">
        <li class="nav-item">
            <button class="nav-link {% if pestana_activa == 'tab-charts' %}active {% endif %}fw-semibold" data-bs-toggle="tab" data-bs-target="#tab-charts" type="button">
                <i class="bi bi-bar-chart-line"></i> Vista General & Tendencias
            </button>
        </li>
        <li class="nav-item">
            <button class="nav-link {% if pestana_activa == 'tab-ads' %}active {% endif %}fw-semibold" data-bs-toggle="tab" data-bs-target="#tab-ads" type="button">
                <i class="bi bi-list-columns"></i> Detalle de Anuncios ({{ anuncios|length }})
            </button>
        </li>
        <li class="nav-item">
            <button class="nav-link {% if pestana_activa == 'tab-winning' %}active {% endif %}fw-semibold position-relative text-danger" data-bs-toggle="tab" data-bs-target="#tab-winning" type="button">
                <i class="bi bi-fire"></i> Winning Ads
                {% if total_winning > 0 %}
                <span class="badge rounded-pill bg-danger ms-1">{{ total_winning }}</span>
                {% endif %}
            </button>
        </li>
        <li class="nav-item">
            <button class="nav-link {% if pestana_activa == 'tab-retirados' %}active {% endif %}fw-semibold position-relative text-danger" data-bs-toggle="tab" data-bs-target="#tab-retirados" type="button">
                {% set fuente_filtro = request.args.get('fuente', '') %}
                <i class="bi bi-eye-slash"></i> {% if es_beta %}Retirados{% if fuente_filtro in ('Meta', 'Google') %} de {{ fuente_filtro }}{% endif %}{% else %}Retirados de Meta{% endif %}
                {% if total_retirados > 0 %}
                <span class="badge rounded-pill bg-danger ms-1">{{ total_retirados }}</span>
                {% endif %}
            </button>
        </li>
        <li class="nav-item">
            <button class="nav-link {% if pestana_activa == 'tab-new' %}active {% endif %}fw-semibold position-relative text-info" data-bs-toggle="tab" data-bs-target="#tab-new" type="button">
                <i class="bi bi-stars text-info"></i> Nuevos Anuncios
                <span class="badge rounded-pill bg-info ms-1" id="tabNuevosBadge" {% if total_nuevos == 0 %}style="display:none;"{% endif %}>{{ total_nuevos }}</span>
            </button>
        </li>
        <li class="nav-item">
            <button class="nav-link {% if pestana_activa == 'tab-keywords' %}active {% endif %}fw-semibold" data-bs-toggle="tab" data-bs-target="#tab-keywords" type="button">
                <i class="bi bi-chat-square-quote"></i> Términos Frecuentes
            </button>
        </li>
    </ul>

    <div class="tab-content">
        <!-- Panel 1: Gráficas y Empresas Registradas -->
        <div class="tab-pane fade{% if pestana_activa == 'tab-charts' %} show active{% endif %}" id="tab-charts">
            <div class="row g-3 mb-4">
                <div class="col-lg-8">
                    <div class="card-custom p-3 h-100">
                        <div class="d-flex flex-wrap justify-content-between align-items-center mb-3 gap-2">
                            <h6 class="fw-bold m-0"><i class="bi bi-graph-up"></i> {{ 'Anuncios Nuevos por Empresa' if es_beta else 'Publicación de Anuncios por Empresa' }}</h6>
                            <div class="d-flex flex-wrap gap-2">
                                <div class="btn-group btn-group-sm" role="group" id="timelineModoFilter">
                                    <button type="button" class="btn btn-primary active" onclick="setTimelineModo('historico', this)">{{ 'Todos' if es_beta else 'Históricos' }}</button>
                                    <button type="button" class="btn btn-outline-secondary" onclick="setTimelineModo('actual', this)" title="Anuncios que siguen {{ 'activos' if es_beta else 'publicados' }} en Meta o Google">{{ 'Vigentes' if es_beta else 'Actuales' }}</button>
                                </div>
                                <div class="btn-group btn-group-sm" role="group" id="timeRangeFilter">
                                    <button type="button" class="btn btn-outline-secondary" onclick="filterTimeline(7, this)">7D</button>
                                    <button type="button" class="btn btn-outline-secondary" onclick="filterTimeline(30, this)">30D</button>
                                    <button type="button" class="btn btn-outline-secondary" onclick="filterTimeline(365, this)">1A</button>
                                    <button type="button" class="btn btn-primary active" onclick="filterTimeline(0, this)">Todo</button>
                                </div>
                            </div>
                        </div>
                        <div style="height: 330px; position: relative;">
                            <canvas id="timelineChart"></canvas>
                        </div>
                    </div>
                </div>
                <div class="col-lg-4">
                    <div class="card-custom p-3 h-100">
                        <div class="d-flex justify-content-between align-items-center mb-3">
                            <h6 class="fw-bold m-0"><i class="bi bi-pie-chart"></i> Distribución de Formatos</h6>
                            <span class="badge bg-secondary-subtle text-secondary small">Total: {{ total_anuncios }}</span>
                        </div>
                        <div style="height: 250px; position: relative;">
                            <canvas id="formatChart"></canvas>
                        </div>
                        <div class="row text-center mt-3 pt-2 border-top g-1 small">
                            <div class="col-3">
                                <span class="text-danger fw-bold d-block">{{ format_data.pct_videos }}%</span>
                                <span class="text-muted" style="font-size:0.75rem;">Videos ({{ format_data.videos }})</span>
                            </div>
                            <div class="col-3">
                                <span class="text-warning fw-bold d-block">{{ format_data.pct_imagenes }}%</span>
                                <span class="text-muted" style="font-size:0.75rem;">Imágenes ({{ format_data.imagenes }})</span>
                            </div>
                            <div class="col-3">
                                <span class="text-info fw-bold d-block">{{ format_data.pct_textos }}%</span>
                                <span class="text-muted" style="font-size:0.75rem;">Textos ({{ format_data.textos }})</span>
                            </div>
                            <div class="col-3">
                                <span class="text-secondary fw-bold d-block">{{ format_data.pct_otros }}%</span>
                                <span class="text-muted" style="font-size:0.75rem;">Otros ({{ format_data.otros }})</span>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            {% if es_beta %}
            <div class="row g-3 mb-4">
                <div class="col-lg-7">
                    <div class="card-custom p-3 h-100">
                        <div class="d-flex justify-content-between align-items-center mb-1">
                            <h6 class="fw-bold m-0"><i class="bi bi-activity"></i> Anuncios Activos en el Tiempo <span class="badge bg-warning text-dark">BETA</span></h6>
                        </div>
                        <p class="small text-muted mb-2">Cuántos anuncios tenía activos cada empresa en cada corrida de los bots (últimos 90 días).</p>
                        {% if historial_data.fechas|length >= 2 %}
                        <div style="height: 300px;"><canvas id="historialChart"></canvas></div>
                        {% else %}
                        <div class="text-center text-muted py-5 small"><i class="bi bi-hourglass-split fs-3 d-block mb-2"></i>El gráfico se irá llenando con cada corrida de los bots{% if historial_data.fechas %} (hay datos de {{ historial_data.fechas[0] }}){% endif %}.</div>
                        {% endif %}
                    </div>
                </div>
                <div class="col-lg-5">
                    <div class="card-custom p-3 h-100">
                        <h6 class="fw-bold mb-1"><i class="bi bi-signpost-split"></i> ¿A Dónde Llevan los Anuncios? <span class="badge bg-warning text-dark">BETA</span></h6>
                        <p class="small text-muted mb-2">Destino del botón de cada anuncio que cumple los filtros.</p>
                        {% if destinos_data.companias %}
                        <div style="height: {{ [160, 70 + 34 * destinos_data.companias|length]|max }}px;"><canvas id="destinosChart"></canvas></div>
                        {% else %}
                        <div class="text-center text-muted py-5 small"><i class="bi bi-hourglass-split fs-3 d-block mb-2"></i>El destino se registra desde la próxima corrida de los bots.</div>
                        {% endif %}
                    </div>
                </div>
            </div>
            {% endif %}

            <!-- Tabla de Empresas Registradas -->
            <div class="card-custom overflow-hidden reveal-scroll">
                <div class="p-3 bg-primary bg-opacity-10 border-bottom d-flex align-items-center justify-content-between">
                    <div>
                        <h6 class="fw-bold text-primary mb-1"><i class="bi bi-buildings"></i> Empresas Monitoreadas y Volumen de {{ 'Anuncios' if es_beta else 'Creatividades' }}</h6>
                        <p class="small text-muted mb-0">{% if es_beta %}Anuncios que cumplen los filtros seleccionados, divididos por formato para cada marca.{% else %}Total de creatividades almacenadas en el sistema divididas por formato para cada marca.{% endif %}</p>
                    </div>
                    {% set modos_empresas = [('historico', stats_empresas), ('actual', stats_empresas_vigentes)] if es_beta else [('historico', stats_empresas)] %}
                    {% for modo, lista in modos_empresas %}
                    <span class="badge bg-primary fs-6{% if modo == 'actual' %} d-none{% endif %}" data-modo-empresas="{{ modo }}">{{ lista|length }} empresa{{ 's' if lista|length != 1 }}</span>
                    {% endfor %}
                </div>
                <div class="table-responsive">
                    <table class="table table-hover align-middle mb-0">
                        <thead class="table-light">
                            <tr class="small text-muted">
                                <th>#</th>
                                <th>Compañía / Marca</th>
                                <th class="text-center">Total Videos</th>
                                <th class="text-center">Total Fotos</th>
                                <th class="text-center">Total Textos</th>
                                <th class="text-center">Total Anuncios</th>
                                <th class="text-end">Acciones</th>
                            </tr>
                        </thead>
                        {% for modo, lista in modos_empresas %}
                        <tbody data-modo-empresas="{{ modo }}"{% if modo == 'actual' %} class="d-none"{% endif %}>
                            {% for emp in lista %}
                            <tr>
                                <td class="text-muted small">{{ loop.index }}</td>
                                <td>
                                    <span class="fw-bold fs-6">{{ emp.compania }}</span>
                                </td>
                                <td class="text-center">
                                    <span class="badge bg-danger-subtle text-danger fw-bold fs-6 px-3 py-1">
                                        <i class="bi bi-camera-video me-1"></i> {{ emp.total_videos }}
                                    </span>
                                </td>
                                <td class="text-center">
                                    <span class="badge bg-warning-subtle text-warning-emphasis fw-bold fs-6 px-3 py-1">
                                        <i class="bi bi-image me-1"></i> {{ emp.total_fotos }}
                                    </span>
                                </td>
                                <td class="text-center">
                                    <span class="badge bg-info-subtle text-info-emphasis fw-bold fs-6 px-3 py-1">
                                        <i class="bi bi-fonts me-1"></i> {{ emp.total_textos }}
                                    </span>
                                </td>
                                <td class="text-center">
                                    <span class="badge bg-secondary-subtle text-secondary fw-bold fs-6 px-3 py-1">
                                        {{ emp.total_anuncios }}
                                    </span>
                                </td>
                                <td class="text-end">
                                    <a href="{% if es_beta %}{{ url_con_filtros(compania=emp.compania, pestana='tab-ads') }}{% else %}/?compania={{ emp.compania|urlencode }}{% endif %}" class="btn btn-sm btn-outline-primary py-0 px-2" style="font-size: 0.75rem;" title="Filtrar anuncios de esta empresa">
                                        <i class="bi bi-funnel"></i> {{ 'Ver Anuncios' if es_beta else 'Ver Creatividades' }}
                                    </a>
                                </td>
                            </tr>
                            {% else %}
                            <tr>
                                <td colspan="7" class="text-center py-5 text-muted">
                                    <i class="bi bi-folder-x fs-2 d-block mb-2"></i> {% if not es_beta %}No hay estadísticas de empresas disponibles.{% elif modo == 'actual' %}No hay empresas con anuncios vigentes para los filtros seleccionados.{% else %}No hay empresas con anuncios para los filtros seleccionados.{% endif %}
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                        {% endfor %}
                    </table>
                </div>
            </div>
        </div>

        <!-- Panel 2: Detalle de Anuncios -->
        <div class="tab-pane fade{% if pestana_activa == 'tab-ads' %} show active{% endif %}" id="tab-ads">
            <div class="card-custom overflow-hidden">
                <div class="table-responsive">
                    <table class="table table-hover align-middle mb-0">
                        <thead class="table-light">
                            <tr class="small text-muted">
                                {% if es_beta %}<th>Vista</th>{% endif %}
                                <th>Empresa</th>
                                <th>Estado / Desempeño</th>
                                <th>Plataformas</th>
                                <th>Formato</th>
                                <th>Copia / Texto</th>
                                <th>Tiempo Activo</th>
                                <th>Fecha de Subida</th>
                                <th>Acción</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for ad in anuncios %}
                            <tr>
                                {% if es_beta %}{{ celda_miniatura(ad) }}{% endif %}
                                <td>
                                    <div class="d-flex align-items-center gap-1">
                                        <span class="fw-bold">{{ ad.compania or 'N/A' }}</span>
                                        {% if ad.compania %}
                                        <form action="/bloquear_compania" method="POST" class="d-inline" onsubmit="return confirm('¿Bloquear la compañía {{ ad.compania }} del dashboard?');">
                                            <input type="hidden" name="compania" value="{{ ad.compania }}">
                                            <button type="submit" class="btn btn-link text-danger p-0 border-0 ms-1" title="Bloquear esta compañía">
                                                <i class="bi bi-slash-circle" style="font-size: 0.8rem;"></i>
                                            </button>
                                        </form>
                                        {% endif %}
                                    </div>
                                </td>
                                <td>
                                    <div class="d-flex flex-wrap gap-1">
                                        <span class="badge {% if ad.estado == 'Activo' %}badge-active{% else %}badge-inactive{% endif %}">
                                            {{ ad.estado or 'Desconocido' }}
                                        </span>
                                        {% if ad.es_winning %}
                                        <span class="badge badge-winning" title="Anuncio con más de 30 días continuos activo">
                                            🔥 Winning Ad
                                        </span>
                                        {% endif %}
                                    </div>
                                </td>
                                <td{% if es_beta %} style="width: 150px; max-width: 150px;"{% endif %}>{% if es_beta %}<div class="d-flex flex-wrap gap-1">{{ ad.plataformas_html|safe }}</div>{% else %}{{ ad.plataformas_html|safe }}{% endif %}</td>
                                <td>
                                    {% if 'video' in (ad.formato|string|lower) %}
                                        <span class="text-danger small fw-semibold">
                                            <i class="bi bi-camera-video"></i> Video
                                            {% if ad.duracion_segundos and ad.duracion_segundos > 0 %}
                                                ({{ ad.duracion_segundos }}s)
                                            {% endif %}
                                        </span>
                                    {% else %}
                                        {% set f = ad.formato|string|lower %}{% if 'texto' in f %}<span class="text-info small fw-semibold"><i class="bi bi-fonts"></i> Texto</span>{% elif 'carrusel' in f %}<span class="text-warning small fw-semibold"><i class="bi bi-images"></i> Carrusel</span>{% elif 'dinámico' in f %}<span class="text-warning small fw-semibold"><i class="bi bi-shuffle"></i> Dinámico</span>{% else %}<span class="text-warning small fw-semibold"><i class="bi bi-image"></i> Imagen</span>{% endif %}
                                    {% endif %}
                                    {% if es_beta and ad.destino %}
                                    <div class="mt-1"><span class="badge bg-body-secondary text-body-secondary fw-semibold" style="font-size: 0.65rem;" title="{{ ad.destino_url or '' }}"><i class="bi {{ iconos_destino.get(ad.destino, 'bi-box-arrow-up-right') }}"></i> {{ ad.destino }}</span></div>
                                    {% endif %}
                                </td>
                                {% set largo_texto = 220 if es_beta else 120 %}
                                <td class="small text-muted" style="{% if es_beta %}min-width: 320px; max-width: 440px;{% else %}max-width: 280px;{% endif %}">
                                    {% set texto_ad = ad.texto or ad.titulo or 'Sin descripción' %}
                                    {% if es_beta and texto_ad|length > largo_texto %}<span class="texto-completo" data-bs-title="{{ texto_ad }}">{{ texto_ad[:largo_texto] }}...</span>{% else %}{{ texto_ad[:largo_texto] }}{% if (ad.texto or ad.titulo or '')|length > largo_texto %}...{% endif %}{% endif %}
                                    {% if es_beta and ad.productos %}
                                    <div class="mt-1 d-flex flex-wrap gap-1">
                                        {% for p in ad.productos %}{% if p in productos_sel %}<span class="badge bg-primary text-white shadow-sm" style="font-size: 0.65rem;" title="Producto filtrado"><i class="bi bi-check-circle-fill"></i> {{ p }}</span>{% else %}<span class="badge bg-primary-subtle text-primary-emphasis{% if productos_sel %} opacity-50{% endif %}" style="font-size: 0.65rem;"><i class="bi bi-box-seam"></i> {{ p }}</span>{% endif %}{% endfor %}
                                    </div>
                                    {% endif %}
                                </td>
                                <td class="small">
                                    {% if ad.fecha_display != 'N/A' %}
                                    <span class="fw-bold {% if ad.es_winning %}text-danger{% else %}text-muted{% endif %}">
                                        {{ ad.dias_activo }} días
                                    </span>
                                    {% else %}
                                    <span class="text-muted">-</span>
                                    {% endif %}
                                </td>
                                <td class="small fw-semibold">{{ ad.fecha_display }}</td>
                                <td>
                                    {% if ad.link_individual %}
                                    <a href="{{ ad.link_individual }}" target="_blank" class="btn btn-sm btn-outline-primary py-0 px-2" style="font-size: 0.75rem;">
                                        <i class="bi bi-box-arrow-up-right"></i> Ver anuncio
                                    </a>
                                    {% else %}
                                    <span class="text-muted small">-</span>
                                    {% endif %}
                                </td>
                            </tr>
                            {% else %}
                            <tr>
                                <td colspan="{{ 9 if es_beta else 8 }}" class="text-center py-5 text-muted">
                                    <i class="bi bi-folder-x fs-2 d-block mb-2"></i> No hay registros disponibles
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Panel 3: Winning Ads -->
        <div class="tab-pane fade{% if pestana_activa == 'tab-winning' %} show active{% endif %}" id="tab-winning">
            <div class="card-custom overflow-hidden">
                <div class="p-3 bg-danger bg-opacity-10 border-bottom d-flex align-items-center justify-content-between">
                    <div>
                        <h6 class="fw-bold text-danger mb-1"><i class="bi bi-fire"></i> Anuncios de Alto Rendimiento (Activos > 30 días)</h6>
                        <p class="small text-muted mb-0">Campañas actualmente activas con más de un mes continuo en circulación.</p>
                    </div>
                    <span class="badge bg-danger fs-6">{{ anuncios_winning|length }} detectados</span>
                </div>
                <div class="table-responsive">
                    <table class="table table-hover align-middle mb-0">
                        <thead class="table-light">
                            <tr class="small text-muted">
                                {% if es_beta %}<th>Vista</th>{% endif %}
                                <th>Empresa</th>
                                <th>Insignia</th>
                                <th>Plataformas</th>
                                <th>Formato</th>
                                <th>Texto</th>
                                <th>Días Activo</th>
                                <th>Fecha de Subida</th>
                                <th>Enlace</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for ad in anuncios_winning %}
                            <tr>
                                {% if es_beta %}{{ celda_miniatura(ad) }}{% endif %}
                                <td class="fw-bold">{{ ad.compania or 'N/A' }}</td>
                                <td><span class="badge badge-winning">🔥 Winning Ad</span></td>
                                <td>{{ ad.plataformas_html|safe }}</td>
                                <td>
                                    {% if 'video' in (ad.formato|string|lower) %}
                                        <span class="text-danger small fw-semibold">
                                            <i class="bi bi-camera-video"></i> Video
                                            {% if ad.duracion_segundos and ad.duracion_segundos > 0 %}
                                                ({{ ad.duracion_segundos }}s)
                                            {% endif %}
                                        </span>
                                    {% else %}
                                        {% set f = ad.formato|string|lower %}{% if 'texto' in f %}<span class="text-info small fw-semibold"><i class="bi bi-fonts"></i> Texto</span>{% elif 'carrusel' in f %}<span class="text-warning small fw-semibold"><i class="bi bi-images"></i> Carrusel</span>{% elif 'dinámico' in f %}<span class="text-warning small fw-semibold"><i class="bi bi-shuffle"></i> Dinámico</span>{% else %}<span class="text-warning small fw-semibold"><i class="bi bi-image"></i> Imagen</span>{% endif %}
                                    {% endif %}
                                </td>
                                <td class="small text-muted" style="max-width: 300px;">
                                    {{ (ad.texto or ad.titulo or 'Sin descripción')[:140] }}
                                </td>
                                <td><span class="badge bg-danger-subtle text-danger fw-bold">{{ ad.dias_activo }} días</span></td>
                                <td class="small fw-semibold">{{ ad.fecha_display }}</td>
                                <td>
                                    {% if ad.link_individual %}
                                    <a href="{{ ad.link_individual }}" target="_blank" class="btn btn-sm btn-primary py-0 px-2" style="font-size: 0.75rem;">
                                        <i class="bi bi-box-arrow-up-right"></i> Ver Anuncio
                                    </a>
                                    {% endif %}
                                </td>
                            </tr>
                            {% else %}
                            <tr>
                                <td colspan="{{ 9 if es_beta else 8 }}" class="text-center py-5 text-muted">
                                    <i class="bi bi-shield-check fs-2 d-block mb-2 text-warning"></i> No hay campañas activas con más de 30 días en los filtros seleccionados.
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Panel 4: Retirados / Inactivos de Meta -->
        <div class="tab-pane fade{% if pestana_activa == 'tab-retirados' %} show active{% endif %}" id="tab-retirados">
            <div class="card-custom overflow-hidden">
                <div class="p-3 bg-danger bg-opacity-10 border-bottom d-flex align-items-center justify-content-between">
                    <div>
                        <h6 class="fw-bold text-danger mb-1"><i class="bi bi-eye-slash"></i> Anuncios Guardados que Ya Fueron Retirados o Apagados</h6>
                        <p class="small text-muted mb-0">Campañas que existieron en {% if not es_beta %}Meta Ads{% elif request.args.get('fuente') == 'Meta' %}Meta Ads{% elif request.args.get('fuente') == 'Google' %}Google Ads{% else %}Meta Ads o Google Ads{% endif %} pero actualmente ya no están activas ni circulando.</p>
                    </div>
                    <span class="badge bg-danger fs-6">{{ anuncios_retirados|length }} inactivos</span>
                </div>
                <div class="table-responsive">
                    <table class="table table-hover align-middle mb-0">
                        <thead class="table-light">
                            <tr class="small text-muted">
                                {% if es_beta %}<th>Vista</th>{% endif %}
                                <th>Empresa</th>
                                <th>Estado</th>
                                <th>Plataformas</th>
                                <th>Formato</th>
                                <th>Texto / Copy</th>
                                <th>Días que Estuvo Activo</th>
                                <th>Fecha Original</th>
                                <th>Enlace Histórico</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for ad in anuncios_retirados %}
                            <tr>
                                {% if es_beta %}{{ celda_miniatura(ad) }}{% endif %}
                                <td class="fw-bold">{{ ad.compania or 'N/A' }}</td>
                                <td><span class="badge badge-retirado"><i class="bi bi-x-circle me-1"></i> Retirado</span></td>
                                <td>{{ ad.plataformas_html|safe }}</td>
                                <td>
                                    {% if 'video' in (ad.formato|string|lower) %}
                                        <span class="text-danger small fw-semibold">
                                            <i class="bi bi-camera-video"></i> Video
                                            {% if ad.duracion_segundos and ad.duracion_segundos > 0 %}
                                                ({{ ad.duracion_segundos }}s)
                                            {% endif %}
                                        </span>
                                    {% else %}
                                        {% set f = ad.formato|string|lower %}{% if 'texto' in f %}<span class="text-info small fw-semibold"><i class="bi bi-fonts"></i> Texto</span>{% elif 'carrusel' in f %}<span class="text-warning small fw-semibold"><i class="bi bi-images"></i> Carrusel</span>{% elif 'dinámico' in f %}<span class="text-warning small fw-semibold"><i class="bi bi-shuffle"></i> Dinámico</span>{% else %}<span class="text-warning small fw-semibold"><i class="bi bi-image"></i> Imagen</span>{% endif %}
                                    {% endif %}
                                </td>
                                <td class="small text-muted" style="max-width: 300px;">
                                    {{ (ad.texto or ad.titulo or 'Sin descripción')[:140] }}
                                </td>
                                <td>
                                    <span class="badge bg-secondary-subtle text-secondary fw-semibold">{{ ad.dias_activo }} días aprox.</span>
                                </td>
                                <td class="small fw-semibold">{{ ad.fecha_display }}</td>
                                <td>
                                    {% if ad.link_individual %}
                                    <a href="{{ ad.link_individual }}" target="_blank" class="btn btn-sm btn-outline-secondary py-0 px-2" style="font-size: 0.75rem;">
                                        <i class="bi bi-box-arrow-up-right"></i> Ver anuncio
                                    </a>
                                    {% endif %}
                                </td>
                            </tr>
                            {% else %}
                            <tr>
                                <td colspan="{{ 9 if es_beta else 8 }}" class="text-center py-5 text-muted">
                                    <i class="bi bi-check-circle fs-2 d-block mb-2 text-success"></i> No hay anuncios retirados registrados en la base de datos para los filtros seleccionados.
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Panel 5: Nuevos Anuncios -->
        <div class="tab-pane fade{% if pestana_activa == 'tab-new' %} show active{% endif %}" id="tab-new">
            <div class="card-custom overflow-hidden">
                <div class="p-3 bg-info bg-opacity-10 border-bottom d-flex flex-wrap align-items-center justify-content-between gap-2">
                    <div>
                        <h6 class="fw-bold text-info mb-0"><i class="bi bi-stars"></i> Nuevos Anuncios Detectados (Últimas 48h)</h6>
                        <span class="small text-muted">Campañas encontradas en los sondeos más recientes</span>
                    </div>
                    {% if total_nuevos > 0 %}
                    <button type="button" class="btn btn-sm btn-outline-info d-flex align-items-center gap-1" id="btnMarcarVistos" onclick="marcarTodosVistos()">
                        <i class="bi bi-check-all fs-6"></i> Marcar todos como vistos
                    </button>
                    {% endif %}
                </div>

                <div class="table-responsive" id="nuevosContainer">
                    <table class="table table-hover align-middle mb-0" id="nuevosTable">
                        <thead class="table-light">
                            <tr class="small text-muted">
                                {% if es_beta %}<th>Vista</th>{% endif %}
                                <th>Empresa</th>
                                <th>Distintivo</th>
                                <th>Plataformas</th>
                                <th>Formato</th>
                                <th>Texto</th>
                                <th>Fecha de Subida</th>
                                <th>Enlace</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for ad in anuncios_nuevos %}
                            <tr class="fila-nuevo-ad" data-ad-id="{{ ad.id or loop.index }}">
                                {% if es_beta %}{{ celda_miniatura(ad) }}{% endif %}
                                <td class="fw-bold">{{ ad.compania or 'N/A' }}</td>
                                <td><span class="badge badge-new"><i class="bi bi-stars"></i> Nuevo</span></td>
                                <td>{{ ad.plataformas_html|safe }}</td>
                                <td>
                                    {% if 'video' in (ad.formato|string|lower) %}
                                        <span class="text-danger small fw-semibold">
                                            <i class="bi bi-camera-video"></i> Video
                                            {% if ad.duracion_segundos and ad.duracion_segundos > 0 %}
                                                ({{ ad.duracion_segundos }}s)
                                            {% endif %}
                                        </span>
                                    {% else %}
                                        {% set f = ad.formato|string|lower %}{% if 'texto' in f %}<span class="text-info small fw-semibold"><i class="bi bi-fonts"></i> Texto</span>{% elif 'carrusel' in f %}<span class="text-warning small fw-semibold"><i class="bi bi-images"></i> Carrusel</span>{% elif 'dinámico' in f %}<span class="text-warning small fw-semibold"><i class="bi bi-shuffle"></i> Dinámico</span>{% else %}<span class="text-warning small fw-semibold"><i class="bi bi-image"></i> Imagen</span>{% endif %}
                                    {% endif %}
                                </td>
                                <td class="small text-muted" style="max-width: 300px;">
                                    {{ (ad.texto or ad.titulo or 'Sin descripción')[:140] }}
                                </td>
                                <td class="small fw-semibold">{{ ad.fecha_display }}</td>
                                <td>
                                    {% if ad.link_individual %}
                                    <a href="{{ ad.link_individual }}" target="_blank" class="btn btn-sm btn-primary py-0 px-2" style="font-size: 0.75rem;">
                                        <i class="bi bi-box-arrow-up-right"></i> Ver Anuncio
                                    </a>
                                    {% endif %}
                                </td>
                            </tr>
                            {% else %}
                            <tr>
                                <td colspan="{{ 8 if es_beta else 7 }}" class="text-center py-5 text-muted">
                                    <i class="bi bi-check2-circle fs-2 d-block mb-2 text-success"></i> No hay anuncios nuevos pendientes de revisión.
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Panel 6: Términos Frecuentes -->
        <div class="tab-pane fade{% if pestana_activa == 'tab-keywords' %} show active{% endif %}" id="tab-keywords">
            <div class="row g-3">
                <div class="col-lg-7">
                    <div class="card-custom p-3">
                        <h6 class="fw-bold mb-3"><i class="bi bi-bar-chart"></i> Top {% if es_beta %}20 {% endif %}Palabras Clave más Usadas</h6>
                        <div style="height: {{ 560 if es_beta else 360 }}px;">
                            <canvas id="keywordsChart"></canvas>
                        </div>
                    </div>
                </div>
                <div class="col-lg-5">
                    <div class="card-custom p-3">
                        <h6 class="fw-bold mb-3"><i class="bi bi-tags"></i> Frecuencia de Términos</h6>
                        <div class="table-responsive" style="max-height: {{ 560 if es_beta else 360 }}px; overflow-y: auto;">
                            <table class="table table-sm table-hover align-middle">
                                <thead class="table-light">
                                    <tr class="small text-muted">
                                        <th>#</th>
                                        <th>Palabra Clave</th>
                                        <th class="text-end">Apariciones</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {% for palabra, conteo in top_palabras %}
                                    <tr>
                                        <td class="text-muted small">{{ loop.index }}</td>
                                        <td class="fw-semibold text-primary">{{ palabra }}</td>
                                        <td class="text-end fw-bold">{{ conteo }}</td>
                                    </tr>
                                    {% else %}
                                    <tr>
                                        <td colspan="3" class="text-center text-muted py-3">No hay texto suficiente para analizar</td>
                                    </tr>
                                    {% endfor %}
                                </tbody>
                            </table>
                        </div>
                    </div>
                </div>
            </div>
        </div>

    </div>
</div>

<script>
    const disenoV2 = document.body.classList.contains('diseno-v2');
    const reducirMovimiento = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (disenoV2) {
        Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
        if (reducirMovimiento) {
            Chart.defaults.animation = false;
        } else {
            Chart.defaults.animation.duration = 1100;
            Chart.defaults.animation.easing = 'easeOutQuart';
        }
    }

    function contarHasta(el) {
        const final = parseInt(el.textContent, 10);
        if (!final) return;
        const inicio = performance.now();
        const duracion = 900;
        const paso = (ahora) => {
            const p = Math.min(1, (ahora - inicio) / duracion);
            el.textContent = Math.round(final * (1 - Math.pow(1 - p, 3)));
            if (p < 1) requestAnimationFrame(paso);
        };
        el.textContent = '0';
        requestAnimationFrame(paso);
    }

    function toggleAllCompanies(source) {
        document.querySelectorAll('.comp-checkbox').forEach(cb => cb.checked = source.checked);
    }

    function getTheme() {
        return localStorage.getItem('theme') || 'light';
    }

    function updateThemeUI(theme) {
        document.documentElement.setAttribute('data-bs-theme', theme);
        localStorage.setItem('theme', theme);
        const label = document.getElementById('themeTextLabel');
        if (label) {
            label.innerHTML = theme === 'dark' 
                ? '<i class="bi bi-sun-fill text-warning me-2"></i> Modo Claro' 
                : '<i class="bi bi-moon-stars me-2"></i> Modo Oscuro';
        }
    }

    function toggleTheme() {
        const nextTheme = getTheme() === 'dark' ? 'light' : 'dark';
        updateThemeUI(nextTheme);
    }
    updateThemeUI(getTheme());

    function marcarTodosVistos() {
        const container = document.getElementById('nuevosContainer');
        const btn = document.getElementById('btnMarcarVistos');
        const kpi = document.getElementById('kpiNuevosCount');
        const tabBadge = document.getElementById('tabNuevosBadge');

        if (container) {
            container.innerHTML = `
                <div class="text-center py-5 text-muted">
                    <i class="bi bi-check2-circle fs-1 d-block mb-2 text-success"></i>
                    <h6 class="fw-bold">¡Todo al día!</h6>
                    <p class="small text-muted mb-0">Has marcado todos los anuncios nuevos como revisados.</p>
                </div>
            `;
        }

        if (btn) btn.style.display = 'none';
        if (kpi) kpi.innerText = '0';
        if (tabBadge) tabBadge.style.display = 'none';
        localStorage.setItem('todos_anuncios_vistos', 'true');
    }

    if (localStorage.getItem('todos_anuncios_vistos') === 'true') {
        const btn = document.getElementById('btnMarcarVistos');
        const kpi = document.getElementById('kpiNuevosCount');
        const tabBadge = document.getElementById('tabNuevosBadge');
        if (btn) btn.style.display = 'none';
        if (kpi) kpi.innerText = '0';
        if (tabBadge) tabBadge.style.display = 'none';
    }

    function startInlineScraping(event) {
        event.preventDefault();
        
        const btn = document.getElementById('btnSyncScraper');
        const icon = document.getElementById('syncIcon');
        const select = document.getElementById('selectDiasScraping');
        const wrapper = document.getElementById('inlineProgressWrapper');
        const bar = document.getElementById('inlineProgressBar');
        const status = document.getElementById('inlineProgressStatus');
        const etaText = document.getElementById('inlineProgressETA');

        btn.disabled = true;
        select.disabled = true;
        icon.classList.add('spinner-border', 'spinner-border-sm', 'border-0');
        wrapper.classList.remove('d-none');
        localStorage.removeItem('todos_anuncios_vistos');

        const dias = parseInt(select.value) || 30;
        let totalSeconds = dias <= 7 ? 12 : (dias <= 15 ? 18 : (dias <= 30 ? 25 : 35));
        let remainingSeconds = totalSeconds;
        let elapsed = 0;

        etaText.innerText = `${remainingSeconds}s restantes`;
        bar.style.width = '10%';

        const timer = setInterval(() => {
            elapsed += 1;
            remainingSeconds = Math.max(1, totalSeconds - elapsed);
            
            let pct = Math.min(92, Math.floor((elapsed / totalSeconds) * 100));
            bar.style.width = pct + '%';
            etaText.innerText = `${remainingSeconds}s restantes`;

            if (pct < 30) {
                status.innerText = 'Conectando con Meta Ads...';
            } else if (pct < 65) {
                status.innerText = 'Scrapeando {{ "anuncios" if es_beta else "creatividades" }}...';
            } else {
                status.innerText = 'Sincronizando Base de Datos...';
            }

            if (elapsed >= totalSeconds - 1) {
                clearInterval(timer);
                bar.style.width = '100%';
                etaText.innerText = '¡Finalizado!';
                status.innerText = 'Actualizando vista...';
                select.disabled = false;
                setTimeout(() => {
                    document.getElementById('scraperForm').submit();
                }, 600);
            }
        }, 1000);
    }

    const rawTimelineDataHistorico = {{ timeline_data_historico|tojson }};
    const rawTimelineDataActual = {{ timeline_data_actual|tojson }};
    const formatData = {{ format_data|tojson }};
    const keywordsData = {{ keywords_chart_data|tojson }};
    const historialData = {{ historial_data|tojson }};
    const destinosData = {{ destinos_data|tojson }};

    let timelineChartInstance = null;
    let timelineModo = 'historico';
    let timelineDiasActual = 0;

    function renderTimeline(labels, datasets) {
        if (!document.getElementById('timelineChart')) return;
        
        if (timelineChartInstance) {
            timelineChartInstance.destroy();
        }

        timelineChartInstance = new Chart(document.getElementById('timelineChart'), {
            type: 'line',
            data: {
                labels: labels.length > 0 ? labels : ['Sin fechas registradas'],
                datasets: datasets.length > 0 ? datasets : [{
                    label: 'Sin datos',
                    data: [0],
                    borderColor: '#94a3b8'
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                plugins: {
                    legend: { 
                        position: 'bottom', 
                        labels: { 
                            boxWidth: 12, 
                            usePointStyle: true, 
                            padding: 15 
                        } 
                    }
                },
                scales: {
                    y: { beginAtZero: true, ticks: { precision: 0 } },
                    x: { grid: { display: false } }
                }
            }
        });
    }

    function setTimelineModo(modo, btnElement) {
        timelineModo = modo;
        // Beta: la tabla de empresas también cambia entre todos los anuncios y solo los vigentes
        const bloquesEmpresas = document.querySelectorAll('[data-modo-empresas]');
        if ([...bloquesEmpresas].some(b => b.dataset.modoEmpresas === 'actual')) {
            bloquesEmpresas.forEach(b => b.classList.toggle('d-none', b.dataset.modoEmpresas !== modo));
        }
        if (btnElement) {
            document.querySelectorAll('#timelineModoFilter button').forEach(b => {
                b.classList.remove('btn-primary', 'active');
                b.classList.add('btn-outline-secondary');
            });
            btnElement.classList.remove('btn-outline-secondary');
            btnElement.classList.add('btn-primary', 'active');
        }
        filterTimeline(timelineDiasActual, null);
    }

    function filterTimeline(days, btnElement) {
        timelineDiasActual = days;
        if (btnElement) {
            document.querySelectorAll('#timeRangeFilter button').forEach(b => {
                b.classList.remove('btn-primary', 'active');
                b.classList.add('btn-outline-secondary');
            });
            btnElement.classList.remove('btn-outline-secondary');
            btnElement.classList.add('btn-primary', 'active');
        }

        const rawTimelineData = timelineModo === 'actual' ? rawTimelineDataActual : rawTimelineDataHistorico;

        if (!rawTimelineData.labels || rawTimelineData.labels.length === 0) {
            renderTimeline([], []);
            return;
        }

        if (days === 0) {
            renderTimeline(rawTimelineData.labels, rawTimelineData.datasets);
            return;
        }

        const now = new Date();
        const cutoff = new Date();
        cutoff.setDate(now.getDate() - days);
        const cutoffStr = cutoff.toISOString().slice(0, 10);

        const filteredIndices = [];
        const filteredLabels = [];

        rawTimelineData.labels.forEach((label, idx) => {
            if (label >= cutoffStr) {
                filteredIndices.push(idx);
                filteredLabels.push(label);
            }
        });

        const filteredDatasets = rawTimelineData.datasets.map(ds => {
            return {
                ...ds,
                data: filteredIndices.map(i => ds.data[i])
            };
        });

        renderTimeline(filteredLabels, filteredDatasets);
    }

    filterTimeline(0, null);

    if (document.getElementById('formatChart')) {
        const totalFmt = formatData.videos + formatData.imagenes + formatData.textos + formatData.otros;
        new Chart(document.getElementById('formatChart'), {
            type: 'doughnut',
            data: {
                labels: [
                    `Videos (${formatData.pct_videos}%)`,
                    `Imágenes (${formatData.pct_imagenes}%)`,
                    `Textos (${formatData.pct_textos}%)`,
                    `Otros (${formatData.pct_otros}%)`
                ],
                datasets: [{
                    data: [formatData.videos, formatData.imagenes, formatData.textos, formatData.otros],
                    backgroundColor: ['#ef4444', '#f59e0b', '#0ea5e9', '#64748b'],
                    borderWidth: 2
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { position: 'bottom', labels: { boxWidth: 10, padding: 12 } },
                    tooltip: {
                        callbacks: {
                            label: function(context) {
                                const val = context.raw || 0;
                                const pct = totalFmt > 0 ? ((val / totalFmt) * 100).toFixed(1) : 0;
                                return ` ${context.label.split(' (')[0]}: ${val} (${pct}%)`;
                            }
                        }
                    }
                }
            }
        });
    }

    if (document.getElementById('keywordsChart') && keywordsData.labels && keywordsData.labels.length > 0) {
        new Chart(document.getElementById('keywordsChart'), {
            type: 'bar',
            data: {
                labels: keywordsData.labels,
                datasets: [{
                    label: 'Repeticiones',
                    data: keywordsData.values,
                    backgroundColor: '#6366f1',
                    borderRadius: 6
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                indexAxis: 'y',
                plugins: { legend: { display: false } },
                scales: { x: { beginAtZero: true, ticks: { precision: 0 } } }
            }
        });
    }

    if (disenoV2 && !reducirMovimiento) {
        document.querySelectorAll('.kpi-row .stat-value').forEach(contarHasta);
    }

    const paletaEmpresas = ['#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#06b6d4', '#ec4899', '#84cc16', '#f97316', '#14b8a6', '#a855f7', '#64748b'];
    if (document.getElementById('historialChart')) {
        new Chart(document.getElementById('historialChart'), {
            type: 'line',
            data: {
                labels: historialData.fechas,
                datasets: historialData.series.slice(0, 7).map((s, i) => ({
                    label: s.label, data: s.data, tension: .3, borderWidth: 2, pointRadius: 2,
                    borderColor: paletaEmpresas[i % paletaEmpresas.length],
                    backgroundColor: paletaEmpresas[i % paletaEmpresas.length],
                })),
            },
            options: {
                responsive: true, maintainAspectRatio: false, spanGaps: true,
                interaction: { mode: 'index', intersect: false },
                plugins: { legend: { position: 'bottom', labels: { boxWidth: 10, padding: 10 } } },
                scales: { y: { beginAtZero: true, ticks: { precision: 0 } } },
            },
        });
    }
    {% if usa_formulario_empresas %}
    // Formulario de empresas: arma las líneas de urls.txt / urls_google.txt en el servidor.
    (function formularioEmpresas() {
        const empresas = {{ empresas_config|tojson }};
        const lista = document.getElementById('empresasLista');
        const form = document.getElementById('empresaFormulario');
        if (!form) return;
        const cajaMeta = document.getElementById('empresaMeta');
        const cajaGoogle = document.getElementById('empresaGoogle');
        const error = document.getElementById('empresaError');
        let original = null;

        function pista(input, nota) {
            // Indica al momento qué se detectó en lo que se pegó (el servidor valida igual al guardar).
            const v = input.value.trim();
            if (input.dataset.tipo === 'meta') {
                if (!v) { nota.textContent = ''; return; }
                const pagina = v.match(/view_all_page_id=(\\d+)/);
                if (pagina) { nota.className = 'form-text text-success'; nota.textContent = `✓ Página de Meta detectada (ID ${pagina[1]})`; }
                else if (/facebook\\.com/i.test(v) && /[?&]q=/.test(v)) { nota.className = 'form-text text-warning'; nota.textContent = 'Búsqueda por palabra clave (menos precisa que la página)'; }
                else if (/facebook\\.com|^https?:/i.test(v)) { nota.className = 'form-text text-danger'; nota.textContent = '⚠ No se ve la página en el enlace; mira "¿Cómo consigo el enlace?"'; }
                else { nota.className = 'form-text text-warning'; nota.textContent = `Se buscará por palabra clave: "${v}"`; }
            } else {
                const host = v.replace(/^[a-z]+:\\/\\//i, '').split(/[\\/?#]/)[0].replace(/^www\\./i, '').toLowerCase();
                nota.className = 'form-text ' + (/^([a-z0-9-]+\\.)+[a-z]{2,}$/.test(host) ? 'text-success' : 'text-danger');
                nota.textContent = v ? (/^([a-z0-9-]+\\.)+[a-z]{2,}$/.test(host) ? `✓ Se guardará como ${host}` : '⚠ No parece un sitio web válido') : '';
            }
        }
        function agregarCampo(caja, tipo, valor) {
            const fila = document.createElement('div');
            const grupo = document.createElement('div');
            grupo.className = 'input-group input-group-sm';
            const input = document.createElement('input');
            input.type = 'text'; input.className = 'form-control'; input.dataset.tipo = tipo; input.value = valor || '';
            input.placeholder = tipo === 'meta' ? 'Pega el enlace de su página en la Biblioteca de Anuncios, o escribe el nombre' : 'Ej.: https://www.empresa.com';
            const quitar = document.createElement('button');
            quitar.type = 'button'; quitar.className = 'btn btn-outline-secondary'; quitar.title = 'Quitar'; quitar.innerHTML = '<i class="bi bi-x-lg"></i>';
            const nota = document.createElement('div');
            nota.className = 'form-text';
            input.addEventListener('input', () => pista(input, nota));
            quitar.addEventListener('click', () => { fila.remove(); if (!caja.children.length) agregarCampo(caja, tipo); });
            grupo.append(input, quitar);
            fila.append(grupo, nota);
            caja.appendChild(fila);
            pista(input, nota);
            return input;
        }
        function abrir(empresa) {
            original = empresa ? empresa.nombre : null;
            document.getElementById('empresaTitulo').textContent = empresa ? `Editar "${empresa.nombre}"` : 'Agregar empresa';
            document.getElementById('empresaNombre').value = empresa ? empresa.nombre : '';
            cajaMeta.innerHTML = ''; cajaGoogle.innerHTML = '';
            (empresa && empresa.meta.length ? empresa.meta.map(m => m.url) : ['']).forEach(v => agregarCampo(cajaMeta, 'meta', v));
            (empresa && empresa.google.length ? empresa.google : ['']).forEach(v => agregarCampo(cajaGoogle, 'google', v));
            error.classList.add('d-none');
            lista.classList.add('d-none'); form.classList.remove('d-none');
            document.getElementById('empresaNombre').focus();
        }
        function cerrar() { form.classList.add('d-none'); lista.classList.remove('d-none'); }
        async function enviar(ruta, cuerpo, boton) {
            const textoBoton = boton.innerHTML;
            boton.disabled = true;
            boton.innerHTML = '<span class="spinner-border spinner-border-sm" role="status"></span> Guardando…';
            try {
                const r = await fetch(ruta, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(cuerpo) });
                const datos = await r.json().catch(() => ({ ok: false, error: `Error ${r.status}` }));
                if (!datos.ok) throw new Error(datos.error || 'No se pudo guardar.');
                window.location.href = '/?msg=' + encodeURIComponent(datos.mensaje);
            } catch (e) {
                boton.disabled = false; boton.innerHTML = textoBoton;
                return e.message;
            }
            return null;
        }

        document.getElementById('empresaNueva').addEventListener('click', () => abrir(null));
        document.getElementById('empresaCancelar').addEventListener('click', cerrar);
        document.getElementById('empresaMasMeta').addEventListener('click', () => agregarCampo(cajaMeta, 'meta').focus());
        document.getElementById('empresaMasGoogle').addEventListener('click', () => agregarCampo(cajaGoogle, 'google').focus());
        document.querySelectorAll('.empresa-editar').forEach(b => b.addEventListener('click', () => abrir(empresas[+b.dataset.indice])));
        document.querySelectorAll('.empresa-eliminar').forEach(b => b.addEventListener('click', async () => {
            if (!confirm(`¿Dejar de monitorear "${b.dataset.nombre}"? Sus anuncios guardados se conservan.`)) return;
            const fallo = await enviar('/empresas/eliminar', { nombre: b.dataset.nombre }, b);
            if (fallo) alert(fallo);
        }));
        form.addEventListener('submit', async ev => {
            ev.preventDefault();
            error.classList.add('d-none');
            const valores = caja => [...caja.querySelectorAll('input')].map(i => i.value.trim()).filter(Boolean);
            const fallo = await enviar('/empresas/guardar', {
                nombre: document.getElementById('empresaNombre').value, nombre_original: original,
                meta: valores(cajaMeta), google: valores(cajaGoogle),
            }, document.getElementById('empresaGuardar'));
            if (fallo) { error.textContent = fallo; error.classList.remove('d-none'); }
        });
    })();
    {% endif %}

    {% if es_beta %}
    // Comparar compañías: la selección vive en la URL (?comparar=A&comparar=B) para poder compartirla.
    (function compararCompanias() {
        const datos = {{ comparativa|tojson }};
        const selector = document.getElementById('compararSelector');
        if (!selector) return;
        const porNombre = Object.fromEntries(datos.map(d => [d.compania, d]));
        const graficos = {};
        const url = new URL(window.location.href);
        let elegidas = url.searchParams.getAll('comparar').filter(c => porNombre[c]);
        const filtradas = {{ companias_sel|tojson }}.filter(c => porNombre[c]);
        if (elegidas.length < 2) elegidas = filtradas.length >= 2 ? filtradas : datos.slice(0, 3).map(d => d.compania);

        const etiqueta = document.getElementById('compararEtiqueta');
        if (datos.length < 2) {
            etiqueta.textContent = 'Hace falta al menos 2 compañías con anuncios para los filtros actuales';
            document.getElementById('compararExportar').disabled = true;
            return;
        }
        // Mismo formato que el filtro de Compañías: "Todas" arriba y una casilla por compañía.
        function fila(id, texto, negrita) {
            const div = document.createElement('div');
            div.className = 'form-check' + (negrita ? ' pb-1 mb-1 border-bottom' : '');
            const input = document.createElement('input');
            input.type = 'checkbox'; input.className = 'form-check-input'; input.id = id;
            const label = document.createElement('label');
            label.className = 'form-check-label small' + (negrita ? ' fw-bold' : ''); label.htmlFor = id; label.textContent = texto;
            div.append(input, label);
            selector.appendChild(div);
            return input;
        }
        const todas = fila('cmp_todas', 'Todas', true);
        const casillas = datos.map((d, i) => {
            const input = fila('cmp_' + i, `${d.compania} (${d.total})`);
            input.value = d.compania;
            input.checked = elegidas.includes(d.compania);
            return input;
        });
        function actualizarEtiqueta() {
            todas.checked = casillas.every(c => c.checked);
            etiqueta.textContent = todas.checked ? `Todas (${casillas.length})`
                : (elegidas.length ? elegidas.join(', ') : 'Elegir compañías');
        }
        function cambio() {
            elegidas = casillas.filter(c => c.checked).map(c => c.value);
            actualizarEtiqueta();
            guardarEnUrl(true);
            dibujar();
        }
        todas.addEventListener('change', () => { casillas.forEach(c => { c.checked = todas.checked; }); cambio(); });
        casillas.forEach(c => c.addEventListener('change', cambio));
        actualizarEtiqueta();

        function avisar(texto) {
            const aviso = document.getElementById('compararAviso');
            aviso.textContent = texto; aviso.classList.toggle('d-none', !texto);
        }
        function pct(n, t) { return t ? Math.round(n * 100 / t) + '%' : '-'; }
        function principal(obj) {
            const [clave, n] = Object.entries(obj || {}).sort((a, b) => b[1] - a[1])[0] || [];
            return clave ? `${clave} (${n})` : '-';
        }
        function clavePrincipal(obj, ocultar) {
            const [clave] = Object.entries(obj || {}).filter(([k]) => k !== ocultar).sort((a, b) => b[1] - a[1])[0] || [];
            return clave;
        }
        const MAX_DETALLE = 40;
        function listaAnuncios(anuncios, mostrarDias) {
            // Lista compacta: miniatura (abre la imagen completa), texto y etiquetas.
            const caja = document.createElement('div');
            caja.className = 'detalle-comparar text-start';
            const cuenta = document.createElement('div');
            cuenta.className = 'small text-muted mb-1';
            cuenta.textContent = anuncios.length === 1 ? '1 anuncio' : `${anuncios.length} anuncios`;
            caja.appendChild(cuenta);
            anuncios.slice(0, MAX_DETALLE).forEach((ad, indice) => {
                const fila = document.createElement('div');
                fila.className = 'd-flex align-items-start gap-2 py-1 border-top item-detalle';
                fila.style.animationDelay = `${Math.min(indice, 10) * 35 + 80}ms`;
                if (ad.imagen) {
                    const b = document.createElement('button');
                    b.type = 'button'; b.className = 'miniatura miniatura-chica border-0 p-0'; b.title = 'Ver imagen completa';
                    Object.assign(b.dataset, { imagen: ad.imagen, enlace: ad.enlace, compania: ad.compania || '', texto: ad.texto });
                    const img = document.createElement('img');
                    img.src = ad.imagen; img.loading = 'lazy'; img.referrerPolicy = 'no-referrer'; img.alt = '';
                    img.onerror = () => sinImagen(img);
                    b.appendChild(img);
                    fila.appendChild(b);
                } else {
                    const vacio = document.createElement('span');
                    vacio.className = 'miniatura miniatura-chica sin-imagen';
                    vacio.innerHTML = '<i class="bi bi-image"></i>';
                    fila.appendChild(vacio);
                }
                const cuerpo = document.createElement('div');
                cuerpo.className = 'flex-grow-1 small';
                const texto = document.createElement('div');
                texto.className = 'text-truncate-2';
                texto.textContent = ad.texto || 'Sin texto';
                const etiquetas = document.createElement('div');
                etiquetas.className = 'd-flex flex-wrap gap-1 mt-1';
                const chips = [ad.fuente, ad.formato === 'video' ? 'Video' : ad.formato === 'foto' ? 'Foto' : ad.formato === 'texto' ? 'Texto' : null,
                               ad.winning ? '🏆 Winning' : null, ad.nuevo ? 'Nuevo' : null, ad.vigente ? null : 'Retirado',
                               mostrarDias && ad.dias != null ? `${ad.dias} días` : null].filter(Boolean);
                chips.forEach(t => {
                    const c = document.createElement('span');
                    c.className = 'badge bg-body-secondary text-body-secondary fw-semibold';
                    c.style.fontSize = '.62rem';
                    c.textContent = t;
                    etiquetas.appendChild(c);
                });
                const enlace = document.createElement('a');
                enlace.href = ad.enlace; enlace.target = '_blank'; enlace.rel = 'noopener';
                enlace.className = 'badge bg-primary-subtle text-primary-emphasis text-decoration-none';
                enlace.style.fontSize = '.62rem';
                enlace.innerHTML = '<i class="bi bi-box-arrow-up-right"></i> Ver';
                etiquetas.appendChild(enlace);
                cuerpo.append(texto, etiquetas);
                fila.appendChild(cuerpo);
                caja.appendChild(fila);
            });
            if (anuncios.length > MAX_DETALLE) {
                const mas = document.createElement('div');
                mas.className = 'small text-muted pt-1 border-top';
                mas.textContent = `y ${anuncios.length - MAX_DETALLE} más (usa "Ver Anuncios" en la tabla de empresas para verlos todos)`;
                caja.appendChild(mas);
            }
            if (!anuncios.length) cuenta.textContent = 'Sin anuncios';
            return caja;
        }
        function grafico(id, config) {
            if (graficos[id]) graficos[id].destroy();
            graficos[id] = new Chart(document.getElementById(id), config);
        }
        const color = i => paletaEmpresas[i % paletaEmpresas.length];

        function dibujar() {
            const lista = elegidas.map(c => porNombre[c]);
            const contenido = document.getElementById('compararContenido');
            if (lista.length < 2) { contenido.classList.add('d-none'); avisar('Elige al menos 2 compañías para comparar.'); return; }
            avisar(''); contenido.classList.remove('d-none');

            // Tabla: una fila por métrica, una columna por compañía; se resalta el valor más alto.
            // [nombre, valor, resaltar, número para resaltar, qué anuncios se listan al desplegar, ordenar por días]
            const filas = [
                ['Anuncios', d => d.total, true, null, () => true],
                ['Vigentes (siguen activos)', d => d.vigentes, true, null, ad => ad.vigente],
                ['Videos', d => `${d.videos} · ${pct(d.videos, d.total)}`, false, d => d.videos, ad => ad.formato === 'video'],
                ['Fotos', d => `${d.fotos} · ${pct(d.fotos, d.total)}`, false, d => d.fotos, ad => ad.formato === 'foto'],
                ['Textos', d => `${d.textos} · ${pct(d.textos, d.total)}`, false, d => d.textos, ad => ad.formato === 'texto'],
                ['En Meta / en Google', d => `${d.meta} / ${d.google}`, false, null, () => true],
                ['Winning Ads (+30 días activos)', d => d.winning, true, null, ad => ad.winning, true],
                ['Nuevos en los últimos 7 días', d => d.nuevos7, true, null, ad => ad.nuevo],
                ['Días activos en promedio', d => d.dias_promedio ?? '-', true, null, ad => ad.dias != null, true],
                ['Producto principal', d => principal(Object.fromEntries(Object.entries(d.productos).filter(([k]) => k !== 'Sin clasificar'))),
                 false, null, (ad, d) => ad.productos.includes(clavePrincipal(d.productos, 'Sin clasificar'))],
                ['Destino principal', d => principal(Object.fromEntries(Object.entries(d.destinos).filter(([k]) => k !== 'Sin dato'))),
                 false, null, (ad, d) => ad.destino === clavePrincipal(d.destinos, 'Sin dato')],
                ['Plataforma principal', d => principal(d.plataformas), false, null, (ad, d) => ad.plataformas.includes(clavePrincipal(d.plataformas))],
            ];
            const tabla = document.getElementById('compararTabla');
            tabla.innerHTML = '';
            const thead = tabla.createTHead().insertRow();
            thead.className = 'small text-muted';
            thead.insertCell().outerHTML = '<th>Métrica</th>';
            lista.forEach((d, i) => {
                const th = document.createElement('th');
                th.className = 'text-center';
                th.innerHTML = `<span class="d-inline-block rounded-circle me-1" style="width:10px;height:10px;background:${color(i)}"></span>`;
                th.append(document.createTextNode(d.compania));
                thead.appendChild(th);
            });
            const tbody = tabla.createTBody();
            filas.forEach(([nombre, valor, resaltar, numero, filtro, porDias]) => {
                const tr = tbody.insertRow();
                tr.className = 'fila-comparar';
                tr.title = 'Clic para ver los anuncios';
                tr.style.animationDelay = `${tbody.rows.length * 25}ms`;
                const td0 = tr.insertCell(); td0.className = 'small fw-semibold text-nowrap';
                td0.innerHTML = '<i class="bi bi-chevron-right me-1 flecha-comparar"></i>';
                td0.append(document.createTextNode(nombre));
                const nums = lista.map(d => Number((numero || valor)(d)) || 0);
                const max = Math.max(...nums);
                lista.forEach((d, i) => {
                    const td = tr.insertCell();
                    td.className = 'text-center small';
                    td.textContent = valor(d);
                    if ((resaltar || numero) && max > 0 && nums[i] === max && nums.filter(n => n === max).length < lista.length) {
                        td.classList.add('fw-bold', 'text-primary');
                    }
                });
                // Fila de detalle (oculta): los anuncios de cada compañía que forman esta métrica.
                let detalle = null;
                tr.addEventListener('click', () => {
                    if (detalle) {
                        // Se pliega y, al terminar la transición, se quita la fila.
                        const cerrando = detalle;
                        detalle = null;
                        tr.classList.remove('abierta');
                        const plegable = cerrando.querySelector('.desplegable');
                        const quitar = () => cerrando.remove();
                        if (reducirMovimiento || !plegable) { quitar(); return; }
                        plegable.classList.remove('abierto');
                        plegable.addEventListener('transitionend', e => { if (e.propertyName === 'grid-template-rows') quitar(); });
                        setTimeout(quitar, 500);
                        return;
                    }
                    detalle = document.createElement('tr');
                    detalle.className = 'detalle-fila';
                    const celda = document.createElement('td');
                    celda.colSpan = lista.length + 1;
                    const rejilla = document.createElement('div');
                    rejilla.className = 'rejilla-detalle';
                    rejilla.style.gridTemplateColumns = `repeat(${lista.length}, minmax(260px, 1fr))`;
                    lista.forEach((d, i) => {
                        const columna = document.createElement('div');
                        const titulo = document.createElement('div');
                        titulo.className = 'small fw-semibold mb-1';
                        titulo.innerHTML = `<span class="d-inline-block rounded-circle me-1" style="width:10px;height:10px;background:${color(i)}"></span>`;
                        titulo.append(document.createTextNode(d.compania));
                        let anuncios = d.anuncios.filter(ad => filtro(ad, d)).map(ad => ({...ad, compania: d.compania}));
                        anuncios.sort(porDias ? (a, b) => (b.dias ?? -1) - (a.dias ?? -1) : (a, b) => (b.vigente - a.vigente) || ((a.dias ?? 1e9) - (b.dias ?? 1e9)));
                        columna.append(titulo, listaAnuncios(anuncios, porDias || nombre.startsWith('Días')));
                        rejilla.appendChild(columna);
                    });
                    const plegable = document.createElement('div');
                    plegable.className = 'desplegable';
                    const interior = document.createElement('div');
                    interior.className = 'desplegable-interior';
                    interior.appendChild(rejilla);
                    plegable.appendChild(interior);
                    celda.appendChild(plegable);
                    detalle.appendChild(celda);
                    tr.after(detalle);
                    tr.classList.add('abierta');
                    // Dos cuadros de espera para que el navegador pinte el estado cerrado antes de abrir.
                    requestAnimationFrame(() => requestAnimationFrame(() => plegable.classList.add('abierto')));
                });
            });

            const opcionesBarra = (apilado, horizontal) => ({
                responsive: true, maintainAspectRatio: false, indexAxis: horizontal ? 'y' : 'x',
                plugins: { legend: { position: 'bottom', labels: { boxWidth: 10, padding: 8 } } },
                scales: { x: { stacked: apilado, beginAtZero: true, ticks: { precision: 0 } }, y: { stacked: apilado, beginAtZero: true, ticks: { precision: 0 } } },
            });
            grafico('compararFormatos', {
                type: 'bar',
                data: { labels: ['Videos', 'Fotos', 'Textos'],
                        datasets: lista.map((d, i) => ({ label: d.compania, data: [d.videos, d.fotos, d.textos], backgroundColor: color(i), borderRadius: 4 })) },
                options: opcionesBarra(false, false),
            });

            const totalesProd = {};
            lista.forEach(d => Object.entries(d.productos).forEach(([p, n]) => { if (p !== 'Sin clasificar') totalesProd[p] = (totalesProd[p] || 0) + n; }));
            const productos = Object.keys(totalesProd).sort((a, b) => totalesProd[b] - totalesProd[a]).slice(0, 8);
            grafico('compararProductos', {
                type: 'bar',
                data: { labels: productos.length ? productos : ['Sin productos clasificados'],
                        datasets: lista.map((d, i) => ({ label: d.compania, data: productos.map(p => d.productos[p] || 0), backgroundColor: color(i), borderRadius: 4 })) },
                options: opcionesBarra(false, true),
            });

            const series = (historialData.series || []).filter(s => elegidas.includes(s.label));
            const hayHistorial = (historialData.fechas || []).length >= 2 && series.length;
            document.getElementById('compararHistorialVacio').classList.toggle('d-none', !!hayHistorial);
            document.getElementById('compararHistorial').parentElement.classList.toggle('d-none', !hayHistorial);
            if (hayHistorial) {
                grafico('compararHistorial', {
                    type: 'line',
                    data: { labels: historialData.fechas,
                            datasets: lista.map((d, i) => ({ label: d.compania, data: (series.find(s => s.label === d.compania) || {data: []}).data,
                                                             borderColor: color(i), backgroundColor: color(i), tension: .3, borderWidth: 2, pointRadius: 2 })) },
                    options: { responsive: true, maintainAspectRatio: false, spanGaps: true, interaction: { mode: 'index', intersect: false },
                               plugins: { legend: { position: 'bottom', labels: { boxWidth: 10, padding: 8 } } },
                               scales: { y: { beginAtZero: true, ticks: { precision: 0 } } } },
                });
            }

            const destinos = [...new Set(lista.flatMap(d => Object.keys(d.destinos)))].sort((a, b) => (a === 'Sin dato') - (b === 'Sin dato'));
            grafico('compararDestinos', {
                type: 'bar',
                data: { labels: lista.map(d => d.compania),
                        datasets: destinos.map(dst => ({ label: dst, data: lista.map(d => d.destinos[dst] || 0), borderRadius: 4,
                                                         backgroundColor: ({'WhatsApp': '#25d366', 'Sitio web': '#3b82f6', 'Messenger': '#0ea5e9', 'Instagram (mensaje)': '#e1306c', 'Instagram (perfil)': '#f472b6', 'Formulario': '#8b5cf6', 'Facebook': '#1877f2', 'Llamada': '#f59e0b', 'Sin enlace': '#94a3b8'})[dst] || '#cbd5e1' })) },
                options: opcionesBarra(true, true),
            });

            const palabras = document.getElementById('compararPalabras');
            palabras.innerHTML = '';
            lista.forEach((d, i) => {
                const col = document.createElement('div');
                col.className = 'col-md-6 col-lg-4';
                const titulo = document.createElement('div');
                titulo.className = 'fw-semibold mb-1';
                titulo.innerHTML = `<span class="d-inline-block rounded-circle me-1" style="width:10px;height:10px;background:${color(i)}"></span>`;
                titulo.append(document.createTextNode(d.compania));
                col.appendChild(titulo);
                const caja = document.createElement('div');
                caja.className = 'd-flex flex-wrap gap-1';
                if (!d.palabras.length) caja.innerHTML = '<span class="small text-muted">Sin texto suficiente</span>';
                d.palabras.forEach(([palabra, n]) => {
                    const chip = document.createElement('span');
                    chip.className = 'badge bg-primary-subtle text-primary-emphasis';
                    chip.textContent = `${palabra} · ${n}`;
                    caja.appendChild(chip);
                });
                col.appendChild(caja);
                palabras.appendChild(col);
            });
        }

        function cargarSheetJS() {
            if (window.XLSX) return Promise.resolve();
            return new Promise((ok, falla) => {
                const script = document.createElement('script');
                script.src = 'https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.18.5/xlsx.full.min.js';
                script.onload = ok; script.onerror = () => falla(new Error('No se pudo cargar el generador de Excel'));
                document.head.appendChild(script);
            });
        }
        function tablaPorClave(lista, campo, nombreColumna, ocultar) {
            const totales = {};
            lista.forEach(d => Object.entries(d[campo] || {}).forEach(([k, n]) => { if (k !== ocultar) totales[k] = (totales[k] || 0) + n; }));
            const claves = Object.keys(totales).sort((a, b) => totales[b] - totales[a]);
            return [[nombreColumna, ...lista.map(d => d.compania)], ...claves.map(k => [k, ...lista.map(d => (d[campo] || {})[k] || 0)])];
        }
        async function exportar() {
            const lista = elegidas.map(c => porNombre[c]).filter(Boolean);
            if (lista.length < 2) { avisar('Elige al menos 2 compañías para exportar la comparación.'); return; }
            const boton = document.getElementById('compararExportar');
            const original = boton.innerHTML;
            boton.disabled = true;
            boton.innerHTML = '<span class="spinner-border spinner-border-sm" role="status"></span> Preparando…';
            try {
                await cargarSheetJS();
                const fraccion = (n, t) => t ? Math.round(n * 1000 / t) / 10 : 0;
                const sinOtros = (obj, ocultar) => Object.fromEntries(Object.entries(obj || {}).filter(([k]) => k !== ocultar));
                const resumen = [
                    ['Métrica', ...lista.map(d => d.compania)],
                    ['Anuncios', ...lista.map(d => d.total)],
                    ['Vigentes (siguen activos)', ...lista.map(d => d.vigentes)],
                    ['Videos', ...lista.map(d => d.videos)],
                    ['% Videos', ...lista.map(d => fraccion(d.videos, d.total))],
                    ['Fotos', ...lista.map(d => d.fotos)],
                    ['% Fotos', ...lista.map(d => fraccion(d.fotos, d.total))],
                    ['Textos', ...lista.map(d => d.textos)],
                    ['% Textos', ...lista.map(d => fraccion(d.textos, d.total))],
                    ['En Meta', ...lista.map(d => d.meta)],
                    ['En Google', ...lista.map(d => d.google)],
                    ['Winning Ads (+30 días activos)', ...lista.map(d => d.winning)],
                    ['Nuevos en los últimos 7 días', ...lista.map(d => d.nuevos7)],
                    ['Días activos en promedio', ...lista.map(d => d.dias_promedio ?? '')],
                    ['Producto principal', ...lista.map(d => principal(sinOtros(d.productos, 'Sin clasificar')))],
                    ['Destino principal', ...lista.map(d => principal(sinOtros(d.destinos, 'Sin dato')))],
                    ['Plataforma principal', ...lista.map(d => principal(d.plataformas))],
                ];
                const maxPalabras = Math.max(...lista.map(d => d.palabras.length), 1);
                const palabras = [lista.map(d => d.compania),
                                  ...Array.from({length: maxPalabras}, (_, i) => lista.map(d => d.palabras[i] ? `${d.palabras[i][0]} (${d.palabras[i][1]})` : ''))];
                const series = (historialData.series || []);
                const historial = [['Fecha', ...lista.map(d => d.compania)],
                                   ...(historialData.fechas || []).map((f, i) => [f, ...lista.map(d => {
                                       const s = series.find(x => x.label === d.compania); return s && s.data[i] != null ? s.data[i] : '';
                                   })])];
                const nombresFiltro = {q: 'Búsqueda', compania: 'Compañía', tiempo: 'Tiempo', duracion: 'Duración', estado: 'Estado',
                                       plataforma: 'Plataforma', formato: 'Formato', fuente: 'Fuente', producto: 'Producto'};
                const filtros = [['Filtro', 'Valor'], ['Generado', new Date().toLocaleString('es-VE')], ['Compañías comparadas', lista.map(d => d.compania).join(', ')]];
                new URL(window.location.href).searchParams.forEach((v, k) => { if (v && nombresFiltro[k]) filtros.push([nombresFiltro[k], v]); });

                const libro = XLSX.utils.book_new();
                const hoja = (filas, nombre, anchos) => {
                    const h = XLSX.utils.aoa_to_sheet(filas);
                    h['!cols'] = (anchos || filas[0].map((_, i) => i === 0 ? 30 : 18)).map(w => ({wch: w}));
                    XLSX.utils.book_append_sheet(libro, h, nombre);
                };
                hoja(resumen, 'Resumen');
                hoja(tablaPorClave(lista, 'productos', 'Producto', 'Sin clasificar'), 'Productos');
                hoja(tablaPorClave(lista, 'destinos', 'Destino'), 'Destinos');
                hoja(tablaPorClave(lista, 'plataformas', 'Plataforma'), 'Plataformas');
                hoja(palabras, 'Palabras', lista.map(() => 22));
                if (historial.length > 1) hoja(historial, 'Historial activos');
                hoja(filtros, 'Filtros', [24, 60]);
                const fecha = new Date().toISOString().slice(0, 10);
                const nombre = lista.length <= 3 ? lista.map(d => d.compania).join('_vs_') : `${lista.length}_companias`;
                XLSX.writeFile(libro, `Comparacion_${nombre.replace(/[^\\w\\u00C0-\\u017F-]+/g, '_')}_${fecha}.xlsx`);
            } catch (e) {
                avisar('No se pudo generar el Excel: ' + e.message);
            } finally {
                boton.disabled = false;
                boton.innerHTML = original;
            }
        }
        document.getElementById('compararExportar').addEventListener('click', exportar);

        // Los gráficos se dibujan al abrir la ventana (oculta, Chart.js no conoce el tamaño). Mientras está
        // abierta, la selección queda en la URL para compartirla; al recargar con ?comparar= se abre sola.
        const ventana = document.getElementById('modalComparar');
        const guardarEnUrl = (conSeleccion) => {
            const u = new URL(window.location.href);
            u.searchParams.delete('comparar');
            if (conSeleccion) elegidas.forEach(c => u.searchParams.append('comparar', c));
            history.replaceState(null, '', u);
        };
        ventana.addEventListener('shown.bs.modal', () => { dibujar(); guardarEnUrl(true); });
        ventana.addEventListener('hidden.bs.modal', () => guardarEnUrl(false));
        if (url.searchParams.getAll('comparar').length) {
            document.addEventListener('DOMContentLoaded', () => window.bootstrap && bootstrap.Modal.getOrCreateInstance(ventana).show());
        }
    })();
    {% endif %}

    if (document.getElementById('destinosChart')) {
        const coloresDestino = {
            'WhatsApp': '#25d366', 'Sitio web': '#3b82f6', 'Messenger': '#0ea5e9', 'Instagram (mensaje)': '#e1306c',
            'Instagram (perfil)': '#f472b6', 'Formulario': '#8b5cf6', 'Facebook': '#1877f2', 'Llamada': '#f59e0b',
            'Sin enlace': '#94a3b8', 'Sin dato': '#cbd5e1',
        };
        new Chart(document.getElementById('destinosChart'), {
            type: 'bar',
            data: {
                labels: destinosData.companias,
                datasets: destinosData.destinos.map(d => ({
                    label: d, data: destinosData.series[d], backgroundColor: coloresDestino[d] || '#64748b', borderRadius: 4,
                })),
            },
            options: {
                responsive: true, maintainAspectRatio: false, indexAxis: 'y', layout: { padding: { left: 6 } },
                plugins: { legend: { position: 'bottom', labels: { boxWidth: 10, padding: 8 } } },
                scales: { x: { stacked: true, beginAtZero: true, ticks: { precision: 0 } }, y: { stacked: true } },
            },
        });
    }

    {% if es_beta %}
    // Filtros automáticos (Beta): cada cambio recarga el panel con los filtros aplicados.
    // Antes de recargar se guarda el scroll y el foco para dejarlos igual después.
    (function filtrosAutomaticos() {
        const form = document.getElementById('filterForm');
        if (!form) return;
        const leer = (k) => { try { return sessionStorage.getItem(k); } catch (e) { return null; } };
        const guardar = (k, v) => { try { sessionStorage.setItem(k, v); } catch (e) {} };
        const borrar = (k) => { try { sessionStorage.removeItem(k); } catch (e) {} };
        const buscador = form.querySelector('input[name="q"]');
        let espera = null;

        form.addEventListener('submit', () => {
            clearTimeout(espera);
            guardar('filtroAuto', JSON.stringify({
                scroll: window.scrollY,
                foco: document.activeElement === buscador ? 'q' : '',
            }));
            document.body.classList.add('aplicando-filtros');
            const estado = document.getElementById('estadoFiltros');
            if (estado) estado.innerHTML = '<span class="spinner-border spinner-border-sm" role="status"></span> Aplicando…';
        });

        function aplicar() {
            if (form.requestSubmit) {
                form.requestSubmit();
            } else {
                form.dispatchEvent(new Event('submit'));
                form.submit();
            }
        }

        form.querySelectorAll('select').forEach(el => el.addEventListener('change', aplicar));

        if (buscador) {
            buscador.addEventListener('input', () => {
                clearTimeout(espera);
                espera = setTimeout(aplicar, 800);
            });
        }

        // La pestaña abierta viaja en la URL (?pestana=): filtrar, limpiar o recargar no devuelve a la principal.
        document.querySelectorAll('#mainTab [data-bs-toggle="tab"]').forEach(boton => {
            boton.addEventListener('shown.bs.tab', () => {
                const pestana = boton.dataset.bsTarget.slice(1);
                const campo = document.getElementById('pestanaActual');
                if (campo) campo.value = pestana;
                const limpiar = document.getElementById('limpiarFiltros');
                if (limpiar) limpiar.href = '/?pestana=' + pestana;
                const url = new URL(window.location.href);
                url.searchParams.set('pestana', pestana);
                history.replaceState(null, '', url);
            });
        });

        // "Todas"/"Todos" equivale a no filtrar: al marcarla se desmarcan las opciones, y queda
        // marcada sola cuando no hay ninguna opción elegida (no se puede quedar en "ninguna").
        [['#selectAllCompanies', '.comp-checkbox'], ['#selectAllProductos', '.prod-checkbox']].forEach(([todo, cada]) => {
            const maestra = document.querySelector(todo);
            const casillas = [...document.querySelectorAll(cada)];
            if (!maestra) return;
            maestra.addEventListener('change', () => {
                if (maestra.checked) casillas.forEach(c => { c.checked = false; });
                else if (!casillas.some(c => c.checked)) maestra.checked = true;
            });
            casillas.forEach(cb => cb.addEventListener('change', () => {
                maestra.checked = !casillas.some(c => c.checked);
            }));
        });

        // Compañías y Productos: se aplican al cerrar el desplegable para poder marcar varios seguidos.
        form.querySelectorAll('.dropdown').forEach(desplegable => {
            let cambiado = false;
            desplegable.querySelectorAll('input[type="checkbox"]').forEach(cb =>
                cb.addEventListener('change', () => { cambiado = true; }));
            desplegable.addEventListener('hidden.bs.dropdown', () => {
                if (cambiado) aplicar();
            });
        });

        // Clic en la miniatura: imagen completa en una ventana. Si la imagen caducó, se abre el anuncio.
        document.addEventListener('click', ev => {
            const boton = ev.target.closest('button.miniatura');
            if (!boton) return;
            if (boton.classList.contains('sin-imagen') || !window.bootstrap) {
                window.open(boton.dataset.enlace, '_blank', 'noopener');
                return;
            }
            document.getElementById('vistaPreviaImagen').src = boton.dataset.imagen;
            document.getElementById('vistaPreviaCompania').textContent = boton.dataset.compania;
            document.getElementById('vistaPreviaTexto').textContent = boton.dataset.texto;
            document.getElementById('vistaPreviaEnlace').href = boton.dataset.enlace;
            const vistaPrevia = document.getElementById('modalVistaPrevia');
            // Si se abre desde la ventana de Comparar, debe quedar por encima de ella.
            vistaPrevia.style.zIndex = document.getElementById('modalComparar')?.classList.contains('show') ? 1065 : '';
            bootstrap.Modal.getOrCreateInstance(vistaPrevia).show();
        });

        // Texto completo del anuncio al dejar el mouse encima; la espera evita que salte al pasar de largo.
        document.addEventListener('DOMContentLoaded', () => {
            if (!window.bootstrap) return;
            document.querySelectorAll('.texto-completo').forEach(el => new bootstrap.Tooltip(el, {
                delay: { show: 700, hide: 100 }, placement: 'top', container: 'body', customClass: 'tooltip-texto',
            }));
        });

        const previo = leer('filtroAuto');
        if (!previo) return;
        borrar('filtroAuto');
        document.addEventListener('DOMContentLoaded', () => {
            let datos = {};
            try { datos = JSON.parse(previo); } catch (e) {}
            if (datos.scroll) window.scrollTo(0, datos.scroll);
            if (datos.foco === 'q' && buscador) {
                buscador.focus({ preventScroll: true });
                const fin = buscador.value.length;
                buscador.setSelectionRange(fin, fin);
            }
        });
    })();
    {% endif %}
</script>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""

ACCESOS_TEMPLATE = """
<!DOCTYPE html>
<html lang="es" data-bs-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Historial de Accesos | Gálac Ads Intelligence</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">
    <style>
        body { background-color: #0f172a; color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
        .card-custom { background-color: #1e293b; border: 1px solid #334155; border-radius: 12px; }
    </style>
</head>
<body>
<div class="container py-4">
    <div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-4">
        <div>
            <h4 class="fw-bold mb-1"><i class="bi bi-clock-history text-primary"></i> Historial de Accesos</h4>
            <p class="text-secondary small mb-0">Inicios de sesión exitosos al dashboard (hora de Venezuela). Se muestran los últimos {{ limite }}.</p>
        </div>
        <a href="/" class="btn btn-sm btn-outline-light"><i class="bi bi-arrow-left"></i> Volver al dashboard</a>
    </div>

    {% if error %}
    <div class="alert alert-danger border-0">{{ error }}</div>
    {% endif %}

    <div class="row g-3 mb-4">
        {% for r in resumen %}
        <div class="col-md-4">
            <div class="card-custom p-3 h-100">
                <div class="small text-secondary text-uppercase fw-semibold">{{ r.nombre }}</div>
                <div class="fs-3 fw-bold">{{ r.total }} <span class="fs-6 text-secondary fw-normal">accesos</span></div>
                <div class="small text-secondary">Último: {{ r.ultimo or 'nunca' }}</div>
            </div>
        </div>
        {% endfor %}
    </div>

    <div class="card-custom overflow-hidden">
        <div class="table-responsive">
            <table class="table table-dark table-hover align-middle mb-0">
                <thead>
                    <tr class="small text-secondary">
                        <th>Fecha y hora</th>
                        <th>Usuario</th>
                        <th>IP</th>
                        <th>Navegador / dispositivo</th>
                    </tr>
                </thead>
                <tbody>
                    {% for a in accesos %}
                    <tr>
                        <td class="text-nowrap">{{ a.fecha_local }}</td>
                        <td class="fw-semibold">{{ a.nombre_usuario }}</td>
                        <td class="font-monospace small">{{ a.ip_address or '-' }}</td>
                        <td class="small text-secondary text-truncate" style="max-width: 420px;" title="{{ a.user_agent }}">{{ a.user_agent or '-' }}</td>
                    </tr>
                    {% else %}
                    <tr><td colspan="4" class="text-center py-5 text-secondary">Todavía no hay accesos registrados.</td></tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>
</div>
</body>
</html>
"""

SALUD_TEMPLATE = """
<!DOCTYPE html>
<html lang="es" data-bs-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Salud de los Bots | Gálac Ads Intelligence</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">
    <style>
        body { background-color: #0f172a; color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
        .card-custom { background-color: #1e293b; border: 1px solid #334155; border-radius: 12px; }
        details summary { cursor: pointer; }
    </style>
</head>
<body>
<div class="container py-4">
    <div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-4">
        <div>
            <h4 class="fw-bold mb-1"><i class="bi bi-heart-pulse text-primary"></i> Salud de los Bots</h4>
            <p class="text-secondary small mb-0">Cada corrida de los scrapers de Meta y Google (hora de Venezuela). Una corrida se marca atrasada si pasan más de {{ dias_atrasada }} días sin correr.</p>
        </div>
        <a href="/" class="btn btn-sm btn-outline-light"><i class="bi bi-arrow-left"></i> Volver al dashboard</a>
    </div>

    {% if error %}<div class="alert alert-danger border-0">{{ error }}</div>{% endif %}

    {% if proximas %}
    <div class="card-custom p-3 mb-3">
        <div class="d-flex flex-wrap align-items-center gap-3">
            <span class="small text-secondary text-uppercase fw-semibold"><i class="bi bi-alarm"></i> Próxima búsqueda automática</span>
            {% for p in proximas %}
            <span class="d-flex align-items-center gap-2">
                <span class="fw-semibold">{{ p.fuente }}:</span> {{ p.local }}
                <span class="badge bg-primary-subtle text-primary-emphasis cuenta-regresiva" data-fecha="{{ p.iso }}">…</span>
            </span>
            {% endfor %}
        </div>
        <div class="small text-secondary mt-1">Hora de Venezuela. GitHub a veces atrasa las ejecuciones programadas unos minutos u horas; también puedes lanzarlas antes con "Sincronizar".</div>
    </div>
    {% endif %}

    <div class="row g-3 mb-4">
        {% for r in resumen %}
        <div class="col-md-6">
            <div class="card-custom p-3 h-100 border-{{ r.color }}">
                <div class="d-flex justify-content-between align-items-center mb-2">
                    <span class="small text-secondary text-uppercase fw-semibold"><i class="bi {{ 'bi-meta' if r.fuente == 'Meta' else 'bi-google' }}"></i> Bot de {{ r.fuente }}</span>
                    <span class="badge bg-{{ r.color }}{% if r.color == 'warning' %} text-dark{% endif %}">{{ r.etiqueta }}</span>
                </div>
                {% if r.estado == 'sin_datos' %}
                <div class="text-secondary small">Todavía no hay corridas registradas (se registran desde la próxima).</div>
                {% else %}
                <div class="fs-4 fw-bold">{{ r.anuncios }} <span class="fs-6 text-secondary fw-normal">anuncios en {{ r.empresas }} empresas</span></div>
                <div class="small text-secondary">Última corrida: {{ r.fin_local }}{% if r.errores %} · <span class="text-warning">{{ r.errores }} con error</span>{% endif %}</div>
                {% if r.url_ejecucion %}<a href="{{ r.url_ejecucion }}" target="_blank" rel="noopener" class="small">Ver registro en GitHub <i class="bi bi-box-arrow-up-right"></i></a>{% endif %}
                {% endif %}
            </div>
        </div>
        {% endfor %}
    </div>

    <div class="card-custom overflow-hidden">
        <div class="table-responsive">
            <table class="table table-dark table-hover align-middle mb-0">
                <thead>
                    <tr class="small text-secondary">
                        <th>Inicio</th><th>Bot</th><th>Estado</th><th class="text-end">Anuncios</th><th class="text-end">Duración</th><th>Detalle por empresa</th>
                    </tr>
                </thead>
                <tbody>
                    {% for c in corridas %}
                    <tr>
                        <td class="text-nowrap">{{ c.inicio_local }}</td>
                        <td>{{ c.fuente }}</td>
                        <td><span class="badge bg-{{ c.color }}{% if c.color == 'warning' %} text-dark{% endif %}">{{ c.etiqueta }}</span></td>
                        <td class="text-end fw-semibold">{{ c.anuncios }}</td>
                        <td class="text-end text-secondary small">{{ c.duracion }}</td>
                        <td class="small">
                            <details>
                                <summary class="text-secondary">{{ c.empresas }} empresas{% if c.errores %} · <span class="text-warning">{{ c.errores }} con error</span>{% endif %}{% if c.url_ejecucion %} · <a href="{{ c.url_ejecucion }}" target="_blank" rel="noopener">GitHub</a>{% endif %}</summary>
                                {% if c.fallo %}<div class="text-danger mt-1">{{ c.fallo }}</div>{% endif %}
                                <ul class="list-unstyled mt-1 mb-0">
                                    {% for nombre, e in c.detalle_empresas %}
                                    <li>{% if e.error %}❌{% elif e.aviso %}⚠️{% else %}✅{% endif %} <strong>{{ nombre }}</strong>: {{ e.anuncios }} anuncios{% if e.error %} — <span class="text-danger">{{ e.error }}</span>{% elif e.aviso %} — <span class="text-warning">{{ e.aviso }}</span>{% endif %}</li>
                                    {% endfor %}
                                </ul>
                            </details>
                        </td>
                    </tr>
                    {% else %}
                    <tr><td colspan="6" class="text-center py-5 text-secondary">Todavía no hay corridas registradas. Aparecerán desde la próxima ejecución de los bots.</td></tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>
</div>
<script>
    // Cuenta regresiva hasta la próxima búsqueda automática; se actualiza cada minuto.
    function actualizarCuentas() {
        document.querySelectorAll('.cuenta-regresiva').forEach(el => {
            const falta = new Date(el.dataset.fecha) - new Date();
            if (falta <= 0) { el.textContent = 'en curso o por empezar'; return; }
            const min = Math.floor(falta / 60000), d = Math.floor(min / 1440), h = Math.floor((min % 1440) / 60), m = min % 60;
            el.textContent = 'en ' + (d ? d + ' d ' : '') + (d || h ? h + ' h ' : '') + m + ' min';
        });
    }
    actualizarCuentas();
    setInterval(actualizarCuentas, 60000);
</script>
</body>
</html>
"""

@app.route('/salud')
@salud_required
def salud():
    corridas, resumen, error = [], [], None
    conn = get_db_connection()
    if not conn:
        error = "No se pudo conectar a la base de datos."
    else:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                resumen = ultimas_corridas(cur)
                cur.execute("""
                    SELECT fuente, estado, anuncios, empresas, errores, detalle, url_ejecucion,
                           to_char(inicio AT TIME ZONE 'America/Caracas', 'YYYY-MM-DD HH24:MI') AS inicio_local,
                           EXTRACT(EPOCH FROM (fin - inicio))::int AS segundos
                    FROM corridas_scraper ORDER BY inicio DESC LIMIT 60
                """)
                corridas = [dict(r) for r in cur.fetchall()]
        except Exception as e:
            error = f"Error consultando las corridas: {e}"
        finally:
            conn.close()
    for c in corridas:
        c['etiqueta'], c['color'] = ESTADOS_CORRIDA.get(c['estado'], (c['estado'], 'secondary'))
        segundos = c.get('segundos') or 0
        c['duracion'] = f"{segundos // 60} min {segundos % 60:02d} s" if segundos else '-'
        try:
            detalle = json.loads(c.get('detalle') or '{}')
        except ValueError:
            detalle = {}
        c['fallo'] = detalle.get('fallo')
        c['detalle_empresas'] = sorted((detalle.get('empresas') or {}).items())
    if not resumen:
        resumen = [{'fuente': f, 'estado': 'sin_datos', 'etiqueta': 'Sin registros', 'color': 'secondary'} for f in ('Meta', 'Google')]
    return render_template_string(SALUD_TEMPLATE, corridas=corridas, resumen=resumen, error=error,
                                  dias_atrasada=DIAS_CORRIDA_ATRASADA, proximas=proximas_busquedas())

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    usuario_sel = 'mercadeo'
    if request.method == 'POST':
        usuario_sel = request.form.get('usuario', 'mercadeo')
        datos = USUARIOS.get(usuario_sel)
        password = request.form.get('password', '')
        if not datos or not datos['password']:
            error = "Ese usuario todavía no tiene contraseña configurada."
        elif hmac.compare_digest(password.encode('utf-8'), datos['password'].encode('utf-8')):
            session['logged_in'] = True
            session['usuario'] = usuario_sel
            session['usuario_origen'] = usuario_sel
            registrar_acceso(usuario_sel)
            return redirect(url_for('index'))
        else:
            error = "Contraseña incorrecta. Inténtalo de nuevo."
    return render_template_string(LOGIN_TEMPLATE, error=error, usuarios=USUARIOS, usuario_sel=usuario_sel)

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    session.pop('usuario', None)
    session.pop('usuario_origen', None)
    return redirect(url_for('login'))

@app.route('/cambiar_usuario', methods=['POST'])
@login_required
def cambiar_usuario():
    if not puede_cambiar_usuario():
        return redirect(url_for('index', msg="⛔ Solo el Administrador y Funciones Beta pueden cambiar de usuario."))
    destino = request.form.get('usuario', '')
    datos = USUARIOS.get(destino)
    if not datos or not datos['password']:
        return redirect(url_for('index', msg="❌ Ese usuario no existe o no tiene contraseña configurada."))
    if destino == usuario_actual():
        return redirect(url_for('index'))
    if cambio_requiere_password(destino):
        password = request.form.get('password', '')
        if not hmac.compare_digest(password.encode('utf-8'), datos['password'].encode('utf-8')):
            return redirect(url_for('index', msg="❌ Contraseña incorrecta, no se cambió de usuario."))
        # Entrar como Administrador con su clave equivale a un inicio de sesión nuevo.
        session['usuario_origen'] = destino
        registrar_acceso(destino)
    session['usuario'] = destino
    return redirect(url_for('index', msg=f"🔄 Ahora estás viendo el panel como {datos['nombre']}."))

@app.route('/accesos')
@admin_required
def accesos():
    limite = 500
    accesos_lista, resumen, error = [], [], None
    conn = get_db_connection()
    if not conn:
        error = "No se pudo conectar a la base de datos."
    else:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT usuario, ip_address, user_agent,
                           to_char(fecha_acceso AT TIME ZONE 'UTC' AT TIME ZONE 'America/Caracas', 'YYYY-MM-DD HH24:MI') AS fecha_local
                    FROM accesos_dashboard
                    ORDER BY fecha_acceso DESC
                    LIMIT %s
                """, (limite,))
                accesos_lista = cur.fetchall()
                cur.execute("""
                    SELECT usuario, COUNT(*) AS total,
                           to_char(MAX(fecha_acceso) AT TIME ZONE 'UTC' AT TIME ZONE 'America/Caracas', 'YYYY-MM-DD HH24:MI') AS ultimo
                    FROM accesos_dashboard
                    GROUP BY usuario
                """)
                por_usuario = {r['usuario']: r for r in cur.fetchall()}
        except Exception as e:
            error = f"Error consultando accesos: {e}"
            por_usuario = {}
        finally:
            conn.close()
        for a in accesos_lista:
            a['nombre_usuario'] = USUARIOS.get(a['usuario'], {}).get('nombre', 'Anterior a usuarios')
        for clave, u in USUARIOS.items():
            r = por_usuario.get(clave, {})
            resumen.append({'nombre': u['nombre'], 'total': r.get('total', 0), 'ultimo': r.get('ultimo')})
    return render_template_string(ACCESOS_TEMPLATE, accesos=accesos_lista, resumen=resumen, error=error, limite=limite)

@app.route('/')
@login_required
def index():
    msg = request.args.get('msg')
    q = request.args.get('q', '').strip()
    companias_sel = request.args.getlist('compania')
    companias_sel = [c.strip() for c in companias_sel if c.strip()]
    estado = request.args.get('estado', '').strip()
    formato = request.args.get('formato', '').strip()
    plataforma = request.args.get('plataforma', '').strip()
    tiempo = request.args.get('tiempo', 'todo').strip()
    duracion = request.args.get('duracion', 'todas').strip()
    fuente = request.args.get('fuente', '').strip()

    raw_urls_content, _ = get_github_urls_file()
    config_urls = parse_urls_txt(raw_urls_content)
    raw_google_content, _ = get_github_urls_file(URLS_GOOGLE_FILE_PATH)
    config_google = parse_urls_google(raw_google_content)
    productos_sel = [p.strip() for p in request.args.getlist('producto') if p.strip()]
    es_beta = usuario_actual() == USUARIO_BETA
    raw_productos_content = leer_config(PRODUCTOS_FILE_PATH)
    reglas_productos = parse_productos(raw_productos_content)

    conn = get_db_connection()
    anuncios = []
    lista_companias = []
    companias_bloqueadas = []
    stats_empresas = []
    stats_empresas_vigentes = []

    if conn:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT compania FROM companias_bloqueadas ORDER BY compania ASC")
                companias_bloqueadas = [r['compania'] for r in cur.fetchall()]

                if companias_bloqueadas:
                    cur.execute("""
                        SELECT DISTINCT compania FROM anuncios 
                        WHERE compania IS NOT NULL AND compania != '' 
                        AND compania != ALL(%s) 
                        ORDER BY compania ASC
                    """, (companias_bloqueadas,))
                else:
                    cur.execute("""
                        SELECT DISTINCT compania FROM anuncios 
                        WHERE compania IS NOT NULL AND compania != '' 
                        ORDER BY compania ASC
                    """)
                lista_companias = [r['compania'] for r in cur.fetchall()]

                if not es_beta:
                    # Conteo agrupado por empresa. Los '%%' son necesarios porque la consulta
                    # siempre se ejecuta con parámetros (psycopg2 leería '%v' como marcador).
                    es_video = "(LOWER(formato) LIKE '%%video%%' OR COALESCE(duracion_segundos, 0) > 0)"
                    filtro_bloqueadas = "AND compania != ALL(%s)" if companias_bloqueadas else ""
                    cur.execute(f"""
                        SELECT
                            compania,
                            COUNT(*) AS total_anuncios,
                            SUM(CASE WHEN {es_video} THEN 1 ELSE 0 END) AS total_videos,
                            SUM(CASE WHEN NOT {es_video} AND (LOWER(formato) LIKE '%%foto%%' OR LOWER(formato) LIKE '%%imagen%%') THEN 1 ELSE 0 END) AS total_fotos,
                            SUM(CASE WHEN NOT {es_video} AND LOWER(formato) LIKE '%%texto%%' THEN 1 ELSE 0 END) AS total_textos
                        FROM anuncios
                        WHERE compania IS NOT NULL AND compania != ''
                        {filtro_bloqueadas}
                        GROUP BY compania
                        ORDER BY total_anuncios DESC;
                    """, (companias_bloqueadas,) if companias_bloqueadas else ())
                    stats_empresas = cur.fetchall()

                query = "SELECT * FROM anuncios WHERE 1=1"
                params = []

                if companias_bloqueadas:
                    query += " AND compania != ALL(%s)"
                    params.append(companias_bloqueadas)

                if q:
                    query += " AND (titulo ILIKE %s OR link_individual ILIKE %s)"
                    like_val = f"%{q}%"
                    params.extend([like_val, like_val])
                if companias_sel:
                    query += " AND compania = ANY(%s)"
                    params.append(companias_sel)
                if estado:
                    query += " AND estado = %s"
                    params.append(estado)
                if formato == 'imagen':
                    query += " AND (formato ILIKE '%%imagen%%' OR formato ILIKE '%%foto%%')"
                elif formato:
                    query += " AND formato ILIKE %s"
                    params.append(f"%{formato}%")
                if plataforma:
                    query += " AND plataformas ILIKE %s"
                    params.append(f"%{plataforma}%")
                if fuente:
                    query += " AND COALESCE(fuente, 'Meta') = %s"
                    params.append(fuente)

                if duracion == 'corta':
                    query += " AND duracion_segundos > 0 AND duracion_segundos < 15"
                elif duracion == 'media':
                    query += " AND duracion_segundos >= 15 AND duracion_segundos <= 60"
                elif duracion == 'larga':
                    query += " AND duracion_segundos > 60"
                elif duracion == 'sin_video':
                    query += " AND (duracion_segundos = 0 OR duracion_segundos IS NULL)"

                hoy = datetime.today()
                if tiempo == '7d':
                    query += " AND fecha_subida >= %s"
                    params.append((hoy - timedelta(days=7)).strftime('%Y-%m-%d'))
                elif tiempo == '15d':
                    query += " AND fecha_subida >= %s"
                    params.append((hoy - timedelta(days=15)).strftime('%Y-%m-%d'))
                elif tiempo == '30d':
                    query += " AND fecha_subida >= %s"
                    params.append((hoy - timedelta(days=30)).strftime('%Y-%m-%d'))
                elif tiempo == '90d':
                    query += " AND fecha_subida >= %s"
                    params.append((hoy - timedelta(days=90)).strftime('%Y-%m-%d'))

                query += " ORDER BY id DESC LIMIT 1000"
                cur.execute(query, tuple(params))
                anuncios = cur.fetchall()
        except Exception as e:
            print(f"Error consultando BD: {e}")
            anuncios = []
        finally:
            conn.close()

    for a in anuncios:
        a['productos'] = clasificar_productos(a, reglas_productos)
    if productos_sel:
        anuncios = [a for a in anuncios if coincide_producto(a['productos'], productos_sel)]

    anuncios_winning = []
    anuncios_nuevos = []
    anuncios_retirados = []

    for a in anuncios:
        fecha_detectada = extraer_fecha_anuncio(a)
        a['fecha_display'] = fecha_detectada
        dias = calcular_dias_activo(fecha_detectada, fecha_ultima_vista(a) if es_beta else None)
        a['dias_activo'] = dias
        
        estado_ad = str(a.get('estado', '')).strip().lower()
        a['es_winning'] = (dias >= DIAS_WINNING_AD and fecha_detectada != 'N/A' and estado_ad == 'activo')
        
        a['plataformas_html'] = render_plataformas_badges(a.get('plataformas'))
        
        if a['es_winning']:
            anuncios_winning.append(a)
            
        if dias <= 2 and fecha_detectada != 'N/A':
            anuncios_nuevos.append(a)

        # Filtro para anuncios retirados o inactivos
        if estado_ad == 'inactivo':
            anuncios_retirados.append(a)

    historial_data = {'fechas': [], 'series': []}
    destinos_data = {'companias': [], 'destinos': [], 'series': {}}
    alerta_salud = False
    if es_beta:
        destinos_data = destinos_por_empresa(anuncios)
        historial_data = historial_activos(companias_sel, fuente, companias_bloqueadas)
    if usuario_actual() in USUARIOS_SALUD:
        conn_salud = get_db_connection()
        if conn_salud:
            try:
                with conn_salud.cursor(cursor_factory=RealDictCursor) as cur:
                    alerta_salud = any(r['problema'] for r in ultimas_corridas(cur))
            except Exception as e:
                print(f"Error leyendo la salud de los bots: {e}")
            finally:
                conn_salud.close()

    if es_beta:
        stats_empresas = estadisticas_por_empresa(anuncios)
        stats_empresas_vigentes = estadisticas_por_empresa([a for a in anuncios if a.get('presente_en_meta')])

    total_anuncios = len(anuncios)
    companias_set = {a['compania'] for a in anuncios if a.get('compania')}
    total_companias = len(companias_set)
    formatos = [str(a.get('formato') or '').lower() for a in anuncios]
    total_videos = sum(1 for f in formatos if 'video' in f)
    total_fotos = sum(1 for f in formatos if 'imagen' in f or 'foto' in f)
    total_textos = sum(1 for f in formatos if 'texto' in f)
    total_otros = max(0, total_anuncios - (total_videos + total_fotos + total_textos))
    total_nuevos = len(anuncios_nuevos)
    total_winning = len(anuncios_winning)
    total_retirados = len(anuncios_retirados)

    pct_videos = round((total_videos / total_anuncios * 100), 1) if total_anuncios > 0 else 0
    pct_imagenes = round((total_fotos / total_anuncios * 100), 1) if total_anuncios > 0 else 0
    pct_textos = round((total_textos / total_anuncios * 100), 1) if total_anuncios > 0 else 0
    pct_otros = round((total_otros / total_anuncios * 100), 1) if total_anuncios > 0 else 0

    palabras_empresas = set()
    # Desde que el panel usa nombres cortos ("Fina"), el nombre largo ("Fina Partner") solo queda
    # como nombre de búsqueda en urls.txt: se excluyen ambos para que "partner" no salga en el ranking.
    nombres_empresas = list(lista_companias) + [c.get('nombre_bot') for c in config_urls]
    for comp in nombres_empresas:
        if comp:
            tokens = re.findall(r'[a-záéíóúñ0-9]+', comp.lower())
            for t in tokens:
                palabras_empresas.add(t)

    palabras_encontradas = []
    for a in anuncios:
        # Los anuncios de Google sin título de YouTube solo traen un título generado por el bot.
        if a.get('fuente') == 'Google' and (not es_beta or str(a.get('titulo') or '').startswith('Anuncio de ')):
            continue
        texto_completo = f"{a.get('texto') or ''} {a.get('titulo') or ''}".lower()
        palabras = re.findall(r'[a-záéíóúñ]{4,}', texto_completo)
        palabras_limpias = [
            p for p in palabras 
            if p not in STOPWORDS_ES and p not in palabras_empresas
        ]
        palabras_encontradas.extend(palabras_limpias)

    contador_palabras = Counter(palabras_encontradas)
    top_palabras = contador_palabras.most_common(20 if es_beta else 12)
    comparativa = comparativa_empresas(anuncios, palabras_empresas) if es_beta else []

    keywords_chart_data = {
        "labels": [p[0].capitalize() for p in top_palabras],
        "values": [p[1] for p in top_palabras]
    }

    conteo_companias = Counter(a['compania'] for a in anuncios if a.get('compania'))
    top_companias = [c for c, _ in conteo_companias.most_common(7)]
    timeline_data_historico = construir_timeline(anuncios, top_companias)
    anuncios_actuales_meta = [a for a in anuncios if a.get('presente_en_meta')]
    timeline_data_actual = construir_timeline(anuncios_actuales_meta, top_companias)

    format_data = {
        "videos": total_videos,
        "imagenes": total_fotos,
        "textos": total_textos,
        "otros": total_otros,
        "pct_videos": pct_videos,
        "pct_imagenes": pct_imagenes,
        "pct_textos": pct_textos,
        "pct_otros": pct_otros
    }

    return render_template_string(
        HTML_TEMPLATE,
        anuncios=anuncios,
        anuncios_winning=anuncios_winning,
        anuncios_nuevos=anuncios_nuevos,
        anuncios_retirados=anuncios_retirados,
        lista_companias=lista_companias,
        companias_bloqueadas=companias_bloqueadas,
        config_urls=config_urls,
        raw_urls_content=raw_urls_content,
        config_google=config_google,
        raw_google_content=raw_google_content,
        lista_productos=sorted((nombre for nombre, _ in reglas_productos), key=normalizar) + [SIN_CLASIFICAR],
        raw_productos_content=raw_productos_content,
        companias_sel=companias_sel,
        productos_sel=productos_sel,
        total_anuncios=total_anuncios,
        total_companias=total_companias,
        total_videos=total_videos,
        total_fotos=total_fotos,
        total_nuevos=total_nuevos,
        total_winning=total_winning,
        total_retirados=total_retirados,
        top_palabras=top_palabras,
        keywords_chart_data=keywords_chart_data,
        timeline_data_historico=timeline_data_historico,
        timeline_data_actual=timeline_data_actual,
        format_data=format_data,
        stats_empresas=stats_empresas,
        stats_empresas_vigentes=stats_empresas_vigentes,
        empresas_config=empresas_configuradas(raw_urls_content, raw_google_content) if usuario_actual() in USUARIOS_FORMULARIO_EMPRESAS else [],
        historial_data=historial_data,
        comparativa=comparativa,
        destinos_data=destinos_data,
        iconos_destino=ICONOS_DESTINO,
        alerta_salud=alerta_salud,
        msg=msg
    )

def formulario_empresas_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return {'ok': False, 'error': 'Tu sesión expiró; vuelve a iniciar sesión.'}, 401
        if usuario_actual() not in USUARIOS_FORMULARIO_EMPRESAS:
            return {'ok': False, 'error': 'Tu usuario todavía no puede modificar las empresas.'}, 403
        return f(*args, **kwargs)
    return decorated_function

@app.route('/empresas/guardar', methods=['POST'])
@formulario_empresas_required
def guardar_empresa():
    datos = request.get_json(silent=True) or {}
    nombre = re.sub(r'\s+', ' ', str(datos.get('nombre') or '')).strip()
    original = (str(datos.get('nombre_original')).strip() or None) if datos.get('nombre_original') else None
    if not nombre:
        return {'ok': False, 'error': 'Escribe el nombre de la empresa.'}, 400
    if '|' in nombre or len(nombre) > 60:
        return {'ok': False, 'error': 'El nombre no puede tener "|" ni más de 60 caracteres.'}, 400

    raw_meta, _ = get_github_urls_file(URLS_FILE_PATH)
    raw_google, _ = get_github_urls_file(URLS_GOOGLE_FILE_PATH)
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return {'ok': False, 'error': 'Falta configurar GITHUB_TOKEN o GITHUB_REPO.'}, 500
    actuales = {e['nombre']: e for e in empresas_configuradas(raw_meta, raw_google)}
    if nombre in actuales and nombre != original:
        return {'ok': False, 'error': f'Ya existe una empresa llamada "{nombre}". Edítala en vez de crear otra.'}, 400
    if original and original not in actuales:
        return {'ok': False, 'error': f'"{original}" ya no existe; recarga la página.'}, 409

    previas = {m['url']: m for m in (actuales.get(original) or {}).get('meta', [])}
    lineas_meta, paginas, errores = [], set(), []
    for texto in datos.get('meta') or []:
        texto = str(texto).strip()
        if not texto:
            continue
        if texto in previas:  # sin cambios: se conserva tal cual, con su nombre de búsqueda (p. ej. "Saul Casanova")
            entrada = previas[texto]
            bot = entrada['bot']
        else:
            entrada, error = normalizar_entrada_meta(texto, nombre)
            if error:
                errores.append(error)
                continue
            bot = nombre if entrada['tipo'] == 'pagina' else entrada['bot']
        tipo, pagina = tipo_entrada_meta(entrada['url'])
        if pagina:
            if pagina in paginas:
                continue
            paginas.add(pagina)
            for otra in actuales.values():
                if otra['nombre'] != original and any(m.get('id') == pagina for m in otra['meta']):
                    errores.append(f'Esa página de Meta ya está registrada en "{otra["nombre"]}".')
        lineas_meta.append(f"{nombre} | {bot} | {entrada['url']}")
    lineas_google, dominios = [], set()
    for texto in datos.get('google') or []:
        dominio, error = normalizar_dominio(str(texto))
        if error:
            errores.append(error)
        elif dominio and dominio not in dominios:
            dominios.add(dominio)
            lineas_google.append(f"{nombre} | {dominio}")
    if errores:
        return {'ok': False, 'error': ' '.join(dict.fromkeys(errores))}, 400
    if not lineas_meta and not lineas_google:
        return {'ok': False, 'error': 'Agrega al menos una página de Meta o un sitio web para Google.'}, 400

    nuevo_meta = reemplazar_lineas_empresa(raw_meta, original, lineas_meta)
    nuevo_google = reemplazar_lineas_empresa(raw_google, original, lineas_google)
    for contenido, anterior, ruta in ((nuevo_meta, raw_meta, URLS_FILE_PATH), (nuevo_google, raw_google, URLS_GOOGLE_FILE_PATH)):
        if contenido != anterior:
            ok, mensaje = update_github_urls_file(contenido, ruta)
            if not ok:
                return {'ok': False, 'error': mensaje}, 502
    renombrados = renombrar_empresa_en_bd(original, nombre) if original and original != nombre else 0
    texto = f'✅ Empresa "{nombre}" {"actualizada" if original else "agregada"}. Los bots la buscarán desde su próxima corrida.'
    if renombrados:
        texto += f' Se renombraron {renombrados} anuncios ya guardados.'
    return {'ok': True, 'mensaje': texto}

@app.route('/empresas/eliminar', methods=['POST'])
@formulario_empresas_required
def eliminar_empresa():
    nombre = str((request.get_json(silent=True) or {}).get('nombre') or '').strip()
    if not nombre:
        return {'ok': False, 'error': 'Falta el nombre de la empresa.'}, 400
    raw_meta, _ = get_github_urls_file(URLS_FILE_PATH)
    raw_google, _ = get_github_urls_file(URLS_GOOGLE_FILE_PATH)
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return {'ok': False, 'error': 'Falta configurar GITHUB_TOKEN o GITHUB_REPO.'}, 500
    cambios = 0
    for anterior, ruta in ((raw_meta, URLS_FILE_PATH), (raw_google, URLS_GOOGLE_FILE_PATH)):
        nuevo = reemplazar_lineas_empresa(anterior, nombre, [])
        if nuevo != anterior:
            ok, mensaje = update_github_urls_file(nuevo, ruta)
            if not ok:
                return {'ok': False, 'error': mensaje}, 502
            cambios += 1
    if not cambios:
        return {'ok': False, 'error': f'"{nombre}" no está en la configuración.'}, 404
    return {'ok': True, 'mensaje': f'🗑️ "{nombre}" ya no se monitoreará. Sus anuncios guardados se conservan en el historial.'}

@app.route('/guardar_urls_txt', methods=['POST'])
@editor_urls_required
def guardar_urls_txt():
    raw_urls = request.form.get('raw_urls', '').strip()
    success, message = update_github_urls_file(raw_urls)
    if success:
        return redirect(url_for('index', msg="✅ Archivo urls.txt guardado en GitHub exitosamente."))
    else:
        return redirect(url_for('index', msg=f"❌ {message}"))

@app.route('/guardar_urls_google', methods=['POST'])
@editor_urls_required
def guardar_urls_google():
    raw_urls = request.form.get('raw_urls', '').strip()
    success, message = update_github_urls_file(raw_urls, URLS_GOOGLE_FILE_PATH)
    if success:
        return redirect(url_for('index', msg="✅ Archivo urls_google.txt guardado en GitHub exitosamente."))
    return redirect(url_for('index', msg=f"❌ {message}"))

@app.route('/guardar_productos', methods=['POST'])
@editor_urls_required
def guardar_productos():
    raw = request.form.get('raw_productos', '').strip()
    success, message = update_github_urls_file(raw, PRODUCTOS_FILE_PATH)
    if success:
        return redirect(url_for('index', msg="✅ Categorías de producto guardadas en GitHub."))
    return redirect(url_for('index', msg=f"❌ {message}"))

@app.route('/bloquear_compania', methods=['POST'])
@login_required
def bloquear_compania():
    comp = request.form.get('compania', '').strip()
    if comp:
        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO companias_bloqueadas (compania) VALUES (%s) ON CONFLICT DO NOTHING", (comp,))
                    conn.commit()
            except Exception as e:
                print(f"Error bloqueando compañía: {e}")
            finally:
                conn.close()
    return redirect(url_for('index', msg=f"🚫 Compañía '{comp}' bloqueada del panel."))

@app.route('/desbloquear_compania', methods=['POST'])
@login_required
def desbloquear_compania():
    comp = request.form.get('compania', '').strip()
    if comp:
        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM companias_bloqueadas WHERE compania = %s", (comp,))
                    conn.commit()
            except Exception as e:
                print(f"Error desbloqueando compañía: {e}")
            finally:
                conn.close()
    return redirect(url_for('index', msg=f"✅ Compañía '{comp}' desbloqueada exitosamente."))

@app.route('/lanzar_scraper', methods=['POST'])
@login_required
def lanzar_scraper():
    dias = request.form.get("dias_scraping", "30")

    if not GITHUB_TOKEN or not GITHUB_REPO:
        return redirect(url_for('index', msg="❌ Falta configurar GITHUB_TOKEN o GITHUB_REPO en Render."))

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    workflows = [
        ("Meta", WORKFLOW_FILE, {"ref": "main", "inputs": {"dias": str(dias)}}),
        ("Google", WORKFLOW_GOOGLE_FILE, {"ref": "main"}),
    ]

    errores = []
    for nombre, archivo, payload in workflows:
        url_api = f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{archivo}/dispatches"
        try:
            response = requests.post(url_api, json=payload, headers=headers, timeout=10)
            if response.status_code != 204:
                errores.append(f"{nombre} (código {response.status_code}): {response.text}")
        except Exception as e:
            errores.append(f"{nombre}: {e}")

    if not errores:
        return redirect(url_for('index', msg="🚀 Scrapers de Meta y Google lanzados. Los datos se actualizarán en breve."))
    return redirect(url_for('index', msg="⚠️ Error al lanzar: " + " | ".join(errores)))

def excel_reporte(df):
    # Excel de Funciones Beta: solo las columnas útiles, con nombres claros y fechas reales.
    filas = df.to_dict('records')

    def fecha(valor):
        f = parse_date_str(valor)
        try:
            return datetime.strptime(f, '%Y-%m-%d').date() if f else None
        except ValueError:
            return None

    def texto(r):
        t = str(r.get('texto') or r.get('titulo') or '').strip()
        return '' if t.startswith('Anuncio de ') else t

    return pd.DataFrame({
        'Empresa': [r.get('compania') or '' for r in filas],
        'Fuente': [r.get('fuente') or 'Meta' for r in filas],
        'Estado': [r.get('estado') or '' for r in filas],
        'Texto del anuncio': [texto(r) for r in filas],
        'Productos': [r.get('productos') or '' for r in filas],
        'Formato': [r.get('formato') or '' for r in filas],
        'Duración (s)': [int(r['duracion_segundos']) if pd.notna(r.get('duracion_segundos')) and r.get('duracion_segundos') else None for r in filas],
        'Plataformas': [r.get('plataformas') or '' for r in filas],
        'Fecha de inicio': [fecha(r.get('fecha_subida_detectada')) for r in filas],
        'Fecha última vista': [fecha(fecha_ultima_vista(r)) for r in filas],
        'Días activo': [r.get('dias_activo') for r in filas],
        'Winning Ad': ['Sí' if r.get('es_winning_ad') else 'No' for r in filas],
        'Sigue publicado': ['Sí' if r.get('presente_en_meta') else 'No' for r in filas],
        'Destino': [r.get('destino') or '' for r in filas],
        'Enlace de destino': [r.get('destino_url') or '' for r in filas],
        'Enlace': [r.get('link_individual') or '' for r in filas],
        'Imagen': [r.get('imagen_url') or '' for r in filas],
    })

@app.route('/descargar_excel')
@login_required
def descargar_excel():
    conn = get_db_connection()
    if not conn:
        return redirect(url_for('index', msg="Error: Base de datos no disponible."))

    try:
        q = request.args.get('q', '').strip()
        companias_sel = request.args.getlist('compania')
        companias_sel = [c.strip() for c in companias_sel if c.strip()]
        estado = request.args.get('estado', '').strip()
        formato = request.args.get('formato', '').strip()
        plataforma = request.args.get('plataforma', '').strip()
        tiempo = request.args.get('tiempo', 'todo').strip()
        duracion = request.args.get('duracion', 'todas').strip()
        fuente = request.args.get('fuente', '').strip()

        with conn.cursor() as cur:
            cur.execute("SELECT compania FROM companias_bloqueadas")
            companias_bloqueadas = [r[0] for r in cur.fetchall()]

        query = "SELECT * FROM anuncios WHERE 1=1"
        params = []

        if companias_bloqueadas:
            query += " AND compania != ALL(%s)"
            params.append(companias_bloqueadas)

        if q:
            query += " AND (titulo ILIKE %s OR link_individual ILIKE %s)"
            like_val = f"%{q}%"
            params.extend([like_val, like_val])
        if companias_sel:
            query += " AND compania = ANY(%s)"
            params.append(companias_sel)
        if estado:
            query += " AND estado = %s"
            params.append(estado)
        if formato == 'imagen':
            query += " AND (formato ILIKE '%%imagen%%' OR formato ILIKE '%%foto%%')"
        elif formato:
            query += " AND formato ILIKE %s"
            params.append(f"%{formato}%")
        if plataforma:
            query += " AND plataformas ILIKE %s"
            params.append(f"%{plataforma}%")
        if fuente:
            query += " AND COALESCE(fuente, 'Meta') = %s"
            params.append(fuente)

        if duracion == 'corta':
            query += " AND duracion_segundos > 0 AND duracion_segundos < 15"
        elif duracion == 'media':
            query += " AND duracion_segundos >= 15 AND duracion_segundos <= 60"
        elif duracion == 'larga':
            query += " AND duracion_segundos > 60"
        elif duracion == 'sin_video':
            query += " AND (duracion_segundos = 0 OR duracion_segundos IS NULL)"

        hoy = datetime.today()
        if tiempo == '7d':
            query += " AND fecha_subida >= %s"
            params.append((hoy - timedelta(days=7)).strftime('%Y-%m-%d'))
        elif tiempo == '15d':
            query += " AND fecha_subida >= %s"
            params.append((hoy - timedelta(days=15)).strftime('%Y-%m-%d'))
        elif tiempo == '30d':
            query += " AND fecha_subida >= %s"
            params.append((hoy - timedelta(days=30)).strftime('%Y-%m-%d'))
        elif tiempo == '90d':
            query += " AND fecha_subida >= %s"
            params.append((hoy - timedelta(days=90)).strftime('%Y-%m-%d'))

        query += " ORDER BY id DESC"

        df = pd.read_sql_query(query, conn, params=params)

        reglas_productos = parse_productos(leer_config(PRODUCTOS_FILE_PATH))
        productos_por_fila = [clasificar_productos(r, reglas_productos) for r in df.to_dict('records')]
        df['productos'] = [', '.join(p) for p in productos_por_fila]
        productos_sel = [p.strip() for p in request.args.getlist('producto') if p.strip()]
        if productos_sel:
            df = df[[coincide_producto(p, productos_sel) for p in productos_por_fila]]

        df['fecha_subida_detectada'] = df.apply(lambda row: extraer_fecha_anuncio(row.to_dict()), axis=1)
        es_beta = usuario_actual() == USUARIO_BETA
        df['dias_activo'] = df.apply(lambda r: calcular_dias_activo(r['fecha_subida_detectada'], fecha_ultima_vista(r.to_dict()) if es_beta else None), axis=1)
        df['es_winning_ad'] = df.apply(lambda r: (r['dias_activo'] >= DIAS_WINNING_AD and str(r.get('estado', '')).strip().lower() == 'activo'), axis=1)

        output = io.BytesIO()
        if es_beta:
            reporte = excel_reporte(df)
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                reporte.to_excel(writer, index=False, sheet_name='Anuncios')
                hoja = writer.sheets['Anuncios']
                hoja.freeze_panes = 'A2'
                hoja.auto_filter.ref = hoja.dimensions
                for i, columna in enumerate(reporte.columns, start=1):
                    largo = max([len(str(columna))] + [len(str(v)) for v in reporte[columna].head(500)])
                    hoja.column_dimensions[hoja.cell(row=1, column=i).column_letter].width = min(max(largo + 2, 10), 70)
                    if columna.startswith('Fecha'):
                        for celda in hoja.iter_rows(min_row=2, min_col=i, max_col=i):
                            celda[0].number_format = 'DD/MM/YYYY'
            output.seek(0)
            filename = f"Reporte_Galac_Ads_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
            return send_file(output, download_name=filename, as_attachment=True, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Anuncios_Meta')
        output.seek(0)

        filename = f"Reporte_Meta_Ads_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        return send_file(output, download_name=filename, as_attachment=True, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    except Exception as e:
        return redirect(url_for('index', msg=f"Error al generar archivo: {e}"))
    finally:
        conn.close()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=True)
