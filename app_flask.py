import os
import io
import base64
import re
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

DATABASE_URL = os.environ.get("DATABASE_URL")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")
WORKFLOW_FILE = "scraper.yml"
WORKFLOW_GOOGLE_FILE = "scraper_google.yml"
URLS_FILE_PATH = "urls.txt"
URLS_GOOGLE_FILE_PATH = "urls_google.txt"

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

def registrar_acceso():
    conn = get_db_connection()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO accesos_dashboard (ip_address, user_agent) VALUES (%s, %s)",
                (obtener_ip_cliente(), request.headers.get('User-Agent', ''))
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

def calcular_dias_activo(fecha_val):
    f_norm = parse_date_str(fecha_val)
    if not f_norm:
        return 0
    try:
        dt = datetime.strptime(f_norm, '%Y-%m-%d')
        diff = (datetime.now() - dt).days
        return max(0, diff)
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
    </style>
</head>
<body>

<nav class="navbar navbar-expand-lg navbar-dark px-3 py-2 sticky-top">
    <div class="container-fluid">
        <a class="navbar-brand d-flex align-items-center gap-2" href="/">
            <i class="bi bi-graph-up-arrow text-primary fs-4"></i>
            <span class="fw-bold tracking-tight">Gálac Ads Intelligence</span>
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
                </ul>
                <div class="tab-content">
                <div class="tab-pane fade show active" id="urls-tab-meta">
                <form action="/guardar_urls_txt" method="POST">
                    <div class="d-flex justify-content-between align-items-center mb-2">
                        <span class="small text-muted">Formato: <code>Nombre en Panel | Nombre Búsqueda Bot | URL</code></span>
                        <span class="badge bg-secondary-subtle text-secondary">{{ config_urls|length }} enlaces detectados</span>
                    </div>
                    
                    <textarea name="raw_urls" class="form-control form-control-sm font-monospace mb-3 bg-dark text-light border-secondary" rows="10" placeholder="Nombre en Panel | Nombre en Meta | https://www.facebook.com/ads/library/?...">{{ raw_urls_content }}</textarea>
                    
                    <div class="d-flex justify-content-between align-items-center">
                        <small class="text-secondary"><i class="bi bi-github"></i> Se sincronizará directamente con el archivo <code>urls.txt</code> de tu repositorio.</small>
                        <button type="submit" class="btn btn-sm btn-primary px-3"><i class="bi bi-cloud-arrow-up"></i> Guardar en GitHub</button>
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

                <div class="tab-pane fade" id="urls-tab-google">
                <form action="/guardar_urls_google" method="POST">
                    <div class="d-flex justify-content-between align-items-center mb-2">
                        <span class="small text-muted">Formato: <code>Nombre en Panel | dominio.com</code></span>
                        <span class="badge bg-secondary-subtle text-secondary">{{ config_google|length }} dominios detectados</span>
                    </div>

                    <textarea name="raw_urls" class="form-control form-control-sm font-monospace mb-2 bg-dark text-light border-secondary" rows="8" placeholder="Nombre en Panel | galac.com">{{ raw_google_content }}</textarea>
                    <p class="small text-muted mb-3">Usa el mismo "Nombre en Panel" que en la pestaña Meta para que los anuncios de ambas fuentes se agrupen en la misma empresa. Se buscan los anuncios mostrados en Venezuela en el Centro de Transparencia de Anuncios de Google.</p>

                    <div class="d-flex justify-content-between align-items-center">
                        <small class="text-secondary"><i class="bi bi-github"></i> Se sincronizará con el archivo <code>urls_google.txt</code> de tu repositorio.</small>
                        <button type="submit" class="btn btn-sm btn-primary px-3"><i class="bi bi-cloud-arrow-up"></i> Guardar en GitHub</button>
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
    <div class="row g-3 mb-4">
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

    <!-- Panel de Filtros -->
    <div class="card-custom p-3 mb-4 card-filter-container">
        <form method="GET" action="/" id="filterForm" class="row g-2 align-items-end">
            <!-- 1. Buscar -->
            <div class="col-md-3 col-lg-2">
                <label class="form-label small fw-semibold text-muted mb-1"><i class="bi bi-search"></i> Buscar</label>
                <input type="text" name="q" class="form-control form-control-sm" placeholder="Texto, link..." value="{{ request.args.get('q', '') }}">
            </div>

            <!-- 2. Selección Múltiple Compañías -->
            <div class="col-md-3 col-lg-2">
                <label class="form-label small fw-semibold text-muted mb-1"><i class="bi bi-building"></i> Compañías ({% if companias_sel %}{{ companias_sel|length }}{% else %}Todas{% endif %})</label>
                <div class="dropdown">
                    <button class="form-select form-select-sm text-start d-flex justify-content-between align-items-center" type="button" data-bs-toggle="dropdown" data-bs-auto-close="outside">
                        <span class="text-truncate">
                            {% if companias_sel %}
                                {{ companias_sel|join(', ') }}
                            {% else %}
                                Todas ({{ lista_companias|length }})
                            {% endif %}
                        </span>
                    </button>
                    <div class="dropdown-menu dropdown-menu-scroll p-2 w-100 shadow-lg">
                        <div class="form-check pb-1 mb-1 border-bottom">
                            <input class="form-check-input" type="checkbox" id="selectAllCompanies" onchange="toggleAllCompanies(this)">
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
                    <option value="corta" {% if request.args.get('duracion', 'corta') == 'corta' %}selected{% endif %}>&lt; 15s</option>
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

            <!-- 8. Botones de Acción -->
            <div class="col-12 col-lg-2 d-flex gap-1">
                <button type="submit" class="btn btn-sm btn-primary w-100"><i class="bi bi-funnel"></i> Filtrar</button>
                <a href="/" class="btn btn-sm btn-outline-secondary" title="Limpiar filtros"><i class="bi bi-arrow-counterclockwise"></i></a>
                <a href="/descargar_excel?{{ request.query_string.decode() }}" class="btn btn-sm btn-success text-nowrap" title="Descargar Excel"><i class="bi bi-file-earmark-excel"></i></a>
            </div>
        </form>
    </div>

    <!-- Pestañas -->
    <ul class="nav nav-tabs mb-3" id="mainTab" role="tablist">
        <li class="nav-item">
            <button class="nav-link active fw-semibold" data-bs-toggle="tab" data-bs-target="#tab-charts" type="button">
                <i class="bi bi-bar-chart-line"></i> Vista General & Tendencias
            </button>
        </li>
        <li class="nav-item">
            <button class="nav-link fw-semibold" data-bs-toggle="tab" data-bs-target="#tab-ads" type="button">
                <i class="bi bi-list-columns"></i> Detalle de Anuncios ({{ anuncios|length }})
            </button>
        </li>
        <li class="nav-item">
            <button class="nav-link fw-semibold position-relative text-danger" data-bs-toggle="tab" data-bs-target="#tab-winning" type="button">
                <i class="bi bi-fire"></i> Winning Ads
                {% if total_winning > 0 %}
                <span class="badge rounded-pill bg-danger ms-1">{{ total_winning }}</span>
                {% endif %}
            </button>
        </li>
        <li class="nav-item">
            <button class="nav-link fw-semibold position-relative text-danger" data-bs-toggle="tab" data-bs-target="#tab-retirados" type="button">
                <i class="bi bi-eye-slash"></i> Retirados de Meta
                {% if total_retirados > 0 %}
                <span class="badge rounded-pill bg-danger ms-1">{{ total_retirados }}</span>
                {% endif %}
            </button>
        </li>
        <li class="nav-item">
            <button class="nav-link fw-semibold position-relative text-info" data-bs-toggle="tab" data-bs-target="#tab-new" type="button">
                <i class="bi bi-stars text-info"></i> Nuevos Anuncios
                <span class="badge rounded-pill bg-info ms-1" id="tabNuevosBadge" {% if total_nuevos == 0 %}style="display:none;"{% endif %}>{{ total_nuevos }}</span>
            </button>
        </li>
        <li class="nav-item">
            <button class="nav-link fw-semibold" data-bs-toggle="tab" data-bs-target="#tab-keywords" type="button">
                <i class="bi bi-chat-square-quote"></i> Términos Frecuentes
            </button>
        </li>
    </ul>

    <div class="tab-content">
        <!-- Panel 1: Gráficas y Empresas Registradas -->
        <div class="tab-pane fade show active" id="tab-charts">
            <div class="row g-3 mb-4">
                <div class="col-lg-8">
                    <div class="card-custom p-3 h-100">
                        <div class="d-flex flex-wrap justify-content-between align-items-center mb-3 gap-2">
                            <h6 class="fw-bold m-0"><i class="bi bi-graph-up"></i> Publicación de Anuncios por Empresa</h6>
                            <div class="d-flex flex-wrap gap-2">
                                <div class="btn-group btn-group-sm" role="group" id="timelineModoFilter">
                                    <button type="button" class="btn btn-primary active" onclick="setTimelineModo('historico', this)">Históricos</button>
                                    <button type="button" class="btn btn-outline-secondary" onclick="setTimelineModo('actual', this)" title="Anuncios que siguen publicados en Meta o Google">Actuales</button>
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
                            <div class="col-4">
                                <span class="text-danger fw-bold d-block">{{ format_data.pct_videos }}%</span>
                                <span class="text-muted" style="font-size:0.75rem;">Videos ({{ format_data.videos }})</span>
                            </div>
                            <div class="col-4">
                                <span class="text-warning fw-bold d-block">{{ format_data.pct_imagenes }}%</span>
                                <span class="text-muted" style="font-size:0.75rem;">Imágenes ({{ format_data.imagenes }})</span>
                            </div>
                            <div class="col-4">
                                <span class="text-secondary fw-bold d-block">{{ format_data.pct_otros }}%</span>
                                <span class="text-muted" style="font-size:0.75rem;">Otros ({{ format_data.otros }})</span>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Tabla de Empresas Registradas -->
            <div class="card-custom overflow-hidden">
                <div class="p-3 bg-primary bg-opacity-10 border-bottom d-flex align-items-center justify-content-between">
                    <div>
                        <h6 class="fw-bold text-primary mb-1"><i class="bi bi-buildings"></i> Empresas Monitoreadas y Volumen de Creatividades</h6>
                        <p class="small text-muted mb-0">Total de creatividades almacenadas en el sistema divididas por formato para cada marca.</p>
                    </div>
                    <span class="badge bg-primary fs-6">{{ stats_empresas|length }} empresas</span>
                </div>
                <div class="table-responsive">
                    <table class="table table-hover align-middle mb-0">
                        <thead class="table-light">
                            <tr class="small text-muted">
                                <th>#</th>
                                <th>Compañía / Marca</th>
                                <th class="text-center">Total Videos</th>
                                <th class="text-center">Total Fotos</th>
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
                                    <span class="badge bg-secondary-subtle text-secondary fw-bold fs-6 px-3 py-1">
                                        {{ emp.total_anuncios }}
                                    </span>
                                </td>
                                <td class="text-end">
                                    <a href="/?compania={{ emp.compania }}" class="btn btn-sm btn-outline-primary py-0 px-2" style="font-size: 0.75rem;" title="Filtrar anuncios de esta empresa">
                                        <i class="bi bi-funnel"></i> Ver Creatividades
                                    </a>
                                </td>
                            </tr>
                            {% else %}
                            <tr>
                                <td colspan="6" class="text-center py-5 text-muted">
                                    <i class="bi bi-folder-x fs-2 d-block mb-2"></i> No hay estadísticas de empresas disponibles.
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Panel 2: Detalle de Anuncios -->
        <div class="tab-pane fade" id="tab-ads">
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
                                        {% if 'texto' in (ad.formato|string|lower) %}<span class="text-info small fw-semibold"><i class="bi bi-fonts"></i> Texto</span>{% else %}<span class="text-warning small fw-semibold"><i class="bi bi-image"></i> Imagen</span>{% endif %}
                                    {% endif %}
                                </td>
                                <td class="small text-muted" style="max-width: 280px;">
                                    {{ (ad.texto or ad.titulo or 'Sin descripción')[:120] }}{% if (ad.texto or ad.titulo or '')|length > 120 %}...{% endif %}
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
        <div class="tab-pane fade" id="tab-winning">
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
                                        {% if 'texto' in (ad.formato|string|lower) %}<span class="text-info small fw-semibold"><i class="bi bi-fonts"></i> Texto</span>{% else %}<span class="text-warning small fw-semibold"><i class="bi bi-image"></i> Imagen</span>{% endif %}
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
        <div class="tab-pane fade" id="tab-retirados">
            <div class="card-custom overflow-hidden">
                <div class="p-3 bg-danger bg-opacity-10 border-bottom d-flex align-items-center justify-content-between">
                    <div>
                        <h6 class="fw-bold text-danger mb-1"><i class="bi bi-eye-slash"></i> Anuncios Guardados que Ya Fueron Retirados o Apagados</h6>
                        <p class="small text-muted mb-0">Campañas que existieron en Meta Ads pero actualmente ya no están activas ni circulando.</p>
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
                                        {% if 'texto' in (ad.formato|string|lower) %}<span class="text-info small fw-semibold"><i class="bi bi-fonts"></i> Texto</span>{% else %}<span class="text-warning small fw-semibold"><i class="bi bi-image"></i> Imagen</span>{% endif %}
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
        <div class="tab-pane fade" id="tab-new">
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
                                        {% if 'texto' in (ad.formato|string|lower) %}<span class="text-info small fw-semibold"><i class="bi bi-fonts"></i> Texto</span>{% else %}<span class="text-warning small fw-semibold"><i class="bi bi-image"></i> Imagen</span>{% endif %}
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
        <div class="tab-pane fade" id="tab-keywords">
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
                status.innerText = 'Scrapeando creatividades...';
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
        const totalFmt = formatData.videos + formatData.imagenes + formatData.otros;
        new Chart(document.getElementById('formatChart'), {
            type: 'doughnut',
            data: {
                labels: [
                    `Videos (${formatData.pct_videos}%)`,
                    `Imágenes (${formatData.pct_imagenes}%)`,
                    `Otros (${formatData.pct_otros}%)`
                ],
                datasets: [{
                    data: [formatData.videos, formatData.imagenes, formatData.otros],
                    backgroundColor: ['#ef4444', '#f59e0b', '#64748b'],
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
</script>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if request.form.get('password') == DASHBOARD_PASSWORD:
            session['logged_in'] = True
            registrar_acceso()
            return redirect(url_for('index'))
        else:
            error = "Contraseña incorrecta. Inténtalo de nuevo."
    return render_template_string(LOGIN_TEMPLATE, error=error)

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

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

                # Conteo agrupado por empresa (videos, fotos y total)
                if companias_bloqueadas:
                    cur.execute("""
                        SELECT 
                            compania,
                            COUNT(*) AS total_anuncios,
                            SUM(CASE WHEN LOWER(formato) LIKE '%video%' OR duracion_segundos > 0 THEN 1 ELSE 0 END) AS total_videos,
                            SUM(CASE WHEN LOWER(formato) NOT LIKE '%video%' AND (duracion_segundos IS NULL OR duracion_segundos = 0) THEN 1 ELSE 0 END) AS total_fotos
                        FROM anuncios
                        WHERE compania IS NOT NULL AND compania != ''
                        AND compania != ALL(%s)
                        GROUP BY compania
                        ORDER BY total_videos DESC, total_anuncios DESC;
                    """, (companias_bloqueadas,))
                else:
                    cur.execute("""
                        SELECT 
                            compania,
                            COUNT(*) AS total_anuncios,
                            SUM(CASE WHEN LOWER(formato) LIKE '%video%' OR duracion_segundos > 0 THEN 1 ELSE 0 END) AS total_videos,
                            SUM(CASE WHEN LOWER(formato) NOT LIKE '%video%' AND (duracion_segundos IS NULL OR duracion_segundos = 0) THEN 1 ELSE 0 END) AS total_fotos
                        FROM anuncios
                        WHERE compania IS NOT NULL AND compania != ''
                        GROUP BY compania
                        ORDER BY total_videos DESC, total_anuncios DESC;
                    """)
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

    anuncios_winning = []
    anuncios_nuevos = []
    anuncios_retirados = []

    for a in anuncios:
        fecha_detectada = extraer_fecha_anuncio(a)
        a['fecha_display'] = fecha_detectada
        dias = calcular_dias_activo(fecha_detectada)
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

    total_anuncios = len(anuncios)
    companias_set = {a['compania'] for a in anuncios if a.get('compania')}
    total_companias = len(companias_set)
    total_videos = sum(1 for a in anuncios if 'video' in str(a.get('formato', '')).lower())
    total_fotos = sum(1 for a in anuncios if 'imagen' in str(a.get('formato', '')).lower() or 'foto' in str(a.get('formato', '')).lower())
    total_otros = max(0, total_anuncios - (total_videos + total_fotos))
    total_nuevos = len(anuncios_nuevos)
    total_winning = len(anuncios_winning)
    total_retirados = len(anuncios_retirados)

    pct_videos = round((total_videos / total_anuncios * 100), 1) if total_anuncios > 0 else 0
    pct_imagenes = round((total_fotos / total_anuncios * 100), 1) if total_anuncios > 0 else 0
    pct_otros = round((total_otros / total_anuncios * 100), 1) if total_anuncios > 0 else 0

    palabras_empresas = set()
    for comp in lista_companias:
        if comp:
            tokens = re.findall(r'[a-záéíóúñ0-9]+', comp.lower())
            for t in tokens:
                palabras_empresas.add(t)

    palabras_encontradas = []
    for a in anuncios:
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

    top_companias = list(companias_set)[:7]
    timeline_data_historico = construir_timeline(anuncios, top_companias)
    anuncios_actuales_meta = [a for a in anuncios if a.get('presente_en_meta')]
    timeline_data_actual = construir_timeline(anuncios_actuales_meta, top_companias)

    format_data = {
        "videos": total_videos,
        "imagenes": total_fotos,
        "otros": total_otros,
        "pct_videos": pct_videos,
        "pct_imagenes": pct_imagenes,
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
        companias_sel=companias_sel,
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
@login_required
def guardar_urls_txt():
    raw_urls = request.form.get('raw_urls', '').strip()
    success, message = update_github_urls_file(raw_urls)
    if success:
        return redirect(url_for('index', msg="✅ Archivo urls.txt guardado en GitHub exitosamente."))
    else:
        return redirect(url_for('index', msg=f"❌ {message}"))

@app.route('/guardar_urls_google', methods=['POST'])
@login_required
def guardar_urls_google():
    raw_urls = request.form.get('raw_urls', '').strip()
    success, message = update_github_urls_file(raw_urls, URLS_GOOGLE_FILE_PATH)
    if success:
        return redirect(url_for('index', msg="✅ Archivo urls_google.txt guardado en GitHub exitosamente."))
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
        
        df['fecha_subida_detectada'] = df.apply(lambda row: extraer_fecha_anuncio(row.to_dict()), axis=1)
        df['dias_activo'] = df['fecha_subida_detectada'].apply(calcular_dias_activo)
        df['es_winning_ad'] = df.apply(lambda r: (r['dias_activo'] >= DIAS_WINNING_AD and str(r.get('estado', '')).strip().lower() == 'activo'), axis=1)

        output = io.BytesIO()
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
