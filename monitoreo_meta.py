import os
import sys
import time
import json
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
import psycopg2
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
import registro_bots

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

load_dotenv()
DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL and "sslmode=" not in DATABASE_URL:
    separador = "&" if "?" in DATABASE_URL else "?"
    DATABASE_URL = f"{DATABASE_URL}{separador}sslmode=require"

JSON_FILE = "anuncios_guardados.json"
# Resumen de la corrida para el panel de salud; se crea en main().
CORRIDA = None
PROGRESO_FILE = "progreso.json"

MESES_MAP = {
    'ene': 1, 'enero': 1, 'jan': 1, 'january': 1,
    'feb': 2, 'febrero': 2, 'february': 2,
    'mar': 3, 'marzo': 3, 'march': 3,
    'abr': 4, 'abril': 4, 'apr': 4, 'april': 4,
    'may': 5, 'mayo': 5,
    'jun': 6, 'junio': 6, 'june': 6,
    'jul': 7, 'julio': 7, 'july': 7,
    'ago': 8, 'agosto': 8, 'aug': 8, 'august': 8,
    'sep': 9, 'septiembre': 9, 'sept': 9, 'september': 9,
    'oct': 10, 'octubre': 10, 'october': 10,
    'nov': 11, 'noviembre': 11, 'november': 11,
    'dic': 12, 'diciembre': 12, 'dec': 12, 'december': 12
}

PLATAFORMAS_META = {
    'FACEBOOK': 'Facebook',
    'INSTAGRAM': 'Instagram',
    'AUDIENCE_NETWORK': 'Audience Network',
    'MESSENGER': 'Messenger',
    'THREADS': 'Threads',
    'WHATSAPP': 'WhatsApp',
}
FORMATOS_META = {
    'VIDEO': 'Video',
    'IMAGE': 'Foto',
    'CAROUSEL': 'Carrusel',
    'DCO': 'Dinámico',
}

def recolectar_oficiales(obj, destino):
    # Meta incluye los datos oficiales de cada anuncio (plataformas, estado, fechas, formato,
    # texto) en el JSON de la página y de las respuestas GraphQL al hacer scroll.
    if isinstance(obj, dict):
        if 'ad_archive_id' in obj and 'publisher_platform' in obj:
            destino[str(obj['ad_archive_id'])] = obj
        for v in obj.values():
            recolectar_oficiales(v, destino)
    elif isinstance(obj, list):
        for v in obj:
            recolectar_oficiales(v, destino)

def oficiales_desde_html(html):
    destino = {}
    for bloque in re.findall(r'<script type="application/json"[^>]*>(.*?)</script>', html, re.S):
        try:
            recolectar_oficiales(json.loads(bloque), destino)
        except Exception:
            pass
    return destino

def texto_oficial(oficial):
    # Los anuncios dinámicos traen una plantilla ({{product.brand}}) en body y el texto real en cards.
    snap = oficial.get('snapshot') or {}
    candidatos = [(snap.get('body') or {}).get('text')]
    candidatos += [c.get('body') for c in (snap.get('cards') or [])]
    candidatos.append(snap.get('title'))
    for t in candidatos:
        if isinstance(t, str) and t.strip() and '{{' not in t:
            return re.sub(r'\s+', ' ', t).strip()
    return None

def imagen_oficial(oficial):
    # Miniatura del video o imagen del anuncio. Las URLs de Meta caducan en unos días; cada
    # corrida las renueva para los anuncios que siguen activos.
    sn = (oficial or {}).get("snapshot") or {}
    for video in sn.get("videos") or []:
        if video.get("video_preview_image_url"):
            return video["video_preview_image_url"]
    for imagen in sn.get("images") or []:
        url = imagen.get("resized_image_url") or imagen.get("original_image_url")
        if url:
            return url
    for card in sn.get("cards") or []:
        url = card.get("resized_image_url") or card.get("video_preview_image_url") or card.get("original_image_url")
        if url:
            return url
    return None


def destino_oficial(oficial):
    # A dónde lleva el anuncio, según el botón (cta_type) y el enlace: WhatsApp, Messenger, web...
    sn = (oficial or {}).get("snapshot") or {}
    cards = sn.get("cards") or []
    cta = (sn.get("cta_type") or next((c.get("cta_type") for c in cards if c.get("cta_type")), "") or "").upper()
    link = sn.get("link_url") or next((c.get("link_url") for c in cards if c.get("link_url")), None)
    host = ""
    if link:
        host = urllib.parse.urlparse(link).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
    if "WHATSAPP" in cta or host in ("wa.me", "api.whatsapp.com", "whatsapp.com", "chat.whatsapp.com", "wa.link"):
        return "WhatsApp", link
    if cta == "INSTAGRAM_MESSAGE" or host == "ig.me":
        return "Instagram (mensaje)", link
    if cta in ("MESSAGE_PAGE", "SEND_MESSAGE") or host in ("m.me", "messenger.com"):
        return "Messenger", link
    if cta == "CALL_NOW" or (link or "").startswith("tel:"):
        return "Llamada", link
    if cta in ("SIGN_UP", "APPLY_NOW", "GET_QUOTE", "SUBSCRIBE", "REQUEST_TIME", "GET_OFFER") and host in ("", "fb.me", "facebook.com"):
        return "Formulario", link
    if host.endswith("instagram.com"):
        return "Instagram (perfil)", link
    if host.endswith("facebook.com") or host == "fb.me":
        return "Facebook", link
    if host:
        return "Sitio web", link
    return "Sin enlace", None


