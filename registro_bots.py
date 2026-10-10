"""Registro compartido de los bots de Meta y Google.

- crear_tablas: columnas y tablas que usan los bots y el dashboard.
- guardar_historial: foto diaria de cuántos anuncios activos tiene cada empresa (gráfico de tendencia).
- Corrida: resumen de cada ejecución para el panel "Salud de los Bots".

Nada de esto debe tumbar un bot: los errores se imprimen y se sigue.
"""
import os
import json
from io import BytesIO
from datetime import datetime, timedelta, timezone

import psycopg2
import requests

# Copia propia de cada miniatura: las URLs de imagen de Meta caducan en unos días. 640 px de lado y
# JPEG calidad 82 se ven nítidas en la tabla y en la vista ampliada (unos 40-70 KB cada una).
LADO_MINIATURA = 640
CALIDAD_MINIATURA = 82


def hoy_venezuela():
    return (datetime.now(timezone.utc) - timedelta(hours=4)).date()


def crear_tablas(cur):
    # La tabla anuncios ya debe existir (la crea cada bot antes de llamar aquí).
    cur.execute("ALTER TABLE anuncios ADD COLUMN IF NOT EXISTS imagen_url TEXT;")
    cur.execute("ALTER TABLE anuncios ADD COLUMN IF NOT EXISTS destino VARCHAR(30);")
    cur.execute("ALTER TABLE anuncios ADD COLUMN IF NOT EXISTS destino_url TEXT;")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS historial_activos (
            fecha DATE NOT NULL,
            compania VARCHAR(255) NOT NULL,
            fuente VARCHAR(20) NOT NULL,
            activos INT NOT NULL DEFAULT 0,
            PRIMARY KEY (fecha, compania, fuente)
        );
    """)
    # Tabla aparte para que el panel no cargue las imágenes al listar anuncios.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS miniaturas (
            link_individual TEXT PRIMARY KEY,
            imagen BYTEA NOT NULL,
            ancho INT,
            alto INT,
            guardada TIMESTAMPTZ DEFAULT NOW()
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS corridas_scraper (
            id SERIAL PRIMARY KEY,
            fuente VARCHAR(20) NOT NULL,
            inicio TIMESTAMPTZ NOT NULL,
            fin TIMESTAMPTZ,
            estado VARCHAR(20) NOT NULL,
            anuncios INT DEFAULT 0,
            empresas INT DEFAULT 0,
            errores INT DEFAULT 0,
            detalle TEXT,
            url_ejecucion TEXT
        );
    """)


def descargar_miniatura(url, timeout=15):
    """Descarga la imagen y la deja en JPEG de hasta LADO_MINIATURA px. Devuelve (bytes, ancho, alto) o None."""
    if not url or not url.startswith(('http://', 'https://')):
        return None
    try:
        from PIL import Image, ImageOps
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200 or not r.content:
            return None
        img = ImageOps.exif_transpose(Image.open(BytesIO(r.content)))
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            fondo = Image.new("RGB", img.size, (255, 255, 255))
            fondo.paste(img, mask=img.split()[-1])
            img = fondo
        elif img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((LADO_MINIATURA, LADO_MINIATURA), Image.LANCZOS)
        salida = BytesIO()
        img.save(salida, "JPEG", quality=CALIDAD_MINIATURA, optimize=True, progressive=True)
        return salida.getvalue(), img.width, img.height
    except Exception as e:
        print(f"  ⚠️ No se pudo procesar la miniatura {url[:60]}: {e}")
        return None


def guardar_miniatura(cur, link, datos):
    imagen, ancho, alto = datos
    cur.execute("""
        INSERT INTO miniaturas (link_individual, imagen, ancho, alto) VALUES (%s, %s, %s, %s)
        ON CONFLICT (link_individual) DO NOTHING
    """, (link, psycopg2.Binary(imagen), ancho, alto))


