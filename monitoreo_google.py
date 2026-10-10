import os
import re
import sys
import json
import requests
from datetime import datetime, timedelta, timezone
import psycopg2
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

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
        data = json.loads(page.evaluate(JS_RPC, freq))
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
            INSERT INTO anuncios (id_anuncio, compania, fecha_subida, estado, plataformas, formato, duracion_segundos, titulo, link_individual, presente_en_meta, fuente, fecha_ultima_vista)
            VALUES (%s, %s, %s, %s, %s, %s, 0, %s, %s, TRUE, 'Google', %s)
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
                fecha_ultima_vista = EXCLUDED.fecha_ultima_vista
            RETURNING id;
        """, (
            anuncio["id_anuncio"], anuncio["compania"], anuncio["fecha_subida"], anuncio["estado"],
            anuncio["plataformas"], anuncio["formato"], anuncio["titulo"], anuncio["link_individual"],
            anuncio["fecha_ultima_vista"],
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
        return

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
        if formato == "Video":
            video_id = id_youtube(page, item)
            titulo_video = titulo_youtube(video_id, titulos_yt) if video_id else None
            if titulo_video:
                con_titulo += 1
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
        }
        if guardar_anuncio(anuncio):
            guardados += 1
            print(f"  ✨ [{formato}] [{anuncio['estado']}] {nombre} | Plat: {anuncio['plataformas']} | Desde: {anuncio['fecha_subida']}")

    print(f"✅ Anuncios de Google registrados para {nombre}: {guardados} de {len(creativos)}")
    videos = sum(1 for it in creativos.values() if FORMATOS.get(it.get("4")) == "Video")
    if videos:
        print(f"🎬 Títulos de YouTube obtenidos: {con_titulo} de {videos} videos")
    marcar_no_detectados(nombre, links)


def main():
    inicializar_bd()
    entradas = leer_entradas()
    if not entradas:
        print(f"❌ '{URLS_FILE}' no existe o está vacío.")
        return

    print(f"🚀 Iniciando extracción de Google para {len(entradas)} dominios...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(locale="es-ES")
        page.goto("https://adstransparency.google.com/?region=VE", wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(3000)
        for nombre, dominio in entradas:
            try:
                procesar_dominio(page, nombre, dominio)
            except Exception as e:
                print(f"❌ Error en {nombre} ({dominio}): {e}")
        browser.close()
    print("\n✨ Proceso de Google completado.")


if __name__ == '__main__':
    main()