def actualizar_progreso(activo=True, actual=0, total=0, empresa="", porcentaje=0, finalizado=False):
    datos = {
        "activo": activo,
        "actual": actual,
        "total": total,
        "empresa": empresa,
        "porcentaje": porcentaje,
        "finalizado": finalizado,
        "timestamp": time.time()
    }
    try:
        with open(PROGRESO_FILE, "w", encoding="utf-8") as f:
            json.dump(datos, f)
    except Exception:
        pass

def normalizar_fecha_texto(texto_fecha):
    if not texto_fecha:
        return None
    txt = texto_fecha.lower().strip()

    m1 = re.search(r'(\d{1,2})\s+(?:de\s+)?([a-záéíóú]+)\.?\s+(?:de\s+)?(\d{4})', txt)
    if m1:
        dia = int(m1.group(1))
        mes_txt = m1.group(2)[:3]
        mes = MESES_MAP.get(mes_txt, 1)
        ano = int(m1.group(3))
        return f"{ano:04d}-{mes:02d}-{dia:02d}"

    m2 = re.search(r'([a-záéíóú]+)\s+(\d{1,2}),?\s+(\d{4})', txt)
    if m2:
        mes_txt = m2.group(1)[:3]
        mes = MESES_MAP.get(mes_txt, 1)
        dia = int(m2.group(2))
        ano = int(m2.group(3))
        return f"{ano:04d}-{mes:02d}-{dia:02d}"

    m3 = re.search(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})', txt)
    if m3:
        y, m, d = m3.groups()
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"

    return None

