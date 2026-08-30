import os
import io
import re
import json
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta
from collections import Counter
from functools import wraps
import pandas as pd
from flask import Flask, render_template_string, request, redirect, url_for, send_file, session

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "meta-ads-intelligence-secret-2026")

DATABASE_URL = os.environ.get("DATABASE_URL")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")
WORKFLOW_FILE = "scraper.yml"

DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "UnaClaveMuySegura2026")

# Umbral en días para considerar un anuncio como "Winning Ad"
DIAS_WINNING_AD = 30

MESES_DICT = {
    'ene': '01', 'feb': '02', 'mar': '03', 'abr': '04', 'may': '05', 'jun': '06',
    'jul': '07', 'ago': '08', 'sep': '09', 'oct': '10', 'nov': '11', 'dic': '12',
    'enero': '01', 'febrero': '02', 'marzo': '03', 'abril': '04', 'mayo': '05', 'junio': '06',
    'julio': '07', 'agosto': '08', 'septiembre': '09', 'octubre': '10', 'noviembre': '11', 'diciembre': '12',
    'jan': '01', 'apr': '04', 'aug': '08', 'dec': '12'
}

# Palabras comunes a ignorar en el análisis de términos
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
    # Filtros personalizados
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
    return psycopg2.connect(url)

def parse_date_str(val, fallback_val=None):
    if not val and fallback_val:
        val = fallback_val
    if not val:
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

def calcular_dias_activo(fecha_inicio_str, fallback_str=None):
    f_norm = parse_date_str(fecha_inicio_str, fallback_str)
    if not f_norm:
        return 0
    try:
        dt = datetime.strptime(f_norm, '%Y-%m-%d')
        diff = (datetime.now() - dt).days
        return max(0, diff)
    except Exception:
        return 0

LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="es" data-bs-theme="light">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Acceso | Meta Ads Intelligence</title>
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
        <p class="text-secondary small">Meta Ads Intelligence Dashboard</p>
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
    <title>Meta Ads Intelligence</title>
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
        @keyframes pulse {
            0% { opacity: 1; }
            50% { opacity: 0.6; }
            100% { opacity: 1; }
        }
        .dropdown-menu-scroll {
            max-height: 250px;
            overflow-y: auto;
        }
    </style>
</head>
<body>

<nav class="navbar navbar-expand-lg navbar-dark px-3 py-2 sticky-top">
    <div class="container-fluid">
        <a class="navbar-brand d-flex align-items-center gap-2" href="/">
            <i class="bi bi-graph-up-arrow text-primary fs-4"></i>
            <span class="fw-bold tracking-tight">Meta Ads Intelligence</span>
        </a>
        <div class="d-flex align-items-center gap-2 ms-auto">
            <form action="/lanzar_scraper" method="POST" class="d-flex align-items-center gap-2 m-0">
                <select name="dias_scraping" class="form-select form-select-sm bg-dark text-light border-secondary">
                    <option value="7">7 días</option>
                    <option value="15">15 días</option>
                    <option value="30" selected>30 días</option>
                    <option value="60">60 días</option>
                </select>
                <button type="submit" class="btn btn-sm btn-primary text-nowrap d-flex align-items-center gap-1">
                    <i class="bi bi-arrow-repeat"></i> Sincronizar
                </button>
            </form>

            <div class="dropdown">
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

