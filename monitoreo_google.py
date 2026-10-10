import os
import re
import sys
import json
import requests
from io import BytesIO
from datetime import datetime, timedelta, timezone
import psycopg2
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
import registro_bots

# OCR para leer el texto de los anuncios de texto e imagen (Google los entrega como imagen).
# En GitHub Actions se instala Tesseract con el idioma español; si no está, el bot sigue sin OCR.
try:
    import pytesseract
    from PIL import Image, ImageOps
    pytesseract.get_tesseract_version()
    OCR_DISPONIBLE = True
except Exception:
    OCR_DISPONIBLE = False

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

load_dotenv()
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and "sslmode=" not in DATABASE_URL:
    separador = "&" if "?" in DATABASE_URL else "?"
    DATABASE_URL = f"{DATABASE_URL}{separador}sslmode=require"

URLS_FILE = "urls_google.txt"
REGION_CODIGO = 2862  # Venezuela en la API interna del Centro de Transparencia
DIAS_PARA_ACTIVO = 3

# Codigos internos del filtro de plataforma, capturados del propio menu de Google (sept. 2026).
# El 6 no aparece en el menu pero filtra un conjunto distinto (casi solo banners): se asume Red de Display.
PLATAFORMAS = {
    1: "Google Play",
    2: "Google Maps",
    3: "Búsqueda de Google",
    4: "Google Shopping",
    5: "YouTube",
    6: "Red de Display",
}
FORMATOS = {1: "Texto", 2: "Imagen", 3: "Video"}

JS_TEXTO = "async (url) => { const r = await fetch(url); if (!r.ok) throw new Error('HTTP ' + r.status); return await r.text(); }"

# Los anuncios de video traen una vista previa (content.js) con la miniatura o el reproductor del
# video de YouTube; de ahí sale el id. La miniatura usa /vi/<id>/ y el reproductor /embed/<id>.
PATRON_YOUTUBE = re.compile(r'(?:ytimg\.com/vi(?:_webp)?/|youtube(?:-nocookie)?\.com/(?:embed/|watch\?v=)|youtu\.be/)([\w-]{11})')

JS_RPC = r"""async (freq) => {
    const r = await fetch('/anji/_/rpc/SearchService/SearchCreatives?authuser=', {
        method: 'POST',
        headers: {'content-type': 'application/x-www-form-urlencoded'},
        body: 'f.req=' + encodeURIComponent(JSON.stringify(freq))
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return await r.text();
}"""


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
        cur.execute("ALTER TABLE anuncios ADD COLUMN IF NOT EXISTS presente_en_meta BOOLEAN DEFAULT TRUE;")
        cur.execute("ALTER TABLE anuncios ADD COLUMN IF NOT EXISTS fuente VARCHAR(20) DEFAULT 'Meta';")
        cur.execute("ALTER TABLE anuncios ADD COLUMN IF NOT EXISTS fecha_ultima_vista VARCHAR(10);")
        registro_bots.crear_tablas(cur)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"❌ Error BD Init: {e}")


def leer_entradas():
    entradas = []
    if not os.path.exists(URLS_FILE):
        return entradas
    with open(URLS_FILE, "r", encoding="utf-8") as f:
        for linea in f:
            l = linea.strip()
            if not l or l.startswith("#"):
                continue
            partes = [p.strip() for p in l.split("|")]
            if len(partes) >= 2 and partes[1]:
                entradas.append((partes[0], partes[1].lower()))
    return entradas


