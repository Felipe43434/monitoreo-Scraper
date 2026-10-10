"""Registro compartido de los bots de Meta y Google.

- crear_tablas: columnas y tablas que usan los bots y el dashboard.
- guardar_historial: foto diaria de cuántos anuncios activos tiene cada empresa (gráfico de tendencia).
- Corrida: resumen de cada ejecución para el panel "Salud de los Bots".

Nada de esto debe tumbar un bot: los errores se imprimen y se sigue.
"""
import os
import json
from datetime import datetime, timedelta, timezone

import psycopg2


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