<div class="container-fluid px-4 py-4">

    {% if msg %}
    <div class="alert alert-info alert-dismissible fade show border-0 shadow-sm" role="alert">
        {{ msg }}
        <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
    </div>
    {% endif %}

    <!-- KPIs -->
    <div class="row g-3 mb-4">
        <div class="col-6 col-lg-3">
            <div class="card-custom p-3">
                <div class="d-flex justify-content-between align-items-center">
                    <span class="stat-label">Total Anuncios</span>
                    <i class="bi bi-collection-play text-primary fs-5"></i>
                </div>
                <div class="stat-value">{{ total_anuncios }}</div>
            </div>
        </div>
        <div class="col-6 col-lg-3">
            <div class="card-custom p-3">
                <div class="d-flex justify-content-between align-items-center">
                    <span class="stat-label">Winning Ads (+30d)</span>
                    <i class="bi bi-fire text-danger fs-5"></i>
                </div>
                <div class="stat-value text-danger">{{ total_winning }}</div>
            </div>
        </div>
        <div class="col-6 col-lg-3">
            <div class="card-custom p-3">
                <div class="d-flex justify-content-between align-items-center">
                    <span class="stat-label">Videos</span>
                    <i class="bi bi-camera-video text-warning fs-5"></i>
                </div>
                <div class="stat-value">{{ total_videos }}</div>
            </div>
        </div>
        <div class="col-6 col-lg-3">
            <div class="card-custom p-3">
                <div class="d-flex justify-content-between align-items-center">
                    <span class="stat-label">Nuevos (48h)</span>
                    <i class="bi bi-stars text-info fs-5"></i>
                </div>
                <div class="stat-value text-info">{{ total_nuevos }}</div>
            </div>
        </div>
    </div>

    <!-- Panel de Filtros -->
    <div class="card-custom p-3 mb-4">
        <form method="GET" action="/" id="filterForm" class="row g-2 align-items-end">
            <div class="col-md-3">
                <label class="form-label small fw-semibold text-muted mb-1"><i class="bi bi-search"></i> Buscar</label>
                <input type="text" name="q" class="form-control form-control-sm" placeholder="Texto, título, link..." value="{{ request.args.get('q', '') }}">
            </div>

            <!-- Filtro Selección Múltiple Compañías -->
            <div class="col-md-3">
                <label class="form-label small fw-semibold text-muted mb-1"><i class="bi bi-building"></i> Compañías ({% if companias_sel %}{{ companias_sel|length }} selec.{% else %}Todas{% endif %})</label>
                <div class="dropdown">
                    <button class="btn btn-sm btn-outline-secondary w-100 text-start d-flex justify-content-between align-items-center" type="button" data-bs-toggle="dropdown" data-bs-auto-close="outside">
                        <span class="text-truncate">
                            {% if companias_sel %}
                                {{ companias_sel|join(', ') }}
                            {% else %}
                                Todas las compañías ({{ lista_companias|length }})
                            {% endif %}
                        </span>
                        <i class="bi bi-chevron-down ms-1"></i>
                    </button>
                    <div class="dropdown-menu dropdown-menu-scroll p-2 w-100 shadow">
                        <div class="form-check pb-1 mb-1 border-bottom">
                            <input class="form-check-input" type="checkbox" id="selectAllCompanies" onchange="toggleAllCompanies(this)">
                            <label class="form-check-label small fw-bold" for="selectAllCompanies">Seleccionar / Deseleccionar Todo</label>
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

            <div class="col-md-2">
                <label class="form-label small fw-semibold text-muted mb-1"><i class="bi bi-toggle-on"></i> Estado</label>
                <select name="estado" class="form-select form-select-sm">
                    <option value="">Todos</option>
                    <option value="Activo" {% if request.args.get('estado') == 'Activo' %}selected{% endif %}>Activo</option>
                    <option value="Inactivo" {% if request.args.get('estado') == 'Inactivo' %}selected{% endif %}>Inactivo</option>
                </select>
            </div>
            <div class="col-md-2">
                <label class="form-label small fw-semibold text-muted mb-1"><i class="bi bi-play-circle"></i> Formato</label>
                <select name="formato" class="form-select form-select-sm">
                    <option value="">Todos</option>
                    <option value="video" {% if request.args.get('formato') == 'video' %}selected{% endif %}>Video</option>
                    <option value="imagen" {% if request.args.get('formato') == 'imagen' %}selected{% endif %}>Imagen</option>
                </select>
            </div>
            <div class="col-md-2 d-flex gap-2">
                <button type="submit" class="btn btn-sm btn-primary w-100"><i class="bi bi-funnel"></i> Filtrar</button>
                <a href="/" class="btn btn-sm btn-outline-secondary"><i class="bi bi-arrow-counterclockwise"></i></a>
                <a href="/descargar_excel?{{ request.query_string.decode() }}" class="btn btn-sm btn-success text-nowrap"><i class="bi bi-file-earmark-excel"></i> Exportar</a>
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
            <button class="nav-link fw-semibold position-relative" data-bs-toggle="tab" data-bs-target="#tab-new" type="button">
                <i class="bi bi-stars text-info"></i> Nuevos Anuncios
                {% if total_nuevos > 0 %}
                <span class="badge rounded-pill bg-info ms-1">{{ total_nuevos }}</span>
                {% endif %}
            </button>
        </li>
        <li class="nav-item">
            <button class="nav-link fw-semibold" data-bs-toggle="tab" data-bs-target="#tab-keywords" type="button">
                <i class="bi bi-chat-square-quote"></i> Términos Frecuentes
            </button>
        </li>
    </ul>

    <div class="tab-content">
        <!-- Panel 1: Gráficas -->
        <div class="tab-pane fade show active" id="tab-charts">
            <div class="row g-3">
                <div class="col-lg-8">
                    <div class="card-custom p-3 h-100">
                        <h6 class="fw-bold mb-3"><i class="bi bi-graph-up"></i> Publicación de Anuncios por Empresa</h6>
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
                                <th>Formato</th>
                                <th>Copia / Texto</th>
                                <th>Tiempo Activo</th>
                                <th>Fecha Inicio</th>
                                <th>Acción</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for ad in anuncios %}
                            <tr>
                                <td class="fw-bold">{{ ad.compania or 'N/A' }}</td>
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
                                <td>
                                    {% if 'video' in (ad.formato|string|lower) %}
                                        <span class="text-danger small fw-semibold"><i class="bi bi-camera-video"></i> Video</span>
                                    {% else %}
                                        <span class="text-warning small fw-semibold"><i class="bi bi-image"></i> Imagen</span>
                                    {% endif %}
                                </td>
                                <td class="small text-muted" style="max-width: 300px;">
                                    {{ (ad.texto or ad.titulo or 'Sin descripción')[:120] }}{% if (ad.texto or ad.titulo or '')|length > 120 %}...{% endif %}
                                </td>
                                <td class="small">
                                    <span class="fw-bold {% if ad.es_winning %}text-danger{% else %}text-muted{% endif %}">
                                        {{ ad.dias_activo }} días
                                    </span>
                                </td>
                                <td class="small">{{ ad.fecha_inicio or 'N/A' }}</td>
                                <td>
                                    {% if ad.link_individual %}
                                    <a href="{{ ad.link_individual }}" target="_blank" class="btn btn-sm btn-outline-primary py-0 px-2" style="font-size: 0.75rem;">
                                        <i class="bi bi-box-arrow-up-right"></i> Ver en Meta
                                    </a>
                                    {% else %}
                                    <span class="text-muted small">-</span>
                                    {% endif %}
                                </td>
                            </tr>
                            {% else %}
                            <tr>
                                <td colspan="7" class="text-center py-5 text-muted">
                                    <i class="bi bi-folder-x fs-2 d-block mb-2"></i> No hay registros disponibles
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Panel 3: Winning Ads (Alto Rendimiento) -->
        <div class="tab-pane fade" id="tab-winning">
            <div class="card-custom overflow-hidden">
                <div class="p-3 bg-danger bg-opacity-10 border-bottom d-flex align-items-center justify-content-between">
                    <div>
                        <h6 class="fw-bold text-danger mb-1"><i class="bi bi-fire"></i> Anuncios de Alto Rendimiento (Longevidad > 30 días)</h6>
                        <p class="small text-muted mb-0">Estas campañas han superado el mes continuas en circulación, indicando alta rentabilidad.</p>
                    </div>
                    <span class="badge bg-danger fs-6">{{ anuncios_winning|length }} detectados</span>
                </div>
                <div class="table-responsive">
                    <table class="table table-hover align-middle mb-0">
                        <thead class="table-light">
                            <tr class="small text-muted">
                                <th>Empresa</th>
                                <th>Insignia</th>
                                <th>Formato</th>
                                <th>Texto</th>
                                <th>Días Activo</th>
                                <th>Fecha Inicio</th>
                                <th>Enlace</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for ad in anuncios_winning %}
                            <tr>
                                <td class="fw-bold">{{ ad.compania or 'N/A' }}</td>
                                <td><span class="badge badge-winning">🔥 Winning Ad</span></td>
                                <td>
                                    {% if 'video' in (ad.formato|string|lower) %}
                                        <span class="text-danger small fw-semibold"><i class="bi bi-camera-video"></i> Video</span>
                                    {% else %}
                                        <span class="text-warning small fw-semibold"><i class="bi bi-image"></i> Imagen</span>
                                    {% endif %}
                                </td>
                                <td class="small text-muted" style="max-width: 320px;">
                                    {{ (ad.texto or ad.titulo or 'Sin descripción')[:140] }}
                                </td>
                                <td><span class="badge bg-danger-subtle text-danger fw-bold">{{ ad.dias_activo }} días</span></td>
                                <td class="small">{{ ad.fecha_inicio or 'N/A' }}</td>
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
                                    <i class="bi bi-shield-check fs-2 d-block mb-2 text-warning"></i> No hay campañas con más de 30 días activos en los filtros actuales.
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Panel 4: Nuevos Anuncios -->
        <div class="tab-pane fade" id="tab-new">
            <div class="card-custom overflow-hidden">
                <div class="table-responsive">
                    <table class="table table-hover align-middle mb-0">
                        <thead class="table-light">
                            <tr class="small text-muted">
                                <th>Empresa</th>
                                <th>Distintivo</th>
                                <th>Formato</th>
                                <th>Texto</th>
                                <th>Fecha Inicio</th>
                                <th>Enlace</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for ad in anuncios_nuevos %}
                            <tr>
                                <td class="fw-bold">{{ ad.compania or 'N/A' }}</td>
                                <td><span class="badge badge-new"><i class="bi bi-stars"></i> Nuevo</span></td>
                                <td>
                                    {% if 'video' in (ad.formato|string|lower) %}
                                        <span class="text-danger small fw-semibold"><i class="bi bi-camera-video"></i> Video</span>
                                    {% else %}
                                        <span class="text-warning small fw-semibold"><i class="bi bi-image"></i> Imagen</span>
                                    {% endif %}
                                </td>
                                <td class="small text-muted" style="max-width: 320px;">
                                    {{ (ad.texto or ad.titulo or 'Sin descripción')[:140] }}
                                </td>
                                <td class="small">{{ ad.fecha_inicio or 'N/A' }}</td>
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
                                <td colspan="6" class="text-center py-5 text-muted">
                                    <i class="bi bi-check2-circle fs-2 d-block mb-2 text-success"></i> No se han detectado nuevos anuncios en las últimas 48 horas.
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Panel 5: Términos Frecuentes -->
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

    const timelineData = {{ timeline_data|tojson }};
    const formatData = {{ format_data|tojson }};
    const keywordsData = {{ keywords_chart_data|tojson }};

    if (document.getElementById('timelineChart')) {
        const labels = timelineData.labels || [];
        const datasets = timelineData.datasets || [];
        new Chart(document.getElementById('timelineChart'), {
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
                    legend: { position: 'bottom', labels: { boxWidth: 12, usePointStyle: true } }
                },
                scales: {
                    y: { beginAtZero: true, ticks: { precision: 0 } },
                    x: { grid: { display: false } }
                }
            }
        });
    }

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

    conn = get_db_connection()
    anuncios = []
    lista_companias = []
    anuncios_nuevos = []

    if conn:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT DISTINCT compania FROM anuncios WHERE compania IS NOT NULL AND compania != '' ORDER BY compania ASC")
                lista_companias = [r['compania'] for r in cur.fetchall()]

                query = "SELECT * FROM anuncios WHERE 1=1"
                params = []

                if q:
                    query += " AND (texto ILIKE %s OR titulo ILIKE %s OR link_individual ILIKE %s)"
                    like_val = f"%{q}%"
                    params.extend([like_val, like_val, like_val])
                if companias_sel:
                    query += " AND compania = ANY(%s)"
                    params.append(companias_sel)
                if estado:
                    query += " AND estado = %s"
                    params.append(estado)
                if formato:
                    query += " AND formato ILIKE %s"
                    params.append(f"%{formato}%")

                query += " ORDER BY id DESC LIMIT 1000"
                cur.execute(query, tuple(params))
                anuncios = cur.fetchall()

                limite_reciente = (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d')
                cur.execute("SELECT * FROM anuncios WHERE (fecha_inicio >= %s OR fecha_registro >= %s) ORDER BY id DESC LIMIT 100", (limite_reciente, limite_reciente))
                anuncios_nuevos = cur.fetchall()
        except Exception as e:
            print(f"Error consultando BD: {e}")
            anuncios = []
        finally:
            conn.close()

    # Cálculo de Días Activos y Winning Ads (+30 días)
    anuncios_winning = []
    for a in anuncios:
        dias = calcular_dias_activo(a.get('fecha_inicio'), a.get('fecha_registro'))
        a['dias_activo'] = dias
        a['es_winning'] = dias >= DIAS_WINNING_AD
        if a['es_winning']:
            anuncios_winning.append(a)

    total_anuncios = len(anuncios)
    companias_set = {a['compania'] for a in anuncios if a.get('compania')}
    total_companias = len(companias_set)
    total_videos = sum(1 for a in anuncios if 'video' in str(a.get('formato', '')).lower())
    total_fotos = sum(1 for a in anuncios if 'imagen' in str(a.get('formato', '')).lower() or 'foto' in str(a.get('formato', '')).lower())
    total_otros = max(0, total_anuncios - (total_videos + total_fotos))
    total_nuevos = len(anuncios_nuevos)
    total_winning = len(anuncios_winning)

    pct_videos = round((total_videos / total_anuncios * 100), 1) if total_anuncios > 0 else 0
    pct_imagenes = round((total_fotos / total_anuncios * 100), 1) if total_anuncios > 0 else 0
    pct_otros = round((total_otros / total_anuncios * 100), 1) if total_anuncios > 0 else 0

    # Exclusión de palabras clave y nombres de empresas
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

    # Gráfico de Tendencias
    timeline_dict = {}
    for a in anuncios:
        f_norm = parse_date_str(a.get('fecha_inicio'), a.get('fecha_registro'))
        if f_norm:
            comp = a.get('compania', 'Otras')
            if f_norm not in timeline_dict:
                timeline_dict[f_norm] = {}
            timeline_dict[f_norm][comp] = timeline_dict[f_norm].get(comp, 0) + 1

    sorted_dates = sorted(timeline_dict.keys())
    top_companias = list(companias_set)[:6]
    palette = ['#0284c7', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#ec4899']

    datasets = []
    for idx, comp in enumerate(top_companias):
        data = [timeline_dict[d].get(comp, 0) for d in sorted_dates]
        datasets.append({
            "label": comp,
            "data": data,
            "borderColor": palette[idx % len(palette)],
            "backgroundColor": palette[idx % len(palette)],
            "tension": 0.25,
            "pointRadius": 3
        })

    timeline_data = {"labels": sorted_dates, "datasets": datasets}
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
        lista_companias=lista_companias,
        companias_sel=companias_sel,
        total_anuncios=total_anuncios,
        total_companias=total_companias,
        total_videos=total_videos,
        total_fotos=total_fotos,
        total_nuevos=total_nuevos,
        total_winning=total_winning,
        top_palabras=top_palabras,
        keywords_chart_data=keywords_chart_data,
        timeline_data=timeline_data,
        format_data=format_data,
        msg=msg
    )

@app.route('/lanzar_scraper', methods=['POST'])
@login_required
def lanzar_scraper():
    dias = request.form.get("dias_scraping", "30")

    if not GITHUB_TOKEN or not GITHUB_REPO:
        return redirect(url_for('index', msg="❌ Falta configurar GITHUB_TOKEN o GITHUB_REPO en Render."))

    url_api = f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    payload = {
        "ref": "main",
        "inputs": {"dias": str(dias)}
    }

    try:
        response = requests.post(url_api, json=payload, headers=headers, timeout=10)
        if response.status_code == 204:
            return redirect(url_for('index', msg=f"🚀 Scraping iniciado en GitHub Actions ({dias} días). Los datos se actualizarán en minutos."))
        else:
            return redirect(url_for('index', msg=f"⚠️ GitHub respondió con código {response.status_code}: {response.text}"))
    except Exception as e:
        return redirect(url_for('index', msg=f"❌ Error al conectar con GitHub Actions: {e}"))

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

        query = "SELECT compania, estado, formato, texto, titulo, link_individual, fecha_inicio, fecha_registro FROM anuncios WHERE 1=1"
        params = []

        if q:
            query += " AND (texto ILIKE %s OR titulo ILIKE %s OR link_individual ILIKE %s)"
            like_val = f"%{q}%"
            params.extend([like_val, like_val, like_val])
        if companias_sel:
            query += " AND compania = ANY(%s)"
            params.append(companias_sel)
        if estado:
            query += " AND estado = %s"
            params.append(estado)
        if formato:
            query += " AND formato ILIKE %s"
            params.append(f"%{formato}%")

        query += " ORDER BY id DESC"

        df = pd.read_sql_query(query, conn, params=params)
        
        df['dias_activo'] = df.apply(lambda row: calcular_dias_activo(row.get('fecha_inicio'), row.get('fecha_registro')), axis=1)
        df['es_winning_ad'] = df['dias_activo'] >= DIAS_WINNING_AD

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