def inicializar_bd():
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS anuncios (
                id SERIAL PRIMARY KEY,
                id_anuncio VARCHAR(100),
                compania VARCHAR(255),
                fecha_subida VARCHAR(100),
                estado VARCHAR(100),
                plataformas VARCHAR(255),
                formato VARCHAR(100),
                duracion_segundos INT DEFAULT 0,
                titulo TEXT,
                link_individual TEXT UNIQUE,
                fecha_registro TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS companias_bloqueadas (
                id SERIAL PRIMARY KEY,
                compania VARCHAR(255) UNIQUE NOT NULL,
                fecha_bloqueo TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("ALTER TABLE anuncios ADD COLUMN IF NOT EXISTS presente_en_meta BOOLEAN DEFAULT TRUE;")
        cur.execute("ALTER TABLE anuncios ADD COLUMN IF NOT EXISTS fuente VARCHAR(20) DEFAULT 'Meta';")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS anuncios_link_idx ON anuncios (link_individual);")
        registro_bots.crear_tablas(cur)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"❌ Error BD Init: {e}")

def obtener_companias_bloqueadas():
    bloqueadas = set()
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cur = conn.cursor()
        cur.execute("SELECT compania FROM companias_bloqueadas;")
        for r in cur.fetchall():
            if r[0]:
                bloqueadas.add(r[0].strip().lower())
        cur.close()
        conn.close()
    except Exception:
        pass
    return bloqueadas

def limpiar_texto_copy(texto, empresa):
    if not texto:
        return f"Anuncio de {empresa}"

    patrones_a_remover = [
        r'(?i)^copy:\s*',
        r'(?i)\bplatforms?\s+open\s+dropdown(?:\s+menu)?\b',
        r'(?i)\bplataformas\s+abrir\s+men[uú]\s+desplegable\b',
        r'(?i)\babrir\s+men[uú]\s+desplegable\b',
        r'(?i)\bopen\s+dropdown\s+menu\b',
        r'(?i)\bplataformas\b',
        r'(?i)\bver\s+detalles\s+del\s+anuncio\b',
        r'(?i)\bver\s+detalles\s+del\s+resumen\b',
        r'(?i)\bver\s+detalles\b',
        r'(?i)\bsee\s+ad\s+details\b',
        r'(?i)\b\d+\s+anuncios?\s+usan\s+este\s+contenido\s+y\s+texto\b',
        r'(?i)\beste\s+anuncio\s+tiene\s+varias\s+versiones\b',
        r'(?i)\bthis\s+ad\s+has\s+multiple\s+versions\b',
        r'(?i)\b\d+\s+ads?\s+uses?\s+this\s+creative\s+and\s+text\b',
        r'(?i)\bsee\s+summary\s+details\b',
        r'\b\d{1,2}:\d{2}\s*/\s*\d{1,2}:\d{2}\b',
        r'(?i)\bidentificador\s+de\s+la\s+biblioteca(?:\s+[uú]nico)?:\s*\d+\b',
        r'(?i)\ben\s+circulaci[oó]n\s+desde\s+(?:el\s+)?[^\n·•]+',
        r'(?i)\bstarted\s+running\s+on\s+[^\n·•]+',
        r'(?i)\b(m[aá]s\s+informaci[oó]n|enviar\s+mensaje|contactar|comprar|registrarse|descargar|solicitar|ver\s+m[aá]s|apply\s+now|learn\s+more|send\s+message|sign\s+up|shop\s+now)\b',
        r'(?i)\b(activo|inactivo|active|inactive|sponsored|publicidad)\b'
    ]

    limpio = texto
    for p in patrones_a_remover:
        limpio = re.sub(p, '', limpio)

    limpio = re.sub(r'\s+', ' ', limpio).strip()
    return limpio if len(limpio) > 5 else f"Anuncio de {empresa}"

def guardar_anuncio(anuncio, bloqueadas):
    empresa_nom = anuncio.get('compania', '').strip()
    if empresa_nom.lower() in bloqueadas:
        return False

    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cur = conn.cursor()

        ad_id = anuncio.get('id_anuncio')
        if not ad_id and 'id=' in anuncio['link_individual']:
            ad_id = anuncio['link_individual'].split('id=')[-1]

        query = """
            INSERT INTO anuncios (id_anuncio, compania, fecha_subida, estado, plataformas, formato, duracion_segundos, titulo, link_individual, presente_en_meta, imagen_url, destino, destino_url)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, %s, %s, %s)
            ON CONFLICT (link_individual) DO UPDATE
            SET id_anuncio = COALESCE(NULLIF(EXCLUDED.id_anuncio, ''), NULLIF(anuncios.id_anuncio, '')),
                compania = EXCLUDED.compania,
                fecha_subida = COALESCE(NULLIF(EXCLUDED.fecha_subida, ''), NULLIF(anuncios.fecha_subida, '')),
                estado = EXCLUDED.estado,
                plataformas = COALESCE(NULLIF(EXCLUDED.plataformas, ''), anuncios.plataformas),
                formato = COALESCE(NULLIF(EXCLUDED.formato, ''), anuncios.formato),
                duracion_segundos = CASE 
                    WHEN EXCLUDED.duracion_segundos > 0 THEN EXCLUDED.duracion_segundos 
                    ELSE COALESCE(anuncios.duracion_segundos, 0)
                END,
                titulo = CASE
                    WHEN COALESCE(EXCLUDED.titulo, '') = '' OR EXCLUDED.titulo LIKE 'Anuncio de %%'
                    THEN COALESCE(NULLIF(anuncios.titulo, ''), EXCLUDED.titulo)
                    ELSE EXCLUDED.titulo
                END,
                presente_en_meta = TRUE,
                imagen_url = COALESCE(NULLIF(EXCLUDED.imagen_url, ''), anuncios.imagen_url),
                destino = COALESCE(NULLIF(EXCLUDED.destino, ''), anuncios.destino),
                destino_url = COALESCE(NULLIF(EXCLUDED.destino_url, ''), anuncios.destino_url)
            RETURNING id;
        """
        cur.execute(query, (
            str(ad_id) if ad_id else None,
            anuncio['compania'],
            anuncio['fecha_subida'],
            anuncio['estado'],
            anuncio['plataformas'],
            anuncio['formato'],
            anuncio['duracion_segundos'],
            anuncio['titulo'],
            anuncio['link_individual'],
            anuncio.get('imagen_url'),
            anuncio.get('destino'),
            anuncio.get('destino_url'),
        ))
        res = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()

        if res and os.path.exists(JSON_FILE):
            try:
                with open(JSON_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, list):
                    data = []
                data = [x for x in data if x.get("link_individual") != anuncio["link_individual"]]
                data.append(anuncio)
                with open(JSON_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4, ensure_ascii=False)
            except Exception:
                pass

        return True if res else False
    except Exception as e:
        print(f"  ⚠️ Error BD: {e}")
        return False

def marcar_no_detectados(compania, links_encontrados, fecha_desde=None):
    # Solo se evalúan anuncios que caían dentro de la ventana buscada: un anuncio más viejo
    # que fecha_desde no sale en la búsqueda aunque siga activo.
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cur = conn.cursor()
        cur.execute("""
            UPDATE anuncios
            SET presente_en_meta = FALSE
            WHERE compania = %s AND COALESCE(fuente, 'Meta') = 'Meta'
              AND link_individual != ALL(%s)
              AND presente_en_meta IS DISTINCT FROM FALSE
              AND (%s IS NULL OR fecha_subida >= %s);
        """, (compania, links_encontrados, fecha_desde, fecha_desde))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"  ⚠️ Error actualizando anuncios retirados de Meta: {e}")

