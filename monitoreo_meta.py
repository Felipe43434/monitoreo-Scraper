import os
import sys
import time
import json
import re
import urllib.parse
from datetime import datetime, timedelta
import psycopg2
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()
DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL and "sslmode=" not in DATABASE_URL:
    separador = "&" if "?" in DATABASE_URL else "?"
    DATABASE_URL = f"{DATABASE_URL}{separador}sslmode=require"

JSON_FILE = "anuncios_guardados.json"
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
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS anuncios_link_idx ON anuncios (link_individual);")
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
            INSERT INTO anuncios (id_anuncio, compania, fecha_subida, estado, plataformas, formato, duracion_segundos, titulo, link_individual)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (link_individual) DO UPDATE 
            SET id_anuncio = COALESCE(NULLIF(EXCLUDED.id_anuncio, ''), NULLIF(anuncios.id_anuncio, '')),
                compania = EXCLUDED.compania,
                fecha_subida = COALESCE(NULLIF(EXCLUDED.fecha_subida, ''), NULLIF(anuncios.fecha_subida, '')),
                estado = EXCLUDED.estado,
                plataformas = CASE 
                    WHEN EXCLUDED.plataformas IS NOT NULL AND EXCLUDED.plataformas != '' AND EXCLUDED.plataformas != 'Facebook' 
                    THEN EXCLUDED.plataformas 
                    ELSE COALESCE(NULLIF(anuncios.plataformas, ''), EXCLUDED.plataformas)
                END,
                formato = CASE 
                    WHEN EXCLUDED.formato = 'Video' THEN 'Video' 
                    ELSE COALESCE(NULLIF(anuncios.formato, ''), EXCLUDED.formato)
                END,
                duracion_segundos = CASE 
                    WHEN EXCLUDED.duracion_segundos > 0 THEN EXCLUDED.duracion_segundos 
                    ELSE COALESCE(anuncios.duracion_segundos, 0)
                END,
                titulo = CASE 
                    WHEN LENGTH(EXCLUDED.titulo) > LENGTH(COALESCE(anuncios.titulo, '')) 
                    THEN EXCLUDED.titulo 
                    ELSE COALESCE(NULLIF(anuncios.titulo, ''), EXCLUDED.titulo)
                END
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
            anuncio['link_individual']
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

def preparar_url_completa(url_base, dias_atras=30):
    parsed = urllib.parse.urlparse(url_base)
    params = urllib.parse.parse_qs(parsed.query)
    params['active_status'] = ['all']
    params['ad_type'] = ['all']

    if dias_atras:
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

def extraer_fecha_json_profundo(obj):
    if isinstance(obj, dict):
        for clave in ['start_date', 'start_time', 'startDate', 'creation_time', 'ad_delivery_start_time']:
            if clave in obj and obj[clave]:
                try:
                    val = int(obj[clave])
                    if val > 1000000000:
                        return datetime.utcfromtimestamp(val).strftime('%Y-%m-%d')
                except Exception:
                    pass
        for v in obj.values():
            f = extraer_fecha_json_profundo(v)
            if f:
                return f
    elif isinstance(obj, list):
        for item in obj:
            f = extraer_fecha_json_profundo(item)
            if f:
                return f
    return None