def asegurar_miniatura(database_url, link, url_imagen):
    """Guarda la copia de la miniatura si todavía no existe (la imagen de un anuncio no cambia).
    Devuelve True si quedó guardada (nueva o ya existente)."""
    if not link or not url_imagen:
        return False
    try:
        conn = psycopg2.connect(database_url, connect_timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM miniaturas WHERE link_individual = %s", (link,))
        if cur.fetchone():
            cur.close(); conn.close()
            return True
        datos = descargar_miniatura(url_imagen)
        if datos:
            guardar_miniatura(cur, link, datos)
            conn.commit()
        cur.close()
        conn.close()
        return bool(datos)
    except Exception as e:
        print(f"  ⚠️ No se pudo guardar la miniatura: {e}")
        return False


def guardar_historial(database_url, fuente, companias):
    """Guarda (o reemplaza) la foto de hoy: anuncios vigentes por empresa para esa fuente.
    Las empresas monitoreadas sin anuncios quedan en 0, para distinguirlas de "sin dato"."""
    try:
        conn = psycopg2.connect(database_url, connect_timeout=10)
        cur = conn.cursor()
        cur.execute("""
            SELECT compania, COUNT(*) FROM anuncios
            WHERE presente_en_meta IS TRUE AND COALESCE(fuente, 'Meta') = %s AND COALESCE(compania, '') <> ''
            GROUP BY compania
        """, (fuente,))
        conteos = dict(cur.fetchall())
        hoy = hoy_venezuela()
        for compania in sorted(set(companias) | set(conteos)):
            cur.execute("""
                INSERT INTO historial_activos (fecha, compania, fuente, activos) VALUES (%s, %s, %s, %s)
                ON CONFLICT (fecha, compania, fuente) DO UPDATE SET activos = EXCLUDED.activos
            """, (hoy, compania, fuente, conteos.get(compania, 0)))
        conn.commit()
        cur.close()
        conn.close()
        print(f"📈 Historial de anuncios activos guardado ({fuente}, {hoy}): "
              + ", ".join(f"{c} {conteos.get(c, 0)}" for c in sorted(set(companias) | set(conteos))))
    except Exception as e:
        print(f"⚠️ No se pudo guardar el historial de anuncios activos: {e}")


class Corrida:
    """Acumula el resultado por empresa y al terminar deja una fila en corridas_scraper."""

    def __init__(self, fuente, database_url):
        self.fuente = fuente
        self.database_url = database_url
        self.inicio = datetime.now(timezone.utc)
        self.empresas = {}

    def resultado(self, empresa, anuncios=0, error=None, aviso=None):
        fila = self.empresas.setdefault(empresa, {"anuncios": 0, "error": None, "aviso": None})
        fila["anuncios"] += anuncios
        if error:
            fila["error"] = str(error)[:300]
        if aviso:
            fila["aviso"] = str(aviso)[:300]

    def terminar(self, fallo=None):
        total = sum(f["anuncios"] for f in self.empresas.values())
        errores = sum(1 for f in self.empresas.values() if f["error"])
        if fallo or (self.empresas and errores == len(self.empresas)):
            estado = "fallo"
        elif errores:
            estado = "con_errores"
        elif total == 0:
            estado = "vacia"
        else:
            estado = "ok"
        detalle = {"empresas": self.empresas}
        if fallo:
            detalle["fallo"] = str(fallo)[:500]
        url = None
        if os.environ.get("GITHUB_RUN_ID"):
            url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                   f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ['GITHUB_RUN_ID']}")
        try:
            conn = psycopg2.connect(self.database_url, connect_timeout=10)
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO corridas_scraper (fuente, inicio, fin, estado, anuncios, empresas, errores, detalle, url_ejecucion)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (self.fuente, self.inicio, datetime.now(timezone.utc), estado, total, len(self.empresas),
                  errores, json.dumps(detalle, ensure_ascii=False), url))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            print(f"⚠️ No se pudo registrar la corrida en el panel de salud: {e}")
        print(f"🩺 Corrida de {self.fuente}: {estado} | {total} anuncios | {len(self.empresas)} empresas | {errores} con error")
        return estado