def marcar_retirados_meta(compania, links_vigentes):
    # En Venezuela Meta solo muestra anuncios activos: si una búsqueda por página trajo la
    # lista completa, todo anuncio guardado que ya no aparece fue apagado.
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cur = conn.cursor()
        cur.execute("""
            UPDATE anuncios
            SET presente_en_meta = FALSE, estado = 'Inactivo'
            WHERE compania = %s AND COALESCE(fuente, 'Meta') = 'Meta'
              AND link_individual != ALL(%s)
              AND (presente_en_meta IS DISTINCT FROM FALSE OR estado IS DISTINCT FROM 'Inactivo');
        """, (compania, list(links_vigentes)))
        print(f"  🗂️ {compania}: {cur.rowcount} anuncios marcados como retirados de Meta.")
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"  ⚠️ Error marcando anuncios retirados de Meta: {e}")

def es_busqueda_por_pagina(url):
    return 'view_all_page_id=' in url

def preparar_url_completa(url_base, dias_atras=30):
    parsed = urllib.parse.urlparse(url_base)
    params = urllib.parse.parse_qs(parsed.query)
    params['active_status'] = ['all']
    params['ad_type'] = ['all']

    # Las búsquedas por página traen todos los anuncios activos del anunciante; la ventana de
    # días solo hace falta en las búsquedas por palabra clave, que mezclan anunciantes ajenos.
    if dias_atras and not es_busqueda_por_pagina(url_base):
        fecha_hasta = datetime.today().strftime('%Y-%m-%d')
        fecha_desde = (datetime.today() - timedelta(days=dias_atras)).strftime('%Y-%m-%d')
        params['start_date[min]'] = [fecha_desde]
        params['start_date[max]'] = [fecha_hasta]

    flat_params = {k: v[0] if isinstance(v, list) else v for k, v in params.items()}
    query_str = urllib.parse.urlencode(flat_params)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, query_str, parsed.fragment))

def extraer_duracion_json_profundo(obj):
    if isinstance(obj, dict):
        for clave in [
            'duration_in_ms', 'playable_duration_in_ms', 'video_duration', 
            'video_play_time_in_seconds', 'length_in_sec', 'duration', 
            'reels_duration', 'length', 'video_length'
        ]:
            if clave in obj and obj[clave]:
                try:
                    val = float(obj[clave])
                    if val > 1000:
                        return round(val / 1000)
                    elif val > 0:
                        return int(val)
                except Exception:
                    pass
        for v in obj.values():
            d = extraer_duracion_json_profundo(v)
            if d > 0:
                return d
    elif isinstance(obj, list):
        for item in obj:
            d = extraer_duracion_json_profundo(item)
            if d > 0:
                return d
    return 0