def extraer_anuncios(page, nombre_flask, nombre_bot, url_final, bloqueadas):
    print(f"\n🌐 Abriendo: {url_final}")
    print(f"🎯 Monitoreando: '{nombre_flask}' (Búsqueda bot: '{nombre_bot}')")

    fechas_api = {}
    duraciones_api = {}

    def interceptar_red(response):
        if any(w in response.url for w in ["graphql", "api", "ad_library"]):
            try:
                texto = response.text()
                if texto.startswith("for (;;);"):
                    texto = texto[len("for (;;);"):]

                for linea in texto.splitlines():
                    linea = linea.strip()
                    if not linea:
                        continue
                    try:
                        data = json.loads(linea)
                        def buscar_ids(o):
                            if isinstance(o, dict):
                                ad_id = o.get("ad_archive_id") or o.get("archive_id") or o.get("adArchiveID")
                                if ad_id:
                                    f = extraer_fecha_json_profundo(o)
                                    if f:
                                        fechas_api[str(ad_id)] = f
                                    dur = extraer_duracion_json_profundo(o)
                                    if dur > 0:
                                        duraciones_api[str(ad_id)] = dur

                                for v in o.values():
                                    buscar_ids(v)
                            elif isinstance(o, list):
                                for i in o:
                                    buscar_ids(i)
                        buscar_ids(data)
                    except Exception:
                        pass
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
    prev_ads_count = 0
    intentos_sin_cambio = 0

    for _ in range(25):
        page.mouse.wheel(0, 3500)
        time.sleep(1.2)
        
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

            // 2. DETECCIÓN PRECISA DE TODAS LAS PLATAFORMAS
            const plataformas = new Set();

            // Meta incluye un botón o grupo cuyo aria-label lista las redes o dice "Plataformas"
            const elementosDropdown = Array.from(card.querySelectorAll('[aria-label*="plataforma" i], [aria-label*="platform" i], [title*="plataforma" i], [title*="platform" i], div[role="button"]'));
            
            elementosDropdown.forEach(el => {
                const label = ((el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '')).toLowerCase();
                if (label.includes('facebook')) plataformas.add('Facebook');
                if (label.includes('instagram')) plataformas.add('Instagram');
                if (label.includes('threads')) plataformas.add('Threads');
                if (label.includes('messenger')) plataformas.add('Messenger');
                if (label.includes('audience') || label.includes('network')) plataformas.add('Audience Network');
            });

            // Si los aria-labels no traían los nombres, inspeccionar los iconos en la fila de plataformas
            const nodosPlataforma = Array.from(card.querySelectorAll('*')).filter(el => {
                const t = (el.innerText || '').trim();
                return /^plataformas\s*:?/i.test(t) && el.children.length <= 2;
            });

            if (nodosPlataforma.length > 0) {
                let contenedorFila = nodosPlataforma[0].parentElement;
                for (let k = 0; k < 3; k++) {
                    if (contenedorFila && contenedorFila.querySelectorAll('svg, i, div[role="img"], span[role="img"]').length >= 1) break;
                    if (contenedorFila && contenedorFila.parentElement) contenedorFila = contenedorFila.parentElement;
                }

                const elementosGraficos = Array.from(contenedorFila.querySelectorAll('svg, i, div[role="img"], span[role="img"], img, path'));
                elementosGraficos.forEach(ico => {
                    const raw = (ico.outerHTML || '').toLowerCase();
                    const dAttr = (ico.getAttribute('d') || '').toLowerCase();

                    // Instagram
                    if (raw.includes('instagram') || raw.includes('circle') || dAttr.includes('m12 2.163') || dAttr.includes('m12 7a5') || raw.includes('r="3.') || raw.includes('r="2.')) {
                        plataformas.add('Instagram');
                    }
                    // Facebook
                    if (raw.includes('facebook') || raw.includes('_fb') || dAttr.includes('m24 12.073') || dAttr.includes('m12 2.04') || dAttr.includes('v-3h-2v-4h2v-2') || dAttr.includes('v-1.9c0-1.9')) {
                        plataformas.add('Facebook');
                    }
                    // Messenger
                    if (raw.includes('messenger') || raw.includes('msgr') || dAttr.includes('l-3.5 5.5') || dAttr.includes('l3.5-5.5') || (dAttr.includes('m12 2c-5.52') && dAttr.includes('l-3'))) {
                        plataformas.add('Messenger');
                    }
                    // Audience Network
                    if (raw.includes('audience') || raw.includes('network') || raw.includes('globe') || dAttr.includes('m12 2a10 10') || dAttr.includes('a10 10 0 1 0') || dAttr.includes('c-3.1 0-5.8')) {
                        plataformas.add('Audience Network');
                    }
                    // Threads
                    if (raw.includes('threads') || raw.includes('hilos')) {
                        plataformas.add('Threads');
                    }
                });
            }

            // Fallback de cabecera
            if (plataformas.size === 0) {
                const headerHtml = card.innerHTML.substring(0, 3000).toLowerCase();
                if (headerHtml.includes('facebook')) plataformas.add('Facebook');
                if (headerHtml.includes('instagram')) plataformas.add('Instagram');
                if (headerHtml.includes('messenger')) plataformas.add('Messenger');
                if (headerHtml.includes('audience network') || headerHtml.includes('audience_network')) plataformas.add('Audience Network');
                if (headerHtml.includes('threads')) plataformas.add('Threads');
            }

            let plataformasFinal = Array.from(plataformas).join(', ');
            if (!plataformasFinal) {
                plataformasFinal = "Facebook";
            }

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
                plataformas: plataformasFinal,
                esVideo: hasVideo,
                duracion: duracionSegundos,
                copy: rawCopy || `Anuncio de ${nombreBuscado}`
            });
        });

        return resultados;
    }""", nombre_bot)

    vistos = set()
    guardados = 0

    for item in datos_anuncios:
        ad_id = item["id"]
        if ad_id in vistos:
            continue
        vistos.add(ad_id)

        empresa_actual = item["empresa"].strip()
        busq_limpia = re.sub(r'[^\w\s]', '', nombre_bot.lower()).strip()
        actual_limpia = re.sub(r'[^\w\s]', '', empresa_actual.lower()).strip()

        es_valida = (busq_limpia in actual_limpia) or (actual_limpia in busq_limpia) or len(actual_limpia) == 0 or len(busq_limpia) == 0
        if not es_valida:
            continue

        if nombre_flask.lower() in bloqueadas or empresa_actual.lower() in bloqueadas:
            continue

        fecha_final = normalizar_fecha_texto(item.get("fechaTexto"))
        if not fecha_final:
            fecha_final = fechas_api.get(ad_id)
        if not fecha_final:
            fecha_final = datetime.today().strftime('%Y-%m-%d')

        duracion_final = item["duracion"]
        if duracion_final == 0:
            duracion_final = duraciones_api.get(ad_id, 0)

        formato = "Video" if (item["esVideo"] or duracion_final > 0) else "Foto"
        icono = "🎬 VIDEO" if formato == "Video" else "🖼️ FOTO"
        dur_txt = f"{duracion_final}s" if duracion_final > 0 else "-"

        titulo_definitivo = limpiar_texto_copy(item["copy"], nombre_flask)

        anuncio_data = {
            "id_anuncio": ad_id,
            "compania": nombre_flask,
            "fecha_subida": fecha_final,
            "estado": item["estado"],
            "plataformas": item["plataformas"],
            "formato": formato,
            "duracion_segundos": duracion_final,
            "titulo": titulo_definitivo[:400],
            "link_individual": f"https://www.facebook.com/ads/library/?id={ad_id}"
        }

        if guardar_anuncio(anuncio_data, bloqueadas):
            guardados += 1
            badge_estado = "🟢 Activo" if item["estado"] == "Activo" else "⚪ Inactivo"
            print(f"  ✨ [{icono}] [{badge_estado}] {nombre_flask} | Plat: {item['plataformas']} | Dur: {dur_txt} | Fecha: {fecha_final} | Titulo: {titulo_definitivo[:40]}...")

    print(f"✅ Anuncios registrados para {nombre_flask}: {guardados}")

def main():
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
        page = context.new_page()

        for indice, (nombre_flask, nombre_bot, url_base) in enumerate(entradas, 1):
            pct = int(((indice - 1) / total_empresas) * 100)
            actualizar_progreso(activo=True, actual=indice, total=total_empresas, empresa=nombre_flask, porcentaje=pct, finalizado=False)
            
            url_final = preparar_url_completa(url_base, dias)
            try:
                extraer_anuncios(page, nombre_flask, nombre_bot, url_final, bloqueadas)
            except Exception as e:
                print(f"❌ Error en {nombre_flask}: {e}")

        browser.close()
        actualizar_progreso(activo=False, actual=total_empresas, total=total_empresas, empresa="Completado", porcentaje=100, finalizado=True)
        print("\n✨ Proceso completado exitosamente.")

if __name__ == '__main__':
    main()