def rpc_con_reintentos(page, freq, intentos=3):
    # Google a veces responde 404 a la API desde GitHub Actions (10-oct-2026, una corrida entera
    # falló y la siguiente funcionó): se recarga la página y se reintenta antes de rendirse.
    for intento in range(1, intentos + 1):
        try:
            return page.evaluate(JS_RPC, freq)
        except Exception as e:
            if intento == intentos:
                raise
            print(f"  ⚠️ La API de Google falló ({e}); reintento {intento} de {intentos - 1}...")
            page.wait_for_timeout(5000 * intento)
            page.goto("https://adstransparency.google.com/?region=VE", wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(3000)


def consultar(page, dominio, plataforma=None):
    filtro = {"8": [REGION_CODIGO], "12": {"1": dominio, "2": True}}
    if plataforma is not None:
        filtro["14"] = [plataforma]
    creativos = {}
    token = None
    for _ in range(50):
        freq = {"2": 40, "3": filtro, "7": {"1": 1, "2": 0, "3": 2840}}
        if token:
            freq["4"] = token
        data = json.loads(rpc_con_reintentos(page, freq))
        for item in data.get("1", []):
            creativos[item["2"]] = item
        token = data.get("2")
        if not token:
            break
    return creativos


def id_youtube(page, item):
    try:
        url = item.get("3", {}).get("1", {}).get("4")
        if not url:
            return None
        js = page.evaluate(JS_TEXTO, url)
        js = js.replace("\\/", "/").replace("\\x2F", "/").replace("\\u002F", "/")
        m = PATRON_YOUTUBE.search(js)
        return m.group(1) if m else None
    except Exception as e:
        print(f"  ⚠️ No se pudo leer la vista previa del video: {e}")
        return None


def titulo_youtube(video_id, cache):
    # oEmbed es público y no necesita clave de API; devuelve 401/404 si el video es privado o se borró.
    if video_id in cache:
        return cache[video_id]
    titulo = None
    try:
        r = requests.get("https://www.youtube.com/oembed",
                         params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"},
                         timeout=15)
        if r.ok:
            # Algunos videos se subieron con el nombre del archivo ("Galac cloud vertical.mp4").
            titulo = re.sub(r'\.(?:mp4|mov|avi|mkv|webm|m4v)$', '', (r.json().get("title") or "").strip(), flags=re.I) or None
    except Exception as e:
        print(f"  ⚠️ No se pudo consultar el título de YouTube {video_id}: {e}")
    cache[video_id] = titulo
    return titulo


def imagen_creativo(item):
    # Los anuncios de texto e imagen traen <img src="...simgad/..."> (URL permanente).
    html = item.get("3", {}).get("3", {}).get("2") or ""
    m = re.search(r'src="([^"]+)"', html)
    return m.group(1) if m else None


# Rótulos que Google dibuja dentro del anuncio y no son parte del texto.
RUIDO_OCR = re.compile(r'(?i)\b(?:patrocinado|sponsored|anuncio|ad)\b\s*[·:•|-]?')


def texto_ocr(url_imagen):
    if not OCR_DISPONIBLE or not url_imagen:
        return None
    try:
        r = requests.get(url_imagen, timeout=20)
        r.raise_for_status()
        img = ImageOps.grayscale(Image.open(BytesIO(r.content)))
        if img.width < 700:  # Tesseract lee mejor con letra más grande
            escala = 700 / img.width
            img = img.resize((int(img.width * escala), int(img.height * escala)))
        datos = pytesseract.image_to_data(img, lang="spa+eng", output_type=pytesseract.Output.DICT)
        lineas = {}
        for palabra, conf, bloque, linea in zip(datos["text"], datos["conf"], datos["block_num"], datos["line_num"]):
            if palabra.strip() and float(conf) >= 60:
                lineas.setdefault((bloque, linea), []).append(palabra.strip())
        texto = " ".join(" ".join(p) for p in lineas.values())
        texto = re.sub(r"\s+", " ", RUIDO_OCR.sub(" ", texto)).strip(" ·•|-")
        letras = sum(ch.isalpha() for ch in texto)
        # Se descarta lo que no parece texto real (pocas palabras o mayoría de símbolos).
        if len(texto.split()) < 3 or letras < 0.6 * len(texto.replace(" ", "")):
            return None
        return texto[:400]
    except Exception as e:
        print(f"  ⚠️ OCR falló para {url_imagen[:60]}: {e}")
        return None


def titulos_guardados(compania):
    # Texto ya obtenido en corridas anteriores (OCR o YouTube): no se vuelve a procesar.
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cur = conn.cursor()
        cur.execute("""
            SELECT link_individual, titulo FROM anuncios
            WHERE fuente = 'Google' AND compania = %s AND titulo IS NOT NULL AND titulo NOT LIKE 'Anuncio de %%'
        """, (compania,))
        datos = dict(cur.fetchall())
        cur.close()
        conn.close()
        return datos
    except Exception:
        return {}


def fecha_desde_epoch(valor):
    try:
        return datetime.fromtimestamp(int(valor["1"]), tz=timezone.utc)
    except Exception:
        return None


def guardar_anuncio(anuncio):
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO anuncios (id_anuncio, compania, fecha_subida, estado, plataformas, formato, duracion_segundos, titulo, link_individual, presente_en_meta, fuente, fecha_ultima_vista, imagen_url, destino, destino_url)
            VALUES (%s, %s, %s, %s, %s, %s, 0, %s, %s, TRUE, 'Google', %s, %s, %s, %s)
            ON CONFLICT (link_individual) DO UPDATE
            SET compania = EXCLUDED.compania,
                fecha_subida = EXCLUDED.fecha_subida,
                estado = EXCLUDED.estado,
                plataformas = EXCLUDED.plataformas,
                formato = EXCLUDED.formato,
                titulo = CASE
                    WHEN EXCLUDED.titulo LIKE 'Anuncio de %%' AND anuncios.titulo NOT LIKE 'Anuncio de %%'
                    THEN anuncios.titulo ELSE EXCLUDED.titulo END,
                presente_en_meta = TRUE,
                fuente = 'Google',
                fecha_ultima_vista = EXCLUDED.fecha_ultima_vista,
                imagen_url = COALESCE(NULLIF(EXCLUDED.imagen_url, ''), anuncios.imagen_url),
                destino = EXCLUDED.destino,
                destino_url = EXCLUDED.destino_url
            RETURNING id;
        """, (
            anuncio["id_anuncio"], anuncio["compania"], anuncio["fecha_subida"], anuncio["estado"],
            anuncio["plataformas"], anuncio["formato"], anuncio["titulo"], anuncio["link_individual"],
            anuncio["fecha_ultima_vista"], anuncio.get("imagen_url"), anuncio.get("destino"), anuncio.get("destino_url"),
        ))
        res = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        return bool(res)
    except Exception as e:
        print(f"  ⚠️ Error BD: {e}")
        return False


def marcar_no_detectados(compania, links_encontrados):
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cur = conn.cursor()
        cur.execute("""
            UPDATE anuncios
            SET presente_en_meta = FALSE
            WHERE compania = %s AND fuente = 'Google' AND link_individual != ALL(%s)
              AND presente_en_meta IS DISTINCT FROM FALSE;
        """, (compania, links_encontrados))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"  ⚠️ Error actualizando anuncios retirados de Google: {e}")


def procesar_dominio(page, nombre, dominio):
    print(f"\n🌐 Google Ads Transparency: '{nombre}' ({dominio})")
    creativos = consultar(page, dominio)
    if not creativos:
        print("  Sin anuncios en Venezuela para este dominio.")
        return 0

    plataformas_por_creativo = {cid: [] for cid in creativos}
    for codigo, nombre_plat in PLATAFORMAS.items():
        for cid in consultar(page, dominio, codigo):
            if cid in plataformas_por_creativo:
                plataformas_por_creativo[cid].append(nombre_plat)

    ahora = datetime.now(timezone.utc)
    links = []
    guardados = 0
    titulos_yt = {}
    con_titulo = 0
    ya_leidos = titulos_guardados(nombre)
    con_ocr = 0
    for cid, item in creativos.items():
        primera = fecha_desde_epoch(item.get("6", {}))
        ultima = fecha_desde_epoch(item.get("7", {}))
        formato = FORMATOS.get(item.get("4"), "Otro")
        anunciante = item.get("12", nombre)
        link = f"https://adstransparency.google.com/advertiser/{item['1']}/creative/{cid}?region=VE"
        links.append(link)
        # El texto de los anuncios de texto e imagen viene dentro de una imagen; en los de video
        # se usa el título del video de YouTube como texto del anuncio.
        titulo_video = None
        imagen = imagen_creativo(item)
        if formato == "Video":
            video_id = id_youtube(page, item)
            titulo_video = titulo_youtube(video_id, titulos_yt) if video_id else None
            if video_id:
                imagen = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
            if titulo_video:
                con_titulo += 1
        else:
            titulo_video = ya_leidos.get(link) or texto_ocr(imagen)
            if titulo_video:
                con_ocr += 1
        anuncio = {
            "id_anuncio": cid,
            "compania": nombre,
            "fecha_subida": primera.strftime('%Y-%m-%d') if primera else ahora.strftime('%Y-%m-%d'),
            "estado": "Activo" if ultima and (ahora - ultima) <= timedelta(days=DIAS_PARA_ACTIVO) else "Inactivo",
            "plataformas": ", ".join(plataformas_por_creativo[cid]) or "Google",
            "formato": formato,
            "titulo": titulo_video or (f"Anuncio de {formato.lower()} en Google de {anunciante}"
                      + (f" (visto por última vez {ultima.strftime('%Y-%m-%d')})" if ultima else "")),
            "link_individual": link,
            "fecha_ultima_vista": ultima.strftime('%Y-%m-%d') if ultima else None,
            "imagen_url": imagen,
            "destino": "Sitio web",
            "destino_url": f"https://{dominio}",
        }
        if guardar_anuncio(anuncio):
            guardados += 1
            print(f"  ✨ [{formato}] [{anuncio['estado']}] {nombre} | Plat: {anuncio['plataformas']} | Desde: {anuncio['fecha_subida']}")

    print(f"✅ Anuncios de Google registrados para {nombre}: {guardados} de {len(creativos)}")
    videos = sum(1 for it in creativos.values() if FORMATOS.get(it.get("4")) == "Video")
    if videos:
        print(f"🎬 Títulos de YouTube obtenidos: {con_titulo} de {videos} videos")
    estaticos = len(creativos) - videos
    if estaticos:
        print(f"🔤 Texto leído con OCR: {con_ocr} de {estaticos} anuncios de texto/imagen"
              + ("" if OCR_DISPONIBLE else " (Tesseract no está instalado: OCR desactivado)"))
    marcar_no_detectados(nombre, links)
    return guardados


def main():
    corrida = registro_bots.Corrida("Google", DATABASE_URL)
    try:
        entradas = ejecutar(corrida)
    except Exception as e:
        print(f"❌ Falla general del bot de Google: {e}")
        corrida.terminar(fallo=e)
        raise
    if entradas:
        registro_bots.guardar_historial(DATABASE_URL, "Google", [n for n, _ in entradas])
    corrida.terminar()


def ejecutar(corrida):
    inicializar_bd()
    entradas = leer_entradas()
    if not entradas:
        print(f"❌ '{URLS_FILE}' no existe o está vacío.")
        return entradas

    print(f"🚀 Iniciando extracción de Google para {len(entradas)} dominios...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(locale="es-ES")
        page.goto("https://adstransparency.google.com/?region=VE", wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(3000)
        for nombre, dominio in entradas:
            try:
                corrida.resultado(nombre, procesar_dominio(page, nombre, dominio))
            except Exception as e:
                print(f"❌ Error en {nombre} ({dominio}): {e}")
                corrida.resultado(nombre, error=f"{dominio}: {e}")
        browser.close()
    print("\n✨ Proceso de Google completado.")
    return entradas


if __name__ == '__main__':
    main()