def extraer_anuncios(page, nombre_flask, nombre_bot, url_final, bloqueadas, fecha_desde=None):
    print(f"\n🌐 Abriendo: {url_final}")
    print(f"🎯 Monitoreando: '{nombre_flask}' (Búsqueda bot: '{nombre_bot}')")

    por_pagina = es_busqueda_por_pagina(url_final)
    oficiales = {}
    duraciones_api = {}
    limitado = {"rate_limit": False, "veces": 0}
    con_miniatura = 0

    def interceptar_red(response):
        if any(w in response.url for w in ["graphql", "api", "ad_library"]):
            try:
                texto = response.text()
                if texto.startswith("for (;;);"):
                    texto = texto[len("for (;;);"):]
                if "Rate limit exceeded" in texto:
                    limitado["rate_limit"] = True
                    limitado["veces"] += 1

                for linea in texto.splitlines():
                    linea = linea.strip()
                    if not linea:
                        continue
                    try:
                        data = json.loads(linea)
                    except Exception:
                        continue
                    nuevos = {}
                    recolectar_oficiales(data, nuevos)
                    if nuevos:
                        # Llegaron anuncios: si antes hubo límite, Meta ya se recuperó.
                        limitado["rate_limit"] = False
                    oficiales.update(nuevos)
                    for ad_id, obj in nuevos.items():
                        dur = extraer_duracion_json_profundo(obj)
                        if dur > 0:
                            duraciones_api[ad_id] = dur
            except Exception:
                pass

    page.on("response", interceptar_red)
    page.goto(url_final, wait_until="domcontentloaded", timeout=90000)
    time.sleep(3)

    try:
        btn_cookie = page.locator("button:has-text('Permitir'), button:has-text('Allow'), button:has-text('Aceptar')").first
        if btn_cookie.is_visible(timeout=2500):
            btn_cookie.click()
            time.sleep(1)
    except Exception:
        pass

    print("📜 Desplazando y cargando creatividades de Meta...")
    if por_pagina:
        cargar_todos_los_anuncios(page, url_final, oficiales, limitado)
    else:
        prev_ads_count = 0
        intentos_sin_cambio = 0
        for _ in range(25):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1.5)

            current_ads = page.locator("text=/Identificador de la biblioteca|Library ID|ID:/i").count()
            if current_ads > prev_ads_count:
                prev_ads_count = current_ads
                intentos_sin_cambio = 0
            else:
                intentos_sin_cambio += 1
                if intentos_sin_cambio >= 4:
                    break

    datos_anuncios = page.evaluate(r"""(nombreBuscado) => {
        const resultados = [];
        
        const elementosTexto = Array.from(document.querySelectorAll('*')).filter(el => {
            const txt = el.innerText || '';
            return /(?:Identificador de la biblioteca|Library ID|ID):\s*\d{10,}/i.test(txt) && el.children.length === 0;
        });

        elementosTexto.forEach(elemId => {
            const txtId = elemId.innerText || '';
            const matchId = txtId.match(/(?:Identificador de la biblioteca|Library ID|ID):\s*(\d{10,})/i);
            if (!matchId) return;
            const adId = matchId[1];

            // Localizar tarjeta contenedora
            let card = elemId;
            while (card && card.parentElement && card.parentElement !== document.body) {
                const rect = card.parentElement.getBoundingClientRect();
                const htmlP = card.parentElement.innerHTML || '';
                if (rect.height > 260 && rect.width > 220 && rect.width < 950 && (htmlP.includes('Publicidad') || htmlP.includes('Sponsored') || htmlP.includes('Ver detalles'))) {
                    card = card.parentElement;
                } else if (rect.height > 260 && rect.width >= 950) {
                    break;
                } else {
                    card = card.parentElement;
                }
            }
            if (!card) return;

            const cardText = card.innerText || '';
            const lineas = cardText.split('\n').map(l => l.trim()).filter(l => l.length > 0);

            // 1. ESTADO
            let estadoAnuncio = "Activo";
            const textoSuperior = cardText.substring(0, 250);
            if (/\b(?:inactivo|inactive)\b/i.test(textoSuperior)) {
                estadoAnuncio = "Inactivo";
            } else if (/\b(?:activo|active)\b/i.test(textoSuperior)) {
                estadoAnuncio = "Activo";
            }

            // 2. Las plataformas salen de los datos oficiales (publisher_platform), no de los
            // iconos: Meta muestra un solo icono aunque el anuncio corra en varias redes.

            // 3. FECHA DE PUBLICACIÓN
            let fechaTexto = "";
            const matchFecha = cardText.match(/(?:En circulaci[oó]n desde(?: el)?|Started running on)\s*:?\s*([^\n·•]+)/i);
            if (matchFecha) {
                fechaTexto = matchFecha[1].trim();
            }

            // 4. EMPRESA ANUNCIANTE (IGNORANDO 'Platforms open dropdown')
            let empresa = nombreBuscado;
            const idxPubli = lineas.findIndex(l => /^(?:publicidad|sponsored)$/i.test(l));
            if (idxPubli > 0) {
                // Revisar líneas previas descartando botones de dropdown o etiquetas de Meta
                for (let i = idxPubli - 1; i >= 0; i--) {
                    const candidate = lineas[i];
                    const candLow = candidate.toLowerCase();
                    if (
                        candLow.includes('platform') || candLow.includes('plataforma') ||
                        candLow.includes('dropdown') || candLow.includes('desplegable') ||
                        candLow.includes('identificador') || candLow.includes('library id') ||
                        candLow.includes('circulación') || candLow.includes('running on') ||
                        candLow.includes('activo') || candLow.includes('inactive') ||
                        candLow.includes('versiones') || candLow.includes('contenido y texto')
                    ) {
                        continue;
                    }
                    if (candidate.length >= 2) {
                        empresa = candidate;
                        break;
                    }
                }
            }

            // 5. FORMATO Y DURACIÓN
            let duracionSegundos = 0;
            const videoEl = card.querySelector('video');
            const playButton = card.querySelector('[aria-label*="reproducir" i], [aria-label*="play" i], [aria-label*="video" i], svg polygon, svg path[d*="M8"], svg path[d*="M5"]');
            const hasVideo = videoEl !== null || 
                             playButton !== null ||
                             /\b\d{1,2}:\d{2}\b/.test(cardText) || 
                             card.innerHTML.includes('video/mp4') || 
                             card.innerHTML.includes('blob:');

            if (videoEl && videoEl.duration && !isNaN(videoEl.duration) && videoEl.duration > 0) {
                duracionSegundos = Math.round(videoEl.duration);
            }

            if (duracionSegundos === 0) {
                const matchTime = cardText.match(/\b(\d{1,2}):(\d{2})\b/);
                if (matchTime) {
                    duracionSegundos = (parseInt(matchTime[1], 10) * 60) + parseInt(matchTime[2], 10);
                }
            }

            // 6. TEXTO / COPY DEL ANUNCIO
            let rawCopy = "";
            const copyContainers = Array.from(card.querySelectorAll('div[style*="white-space: pre-wrap"], div[dir="auto"], span[dir="auto"]'));
            for (const c of copyContainers) {
                const txt = (c.innerText || '').trim();
                const low = txt.toLowerCase();
                if (
                    txt.length > 15 &&
                    !/^\d{1,2}:\d{2}\s*\/\s*\d{1,2}:\d{2}$/.test(txt) &&
                    !low.includes('identificador') && !low.includes('library id') &&
                    !low.includes('en circulación') && !low.includes('started running') &&
                    !low.includes('plataformas') && !low.includes('platform') &&
                    !low.includes('ver detalles') && !low.includes('dropdown') &&
                    txt !== empresa
                ) {
                    rawCopy = txt;
                    break;
                }
            }

            if (!rawCopy) {
                const parrafos = lineas.filter(l => {
                    const low = l.toLowerCase();
                    return !(
                        low.includes('identificador') || low.includes('library id') ||
                        low.includes('en circulación') || low.includes('started running') ||
                        low.includes('activo') || low.includes('inactivo') ||
                        low.includes('publicidad') || low.includes('sponsored') ||
                        low.includes('ver detalles') || low.includes('plataformas') ||
                        low.includes('platform') || low.includes('dropdown') ||
                        low.includes('usan este contenido') ||
                        low.includes('versiones') || l === empresa || l.length <= 5
                    );
                });
                rawCopy = parrafos.slice(0, 3).join(' ');
            }

            resultados.push({
                id: adId,
                empresa: empresa,
                estado: estadoAnuncio,
                fechaTexto: fechaTexto,
                esVideo: hasVideo,
                duracion: duracionSegundos,
                copy: rawCopy || `Anuncio de ${nombreBuscado}`
            });
        });

        return resultados;
    }""", nombre_bot)

    oficiales.update(oficiales_desde_html(page.content()))

    # Las variantes agrupadas ("2 ads use this creative") se muestran en una sola tarjeta pero
    # son anuncios activos distintos: se procesan desde los datos oficiales.
    ids_dom = {str(i["id"]) for i in datos_anuncios}
    for ad_id, oficial in oficiales.items():
        if ad_id not in ids_dom:
            datos_anuncios.append({"id": ad_id, "empresa": oficial.get("page_name") or nombre_bot, "estado": "",
                                   "fechaTexto": "", "esVideo": False, "duracion": 0, "copy": ""})

    vistos = set()
    guardados = 0
    sin_datos_oficiales = 0
    links_encontrados = []

    for item in datos_anuncios:
        ad_id = item["id"]
        if ad_id in vistos:
            continue
        vistos.add(ad_id)

        oficial = oficiales.get(ad_id)
        if not oficial:
            sin_datos_oficiales += 1

        empresa_actual = ((oficial or {}).get("page_name") or item["empresa"]).strip()
        busq_limpia = re.sub(r'[^\w\s]', '', nombre_bot.lower()).strip()
        actual_limpia = re.sub(r'[^\w\s]', '', empresa_actual.lower()).strip()

        es_valida = (busq_limpia in actual_limpia) or (actual_limpia in busq_limpia) or len(actual_limpia) == 0 or len(busq_limpia) == 0
        # En una búsqueda por página todos los anuncios son de ese anunciante, aunque el
        # nombre de la página no coincida con el nombre en el panel.
        if not por_pagina and not es_valida:
            continue

        if nombre_flask.lower() in bloqueadas or empresa_actual.lower() in bloqueadas:
            continue

        duracion_final = item["duracion"]
        if duracion_final == 0:
            duracion_final = duraciones_api.get(ad_id, 0)

        if oficial:
            estado = "Activo" if oficial.get("is_active") else "Inactivo"
            inicio = oficial.get("start_date")
            fecha_final = (datetime.fromtimestamp(int(inicio), tz=timezone.utc).strftime('%Y-%m-%d')
                           if inicio else normalizar_fecha_texto(item.get("fechaTexto")))
            plataformas = ", ".join(PLATAFORMAS_META.get(p, p.replace('_', ' ').title())
                                    for p in (oficial.get("publisher_platform") or []))
            fmt = (oficial.get("snapshot") or {}).get("display_format") or ""
            formato = FORMATOS_META.get(fmt, fmt.title())
            titulo_definitivo = texto_oficial(oficial) or limpiar_texto_copy(item["copy"], nombre_flask)
        else:
            estado = item["estado"]
            fecha_final = normalizar_fecha_texto(item.get("fechaTexto"))
            plataformas = ""
            formato = "Video" if (item["esVideo"] or duracion_final > 0) else "Foto"
            titulo_definitivo = limpiar_texto_copy(item["copy"], nombre_flask)

        if not fecha_final:
            fecha_final = datetime.today().strftime('%Y-%m-%d')
        if formato != "Video":
            duracion_final = 0
        dur_txt = f"{duracion_final}s" if duracion_final > 0 else "-"

        link_individual = f"https://www.facebook.com/ads/library/?id={ad_id}"
        links_encontrados.append(link_individual)

        anuncio_data = {
            "id_anuncio": ad_id,
            "compania": nombre_flask,
            "fecha_subida": fecha_final,
            "estado": estado,
            "plataformas": plataformas,
            "formato": formato,
            "duracion_segundos": duracion_final,
            "titulo": titulo_definitivo[:400],
            "link_individual": link_individual,
            "imagen_url": imagen_oficial(oficial),
            "destino": destino_oficial(oficial)[0] if oficial else None,
            "destino_url": destino_oficial(oficial)[1] if oficial else None,
        }

        if guardar_anuncio(anuncio_data, bloqueadas):
            guardados += 1
            if anuncio_data["imagen_url"] and registro_bots.asegurar_miniatura(DATABASE_URL, link_individual, anuncio_data["imagen_url"]):
                con_miniatura += 1
            badge_estado = "🟢 Activo" if estado == "Activo" else "⚪ Inactivo"
            print(f"  ✨ [{formato}] [{badge_estado}] {nombre_flask} | Plat: {plataformas or '?'} | Dur: {dur_txt} | Fecha: {fecha_final} | Titulo: {titulo_definitivo[:40]}...")

    print(f"✅ Anuncios registrados para {nombre_flask}: {guardados}")
    if guardados:
        print(f"🖼️ Miniaturas guardadas: {con_miniatura} de {guardados}")
    if CORRIDA:
        CORRIDA.resultado(nombre_flask, guardados,
                          aviso=f"{sin_datos_oficiales} sin datos oficiales" if sin_datos_oficiales else None)
    if sin_datos_oficiales:
        print(f"  ⚠️ {sin_datos_oficiales} anuncios sin datos oficiales de Meta: se usaron los datos visibles de la página (plataformas desconocidas).")

    if por_pagina:
        completo = busqueda_completa(page, len(oficiales), limitado["rate_limit"])
        if CORRIDA and not completo:
            CORRIDA.resultado(nombre_flask, aviso="Búsqueda incompleta: no se marcaron retiros")
        return {"links": links_encontrados, "completo": completo}

    if datos_anuncios and nombre_flask.lower() not in bloqueadas:
        marcar_no_detectados(nombre_flask, links_encontrados, fecha_desde)
    return None

