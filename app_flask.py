import os
import io
import hmac
import base64
import re
import unicodedata
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta
from collections import Counter
from functools import wraps
import pandas as pd
from dotenv import load_dotenv
from flask import Flask, render_template_string, request, redirect, url_for, send_file, session

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

@app.context_processor
def inyectar_usuario():
    clave = usuario_actual()
    return {
        'usuario_nombre': USUARIOS.get(clave, {}).get('nombre', clave),
        'es_admin': clave == USUARIO_ADMIN,
        'es_beta': clave == USUARIO_BETA,
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
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS accesos_dashboard (
                        id SERIAL PRIMARY KEY,
                        fecha_acceso TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        ip_address VARCHAR(100),
                        user_agent TEXT
                    );
                """)
                cur.execute("ALTER TABLE accesos_dashboard ADD COLUMN IF NOT EXISTS usuario VARCHAR(50);")
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
                            <i class="bi bi-people"></i> Cambiar de Usuario
                        </button>
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
                    <li class="nav-item">
                        <button class="nav-link active fw-semibold" data-bs-toggle="tab" data-bs-target="#urls-tab-meta" type="button">
                            <i class="bi bi-meta"></i> Meta ({{ config_urls|length }})
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
                <div class="tab-pane fade show active" id="urls-tab-meta">
                <form action="/guardar_urls_txt" method="POST">
                    <div class="d-flex justify-content-between align-items-center mb-2">
                        <span class="small text-muted">Formato: <code>Nombre en Panel | Nombre Búsqueda Bot | URL</code><br>Recomendado: URL de la página del anunciante (con <code>view_all_page_id</code>), así los anuncios retirados se detectan con precisión.</span>
                        <span class="badge bg-secondary-subtle text-secondary">{{ config_urls|length }} enlaces detectados</span>
                    </div>
                    
                    <textarea name="raw_urls" class="form-control form-control-sm font-monospace mb-3 bg-dark text-light border-secondary" rows="10" placeholder="Nombre en Panel | Nombre en Meta | https://www.facebook.com/ads/library/?..." {% if not es_admin %}readonly{% endif %}>{{ raw_urls_content }}</textarea>
                    
                    <div class="d-flex justify-content-between align-items-center">
                        <small class="text-secondary"><i class="bi bi-github"></i> Se sincronizará directamente con el archivo <code>urls.txt</code> de tu repositorio.</small>
                        {% if es_admin %}
                        <button type="submit" class="btn btn-sm btn-primary px-3"><i class="bi bi-cloud-arrow-up"></i> Guardar en GitHub</button>
                        {% else %}
                        <span class="small text-warning"><i class="bi bi-lock-fill"></i> Solo el Administrador puede modificar las URLs</span>
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
                    <textarea name="raw_productos" class="form-control form-control-sm font-monospace mb-2 bg-dark text-light border-secondary" rows="10" {% if not es_admin %}readonly{% endif %}>{{ raw_productos_content }}</textarea>
                    <p class="small text-muted mb-3">Cada anuncio se clasifica según las palabras de su texto (sin importar mayúsculas ni tildes); puede tener varias categorías. Con <code>*</code> al final se incluyen variantes: <code>factur*</code> = factura, facturación, facturar. Los anuncios de Google no traen texto, por eso quedan "Sin clasificar".</p>
                    <div class="d-flex justify-content-between align-items-center">
                        <small class="text-secondary"><i class="bi bi-github"></i> Se sincronizará con el archivo <code>productos.txt</code> de tu repositorio.</small>
                        {% if es_admin %}
                        <button type="submit" class="btn btn-sm btn-primary px-3"><i class="bi bi-cloud-arrow-up"></i> Guardar en GitHub</button>
                        {% else %}
                        <span class="small text-warning"><i class="bi bi-lock-fill"></i> Solo el Administrador puede modificar las categorías</span>
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

                    <textarea name="raw_urls" class="form-control form-control-sm font-monospace mb-2 bg-dark text-light border-secondary" rows="8" placeholder="Nombre en Panel | galac.com" {% if not es_admin %}readonly{% endif %}>{{ raw_google_content }}</textarea>
                    <p class="small text-muted mb-3">Usa el mismo "Nombre en Panel" que en la pestaña Meta para que los anuncios de ambas fuentes se agrupen en la misma empresa. Se buscan los anuncios mostrados en Venezuela en el Centro de Transparencia de Anuncios de Google.</p>

                    <div class="d-flex justify-content-between align-items-center">
                        <small class="text-secondary"><i class="bi bi-github"></i> Se sincronizará con el archivo <code>urls_google.txt</code> de tu repositorio.</small>
                        {% if es_admin %}
                        <button type="submit" class="btn btn-sm btn-primary px-3"><i class="bi bi-cloud-arrow-up"></i> Guardar en GitHub</button>
                        {% else %}
                        <span class="small text-warning"><i class="bi bi-lock-fill"></i> Solo el Administrador puede modificar las URLs</span>
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
                <label class="form-label small fw-semibold text-muted mb-1">{% set todas_companias = es_beta and lista_companias and lista_companias|reject('in', companias_sel)|list|length == 0 %}
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
                            <input class="form-check-input" type="checkbox" id="selectAllCompanies" onchange="toggleAllCompanies(this)" {% if todas_companias %}checked{% endif %}>
                            <label class="form-check-label small fw-bold" for="selectAllCompanies">Seleccionar Todo</label>
                        </div>
                        {% for comp in lista_companias %}
                        <div class="form-check">
                            <input class="form-check-input comp-checkbox" type="checkbox" name="compania" value="{{ comp }}" id="comp_{{ loop.index }}" {% if comp in companias_sel %}checked{% endif %}>
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
                            <input class="form-check-input" type="checkbox" id="selectAllProductos" onchange="document.querySelectorAll('.prod-checkbox').forEach(cb => cb.checked = this.checked)" {% if todos_productos %}checked{% endif %}>
                            <label class="form-check-label small fw-bold" for="selectAllProductos">Seleccionar Todo</label>
                        </div>
                        {% for p in lista_productos %}
                        <div class="form-check">
                            <input class="form-check-input prod-checkbox" type="checkbox" name="producto" value="{{ p }}" id="prod_{{ loop.index }}" {% if p in productos_sel %}checked{% endif %}>
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
                                    <button type="button" class="btn btn-outline-secondary" onclick="setTimelineModo('actual', this)" title="Anuncios que siguen publicados en Meta o Google">{{ 'Vigentes' if es_beta else 'Actuales' }}</button>
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

            <!-- Tabla de Empresas Registradas -->
            <div class="card-custom overflow-hidden reveal-scroll">
                <div class="p-3 bg-primary bg-opacity-10 border-bottom d-flex align-items-center justify-content-between">
                    <div>
                        <h6 class="fw-bold text-primary mb-1"><i class="bi bi-buildings"></i> Empresas Monitoreadas y Volumen de {{ 'Anuncios' if es_beta else 'Creatividades' }}</h6>
                        <p class="small text-muted mb-0">{% if es_beta %}Anuncios que cumplen los filtros seleccionados, divididos por formato para cada marca.{% else %}Total de creatividades almacenadas en el sistema divididas por formato para cada marca.{% endif %}</p>
                    </div>
                    <span class="badge bg-primary fs-6">{{ stats_empresas|length }} empresa{{ 's' if stats_empresas|length != 1 }}</span>
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
                        <tbody>
                            {% for emp in stats_empresas %}
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
                                    <a href="/?compania={{ emp.compania|urlencode }}{% if es_beta %}&pestana=tab-ads{% endif %}" class="btn btn-sm btn-outline-primary py-0 px-2" style="font-size: 0.75rem;" title="Filtrar anuncios de esta empresa">
                                        <i class="bi bi-funnel"></i> {{ 'Ver Anuncios' if es_beta else 'Ver Creatividades' }}
                                    </a>
                                </td>
                            </tr>
                            {% else %}
                            <tr>
                                <td colspan="7" class="text-center py-5 text-muted">
                                    <i class="bi bi-folder-x fs-2 d-block mb-2"></i> {{ 'No hay empresas con anuncios para los filtros seleccionados.' if es_beta else 'No hay estadísticas de empresas disponibles.' }}
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
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
                                <td colspan="8" class="text-center py-5 text-muted">
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
                                <td colspan="8" class="text-center py-5 text-muted">
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
                                <td colspan="8" class="text-center py-5 text-muted">
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
                                <td colspan="7" class="text-center py-5 text-muted">
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
                        <h6 class="fw-bold mb-3"><i class="bi bi-bar-chart"></i> Top Palabras Clave más Usadas</h6>
                        <div style="height: 360px;">
                            <canvas id="keywordsChart"></canvas>
                        </div>
                    </div>
                </div>
                <div class="col-lg-5">
                    <div class="card-custom p-3">
                        <h6 class="fw-bold mb-3"><i class="bi bi-tags"></i> Frecuencia de Términos</h6>
                        <div class="table-responsive" style="max-height: 360px; overflow-y: auto;">
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

        [['#selectAllCompanies', '.comp-checkbox'], ['#selectAllProductos', '.prod-checkbox']].forEach(([todo, cada]) => {
            const maestra = document.querySelector(todo);
            const casillas = document.querySelectorAll(cada);
            if (!maestra) return;
            casillas.forEach(cb => cb.addEventListener('change', () => {
                maestra.checked = [...casillas].every(c => c.checked);
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

    if es_beta:
        stats_empresas = estadisticas_por_empresa(anuncios)

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
    for comp in lista_companias:
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
    top_palabras = contador_palabras.most_common(12)

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
        msg=msg
    )

@app.route('/guardar_urls_txt', methods=['POST'])
@admin_required
def guardar_urls_txt():
    raw_urls = request.form.get('raw_urls', '').strip()
    success, message = update_github_urls_file(raw_urls)
    if success:
        return redirect(url_for('index', msg="✅ Archivo urls.txt guardado en GitHub exitosamente."))
    else:
        return redirect(url_for('index', msg=f"❌ {message}"))

@app.route('/guardar_urls_google', methods=['POST'])
@admin_required
def guardar_urls_google():
    raw_urls = request.form.get('raw_urls', '').strip()
    success, message = update_github_urls_file(raw_urls, URLS_GOOGLE_FILE_PATH)
    if success:
        return redirect(url_for('index', msg="✅ Archivo urls_google.txt guardado en GitHub exitosamente."))
    return redirect(url_for('index', msg=f"❌ {message}"))

@app.route('/guardar_productos', methods=['POST'])
@admin_required
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
        'Enlace': [r.get('link_individual') or '' for r in filas],
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
