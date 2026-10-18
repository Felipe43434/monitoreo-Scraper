"""Bot de precios: revisa las páginas de precios de la competencia (precios.txt) y guarda una captura del
bloque de precios cada vez que cambia. Con esas capturas el panel arma el historial de precios de cada sitio.

Solo visita páginas públicas, una vez por corrida y sin iniciar sesión.
"""
import os
import re
import sys
import json
import hashlib

import psycopg2
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

import registro_bots
from extraer_precios import precios_de_bloque

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

load_dotenv()
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and "sslmode=" not in DATABASE_URL:
    separador = "&" if "?" in DATABASE_URL else "?"
    DATABASE_URL = f"{DATABASE_URL}{separador}sslmode=require"

ARCHIVO = "precios.txt"


def leer_paginas():
    paginas = []
    with open(ARCHIVO, encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if not linea or linea.startswith("#"):
                continue
            partes = [p.strip() for p in linea.split("|")]
            if len(partes) < 4 or not partes[1].startswith("https://"):
                print(f"⚠️ Línea ignorada en {ARCHIVO}: {linea}")
                continue
            paginas.append({"compania": partes[0], "url": partes[1], "inicio": partes[2], "fin": partes[3],
                            "moneda": partes[4].upper() if len(partes) > 4 else ""})
    return paginas


def recortar(texto, inicio, fin):
    # Texto entre la primera aparición de "inicio" y la siguiente de "fin" (sin distinguir mayúsculas).
    # Si "fin" es un número, se toman esas líneas después de la que contiene "inicio".
    bajo = texto.lower()
    i = bajo.find(inicio.lower())
    if i < 0:
        return None
    if fin.isdigit():
        lineas = [l.strip() for l in texto[bajo.rfind("\n", 0, i) + 1:].splitlines() if l.strip()]
        return "\n".join(lineas[:int(fin) + 1])
    j = bajo.find(fin.lower(), i + len(inicio))
    if j < 0:
        return None
    return "\n".join(l.strip() for l in texto[i:j].splitlines() if l.strip())


def leer_bloque(web, pagina):
    # Algunas páginas cargan los precios con JavaScript después de mostrar unos de respaldo: se espera a
    # que la red se calme y a que el bloque salga igual en dos lecturas seguidas.
    # TVirtual muestra los precios de Venezuela o los globales según la región que detecta (al azar desde
    # GitHub Actions): se piden siempre los de Venezuela para no confundir un cambio de región con uno de precios.
    web.unroute("**/api/precios.php*")
    if "tvirtual.net" in pagina["url"]:
        web.route("**/api/precios.php*", lambda ruta: ruta.continue_(url=re.sub(r"region=\w+", "region=VE", ruta.request.url)))
    web.goto(pagina["url"], wait_until="load", timeout=60000)
    try:
        web.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    anterior = None
    for _ in range(4):
        web.wait_for_timeout(3000)
        bloque = recortar(web.inner_text("body"), pagina["inicio"], pagina["fin"])
        if bloque and bloque == anterior:
            return bloque
        anterior = bloque
    return anterior


def guardar_captura(cur, pagina, bloque, precios):
    # Devuelve "primera", "cambio" o None si el bloque es igual al de la última captura.
    huella = hashlib.md5(bloque.encode("utf-8")).hexdigest()
    cur.execute("SELECT huella FROM precios_capturas WHERE compania = %s AND url = %s ORDER BY fecha DESC LIMIT 1;",
                (pagina["compania"], pagina["url"]))
    anterior = cur.fetchone()
    if anterior and anterior[0] == huella:
        return None
    cur.execute("INSERT INTO precios_capturas (compania, url, texto, huella, precios) VALUES (%s, %s, %s, %s, %s);",
                (pagina["compania"], pagina["url"], bloque, huella, json.dumps(precios, ensure_ascii=False)))
    return "cambio" if anterior else "primera"


def marcar(cur, pagina, estado, error=None):
    cur.execute("""
        INSERT INTO precios_paginas (compania, url, revisado, estado, error) VALUES (%s, %s, NOW(), %s, %s)
        ON CONFLICT (compania, url) DO UPDATE SET revisado = NOW(), estado = EXCLUDED.estado, error = EXCLUDED.error;
    """, (pagina["compania"], pagina["url"], estado, error))


def ejecutar():
    if not DATABASE_URL:
        print("❌ Falta DATABASE_URL.")
        sys.exit(1)
    corrida = registro_bots.Corrida("Precios", DATABASE_URL)
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=15)
    try:
        with conn.cursor() as cur:
            registro_bots.crear_tablas(cur)
        conn.commit()
        with sync_playwright() as pw:
            navegador = pw.chromium.launch()
            web = navegador.new_page(locale="es-VE")
            for pagina in leer_paginas():
                print(f"💲 {pagina['compania']}: {pagina['url']}")
                try:
                    bloque = leer_bloque(web, pagina)
                    if not bloque:
                        hasta = "" if pagina["fin"].isdigit() else f" a «{pagina['fin']}»"
                        raise ValueError(f"No se encontró el bloque de precios (de «{pagina['inicio']}»{hasta}); "
                                         "puede que la empresa haya cambiado su página.")
                    precios = precios_de_bloque(bloque, pagina["moneda"])
                    if not precios:
                        raise ValueError("El bloque de precios no tiene montos.")
                    with conn.cursor() as cur:
                        captura = guardar_captura(cur, pagina, bloque, precios)
                        marcar(cur, pagina, "ok")
                    conn.commit()
                    print(f"  {len(precios)} precios: " + " · ".join(p["etiqueta"] for p in precios)
                          + {"primera": "  (primera captura)", "cambio": "  ⚠️ CAMBIÓ desde la última revisión"}.get(captura, ""))
                    corrida.resultado(pagina["compania"], anuncios=len(precios),
                                      aviso="Cambiaron los precios" if captura == "cambio" else None)
                except Exception as e:
                    conn.rollback()
                    print(f"  ❌ {e}")
                    with conn.cursor() as cur:
                        marcar(cur, pagina, "error", str(e)[:300])
                    conn.commit()
                    corrida.resultado(pagina["compania"], error=e)
            navegador.close()
    except Exception as e:
        corrida.terminar(fallo=e)
        raise
    finally:
        conn.close()
    corrida.terminar()


if __name__ == "__main__":
    ejecutar()