def total_resultados(page):
    # Contador que muestra Meta ("~59 results"); None si no aparece.
    try:
        cuerpo = page.inner_text("body")
    except Exception:
        return None
    if re.search(r"No ads match|No hay anuncios que coincidan", cuerpo, re.I):
        return 0
    m = re.search(r"~?\s*([\d.,]+)\s+(?:results?|resultados?)", cuerpo)
    return int(re.sub(r"[.,]", "", m.group(1))) if m else None


ESPERAS_LIMITE_META = [15, 30, 60]  # segundos de espera cuando Meta corta por "Rate limit"
RECARGAS_MAXIMAS = 2
SEMANAS_RESPALDO = 8  # cortes semanales cuando la paginación de Meta no deja cargar todo


def cargar_todos_los_anuncios(page, url, oficiales, limitado):
    """Búsqueda por página: sigue desplazando hasta tener el total que informa Meta. Si Meta corta por
    límite de solicitudes, espera y reintenta; si se queda corta, recarga la página (los anuncios ya
    recibidos se conservan, no se duplican)."""
    esperas = list(ESPERAS_LIMITE_META)
    recargas = RECARGAS_MAXIMAS
    total = total_resultados(page)
    previo, sin_cambio = len(oficiales), 0
    for _ in range(150):
        if total is not None and len(oficiales) >= total:
            break
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1.5)
        if total is None:
            total = total_resultados(page)
        actual = len(oficiales)
        if actual > previo:
            previo, sin_cambio = actual, 0
            continue
        sin_cambio += 1
        if limitado["rate_limit"] and esperas:
            espera = esperas.pop(0)
            print(f"  ⏳ Meta limitó las solicitudes ({actual} de ~{total or '?'} cargados); espero {espera} s y sigo...")
            time.sleep(espera)
            sin_cambio = 0
            continue
        if sin_cambio >= 4:
            if total and actual < total * 0.9 and recargas > 0:
                recargas -= 1
                print(f"  🔄 Se cargaron {actual} de ~{total}; recargo la página para seguir sumando...")
                time.sleep(10)
                page.goto(url, wait_until="domcontentloaded", timeout=90000)
                time.sleep(3)
                sin_cambio = 0
                continue
            break
    if total and len(oficiales) < total * 0.9:
        cargar_por_semanas(page, url, oficiales, total)
    print(f"  📦 Anuncios recibidos de Meta: {len(oficiales)} de ~{total if total is not None else '?'}"
          + (f" (hubo {limitado['veces']} cortes por límite)" if limitado["veces"] else ""))


