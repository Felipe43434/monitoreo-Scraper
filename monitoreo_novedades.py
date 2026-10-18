"""Bot de novedades: notas de versión, anuncios oficiales y versiones de producto de la competencia
(novedades.txt) para la Línea de Tiempo del panel. Cada entrada se guarda una sola vez, con su fecha.

Solo lee páginas y APIs públicas, sin iniciar sesión. Usa un navegador porque algunos sitios bloquean
las peticiones que no vienen de uno.
"""
import os
import re
import sys
import json
import html
import unicodedata

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

ARCHIVO = "novedades.txt"
# "ADMINISTRATIVO 9.1", "SISTEMA ADMINISTRATIVO 9.1X": nombre de producto en mayúsculas seguido de su versión.
PATRON_VERSION_PRODUCTO = re.compile(r"\b([A-ZÁÉÍÓÚÑ]{4,}(?: [A-ZÁÉÍÓÚÑ]{2,}){0,2})\s+(\d+\.\d+(?:\.\d+)?[A-Z]{0,3})\b")


def sin_tildes(texto):
    return "".join(c for c in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(c) != "Mn")


def leer_fuentes():
    fuentes = []
    with open(ARCHIVO, encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if not linea or linea.startswith("#"):
                continue
            partes = [p.strip() for p in linea.split("|")]
            if len(partes) < 3 or partes[1] not in ("wordpress", "tvirtual", "versiones") or not partes[2].startswith("https://"):
                print(f"⚠️ Línea ignorada en {ARCHIVO}: {linea}")
                continue
            filtro = [sin_tildes(p) for p in partes[3].split(",") if p.strip()] if len(partes) > 3 else []
            fuentes.append({"compania": partes[0], "tipo": partes[1], "url": partes[2].rstrip("/"), "filtro": filtro})
    return fuentes


def json_de(respuesta):
    # Algunos sitios mandan el JSON en latin-1 aunque digan UTF-8.
    cuerpo = respuesta.body()
    try:
        return json.loads(cuerpo.decode("utf-8"))
    except UnicodeDecodeError:
        return json.loads(cuerpo.decode("latin-1"))


def entradas_wordpress(web, fuente):
    web.goto(fuente["url"] + "/", wait_until="load", timeout=60000)
    r = web.request.get(fuente["url"] + "/wp-json/wp/v2/posts?per_page=30&_fields=id,date,title,link")
    if not r.ok:
        raise ValueError(f"La API de noticias respondió {r.status}.")
    for post in json_de(r):
        titulo = " ".join(re.sub(r"<[^>]+>", "", html.unescape(post["title"]["rendered"])).split())
        if titulo and (not fuente["filtro"] or any(p in sin_tildes(titulo) for p in fuente["filtro"])):
            yield {"clave": f"wp:{post['id']}", "fecha": post["date"][:10], "titulo": titulo, "enlace": post.get("link"), "tipo": "anuncio"}


def entradas_tvirtual(web, fuente):
    r = web.request.get(fuente["url"])
    if not r.ok:
        raise ValueError(f"Las notas de versión respondieron {r.status}.")
    sistemas = {"nomina": "Nómina", "administrativo": "Sistema Administrativo"}
    for nota in json_de(r):
        lineas = [l.strip() for l in str(nota.get("descripcion") or "").splitlines() if l.strip()]
        tema = re.sub(r"^\d+\.\s*", "", next((l for l in lineas if not l.lower().startswith("registro de actualizaciones")), ""))
        titulo = f"{sistemas.get(nota.get('sistema'), nota.get('sistema') or 'Sistema')} {nota.get('version') or ''}".strip()
        yield {"clave": f"tv:{nota['id']}", "fecha": str(nota.get("fecha"))[:10], "tipo": "nota",
               "titulo": f"Nota de versión: {titulo}" + (f" — {tema[:90]}" if tema else ""),
               "enlace": "https://tvirtual.net/actualizaciones_lista.php"}


# Texto de cada elemento en su propia línea, también el de los oculto (carruseles y secciones que se cargan
# al bajar por la página): así la lectura no depende de qué parte se alcanzó a mostrar.
JS_TEXTO_COMPLETO = """() => [...document.body.querySelectorAll('*')]
    .filter(e => !['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE'].includes(e.tagName))
    .map(e => [...e.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent.trim()).join(' ').trim())
    .filter(Boolean).join('\\n')"""


def entradas_versiones(web, fuente):
    web.goto(fuente["url"] + "/", wait_until="load", timeout=60000)
    try:
        web.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    hoy = registro_bots.hoy_venezuela().isoformat()
    for producto, version in sorted(set(PATRON_VERSION_PRODUCTO.findall(web.evaluate(JS_TEXTO_COMPLETO)))):
        yield {"clave": f"v:{producto} {version}", "fecha": hoy, "tipo": "version", "enlace": fuente["url"] + "/",
               "titulo": f"{producto.title()} pasó a la versión {version}"}


LECTORES = {"wordpress": entradas_wordpress, "tvirtual": entradas_tvirtual, "versiones": entradas_versiones}


def guardar(cur, fuente, entradas):
    # Devuelve cuántas entradas son nuevas. En una página de versiones, lo que ya estaba la primera vez
    # que se revisa se marca como inicial (es la versión actual, no un cambio).
    inicial = False
    if fuente["tipo"] == "versiones":
        cur.execute("SELECT 1 FROM novedades WHERE compania = %s AND tipo = 'version' AND enlace = %s LIMIT 1;",
                    (fuente["compania"], fuente["url"] + "/"))
        inicial = cur.fetchone() is None
    nuevas = 0
    for e in entradas:
        cur.execute("""
            INSERT INTO novedades (compania, clave, fecha, titulo, enlace, tipo, inicial) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (compania, clave) DO NOTHING;
        """, (fuente["compania"], e["clave"], e["fecha"], e["titulo"], e["enlace"], e["tipo"], inicial))
        nuevas += cur.rowcount
    return nuevas


def ejecutar():
    if not DATABASE_URL:
        print("❌ Falta DATABASE_URL.")
        sys.exit(1)
    corrida = registro_bots.Corrida("Novedades", DATABASE_URL)
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=15)
    try:
        with conn.cursor() as cur:
            registro_bots.crear_tablas(cur)
        conn.commit()
        with sync_playwright() as pw:
            navegador = pw.chromium.launch()
            web = navegador.new_page(locale="es-VE")
            for fuente in leer_fuentes():
                print(f"📰 {fuente['compania']} ({fuente['tipo']}): {fuente['url']}")
                try:
                    entradas = list(LECTORES[fuente["tipo"]](web, fuente))
                    with conn.cursor() as cur:
                        nuevas = guardar(cur, fuente, entradas)
                    conn.commit()
                    print(f"  {len(entradas)} entradas, {nuevas} nuevas" + "".join(f"\n    {e['fecha']} {e['titulo'][:90]}" for e in entradas[:5]))
                    corrida.resultado(fuente["compania"], anuncios=len(entradas), aviso=f"{nuevas} nuevas" if nuevas else None)
                except Exception as e:
                    conn.rollback()
                    print(f"  ❌ {e}")
                    corrida.resultado(fuente["compania"], error=e)
            navegador.close()
    except Exception as e:
        corrida.terminar(fallo=e)
        raise
    finally:
        conn.close()
    corrida.terminar()


if __name__ == "__main__":
    ejecutar()