def cargar_por_semanas(page, url, oficiales, total):
    """Respaldo: la primera página de cada búsqueda (30 anuncios) viene dentro de la página y no
    depende de la paginación que Meta limita. Filtrando por semanas en que el anuncio estuvo activo
    se ven otros grupos de anuncios. No garantiza llegar al total (en semanas con más de 30 activos
    solo se ven 30), pero suma lo que encuentre. Al final se vuelve a la búsqueda original para que
    el contador y la verificación de búsqueda completa usen el total real."""
    antes = len(oficiales)
    hasta = datetime.now(timezone.utc).date()
    for _ in range(SEMANAS_RESPALDO):
        desde = hasta - timedelta(days=6)
        try:
            page.goto(f"{url}&start_date[min]={desde}&start_date[max]={hasta}", wait_until="domcontentloaded", timeout=90000)
            time.sleep(3.5)
            oficiales.update(oficiales_desde_html(page.content()))
        except Exception as e:
            print(f"  ⚠️ No se pudo revisar la semana {desde}..{hasta}: {e}")
        if len(oficiales) >= total:
            break
        hasta = desde - timedelta(days=1)
    print(f"  🗓️ Revisión por semanas: +{len(oficiales) - antes} anuncios")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        time.sleep(3)
    except Exception:
        pass


def busqueda_completa(page, cargados, rate_limit):
    # Solo se confía en la búsqueda si Meta no cortó la paginación y lo cargado coincide con
    # su contador aproximado ("~N results"); si no, retirar anuncios sería un falso positivo.
    if rate_limit:
        print("  ⚠️ Meta limitó las solicitudes (rate limit): la lista quedó incompleta, no se marcan retiros.")
        return False
    cuerpo = page.inner_text("body")
    if re.search(r"No ads match|No hay anuncios que coincidan", cuerpo, re.I):
        return True
    m = re.search(r"~?\s*([\d.,]+)\s+(?:results?|resultados?)", cuerpo)
    if not m:
        print("  ⚠️ No se encontró el contador de resultados de Meta: no se marcan retiros.")
        return False
    total = int(re.sub(r"[.,]", "", m.group(1)))
    if cargados < int(total * 0.9):
        print(f"  ⚠️ Se cargaron {cargados} de ~{total} anuncios: lista incompleta, no se marcan retiros.")
        return False
    return True

def main():
    global CORRIDA
    CORRIDA = registro_bots.Corrida("Meta", DATABASE_URL)
    try:
        ejecutar()
    except Exception as e:
        print(f"❌ Falla general del bot de Meta: {e}")
        CORRIDA.terminar(fallo=e)
        raise
    CORRIDA.terminar()


def ejecutar():
    inicializar_bd()
    bloqueadas = obtener_companias_bloqueadas()

    dias = 30
    if len(sys.argv) > 1:
        dias = None if sys.argv[1] == "todo" else int(sys.argv[1])

    if not os.path.exists("urls.txt"):
        print("❌ Error: No se encontró 'urls.txt'.")
        actualizar_progreso(activo=False, finalizado=True)
        return

    entradas = []
    with open("urls.txt", "r", encoding="utf-8") as f:
        for linea in f:
            l = linea.strip()
            if not l or l.startswith("#"):
                continue
            partes = [p.strip() for p in l.split("|")]
            if len(partes) >= 3:
                entradas.append((partes[0], partes[1], partes[2]))
            elif len(partes) == 2:
                entradas.append((partes[0], partes[0], partes[1]))
            else:
                entradas.append(("Marca Monitoreada", "Marca Monitoreada", l))

    total_empresas = len(entradas)
    if total_empresas == 0:
        print("❌ Error: 'urls.txt' está vacío.")
        actualizar_progreso(activo=False, finalizado=True)
        return

    print(f"🚀 Iniciando extracción para {total_empresas} empresas...")
    actualizar_progreso(activo=True, actual=0, total=total_empresas, empresa="Iniciando navegador...", porcentaje=0, finalizado=False)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900}
        )
        fecha_desde = (datetime.today() - timedelta(days=dias)).strftime('%Y-%m-%d') if dias else None
        # Por empresa: links vistos en sus búsquedas por página y si todas salieron completas.
        por_empresa = {}

        for indice, (nombre_flask, nombre_bot, url_base) in enumerate(entradas, 1):
            pct = int(((indice - 1) / total_empresas) * 100)
            actualizar_progreso(activo=True, actual=indice, total=total_empresas, empresa=nombre_flask, porcentaje=pct, finalizado=False)

            url_final = preparar_url_completa(url_base, dias)
            if es_busqueda_por_pagina(url_final):
                por_empresa.setdefault(nombre_flask, {"links": set(), "completo": True})
            # Una página por empresa: así no se acumulan los listeners de red entre empresas.
            page = context.new_page()
            try:
                resultado = extraer_anuncios(page, nombre_flask, nombre_bot, url_final, bloqueadas, fecha_desde)
                if resultado is not None:
                    por_empresa[nombre_flask]["links"].update(resultado["links"])
                    por_empresa[nombre_flask]["completo"] &= resultado["completo"]
            except Exception as e:
                print(f"❌ Error en {nombre_flask}: {e}")
                if CORRIDA:
                    CORRIDA.resultado(nombre_flask, error=e)
                if nombre_flask in por_empresa:
                    por_empresa[nombre_flask]["completo"] = False
            finally:
                page.close()

        print("\n🗂️ Actualizando anuncios retirados de Meta...")
        for nombre_flask, datos in por_empresa.items():
            if nombre_flask.lower() in bloqueadas:
                continue
            if datos["completo"]:
                marcar_retirados_meta(nombre_flask, datos["links"])
            else:
                print(f"  ⏭️ {nombre_flask}: búsqueda incompleta, se omite para no marcar retiros por error.")

        browser.close()
        registro_bots.guardar_historial(DATABASE_URL, "Meta",
                                        [n for n, _, _ in entradas if n.lower() not in bloqueadas])
        actualizar_progreso(activo=False, actual=total_empresas, total=total_empresas, empresa="Completado", porcentaje=100, finalizado=True)
        print("\n✨ Proceso completado exitosamente.")

if __name__ == '__main__':
    main()
