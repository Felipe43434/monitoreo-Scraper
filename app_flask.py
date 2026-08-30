import os
import io
import re
import json
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta
from collections import Counter
import pandas as pd
from flask import Flask, render_template_string, request, redirect, url_for, send_file

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "meta-ads-intelligence-secret-2026")

DATABASE_URL = os.environ.get("DATABASE_URL")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")
WORKFLOW_FILE = "scraper.yml"

# Palabras comunes a ignorar en el análisis de términos
STOPWORDS_ES = {
    'de', 'la', 'que', 'el', 'en', 'y', 'a', 'los', 'del', 'se', 'las', 'por', 'un', 'para', 'con', 
    'no', 'una', 'su', 'al', 'lo', 'como', 'más', 'pero', 'sus', 'le', 'ya', 'o', 'este', 'sí', 
    'porque', 'esta', 'son', 'entre', 'está', 'cuando', 'muy', 'sin', 'sobre', 'ser', 'tiene', 
    'también', 'me', 'hasta', 'hay', 'donde', 'quien', 'desde', 'todo', 'nos', 'durante', 'todos', 
    'uno', 'les', 'ni', 'contra', 'otros', 'ese', 'eso', 'ante', 'ellos', 'e', 'esto', 'mí', 'antes', 
    'algunos', 'qué', 'unos', 'yo', 'otro', 'otras', 'otra', 'él', 'tanto', 'esa', 'estos', 'mucho', 
    'quienes', 'nada', 'muchos', 'cual', 'sea', 'poco', 'ella', 'estar', 'estas', 'algunas', 'algo', 
    'nosotros', 'mi', 'mis', 'tu', 'tus', 'te', 'ti', 'aquí', 'solo', 'cada', 'ahora', 'mas', 'si',
    'http', 'https', 'com', 'www', 'meta', 'ads', 'click', 'link'
}

def get_db_connection():
    if not DATABASE_URL:
        return None
    url = DATABASE_URL
    if "sslmode=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode=require"
    return psycopg2.connect(url)

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
        @keyframes pulse {
            0% { opacity: 1; }
            50% { opacity: 0.6; }
            100% { opacity: 1; }
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
            <!-- Formulario de Scraping -->
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

            <!-- Menú de Configuración / Engranaje -->
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

    <!-- KPIs Principales -->
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
                    <span class="stat-label">Compañías</span>
                    <i class="bi bi-building text-success fs-5"></i>
                </div>
                <div class="stat-value">{{ total_companias }}</div>
            </div>
        </div>
        <div class="col-6 col-lg-3">
            <div class="card-custom p-3">
                <div class="d-flex justify-content-between align-items-center">
                    <span class="stat-label">Videos</span>
                    <i class="bi bi-camera-video text-danger fs-5"></i>
                </div>
                <div class="stat-value">{{ total_videos }}</div>
            </div>
        </div>
        <div class="col-6 col-lg-3">
            <div class="card-custom p-3">
                <div class="d-flex justify-content-between align-items-center">
                    <span class="stat-label">Nuevos (48h)</span>
                    <i class="bi bi-stars text-warning fs-5"></i>
                </div>
                <div class="stat-value text-warning">{{ total_nuevos }}</div>
            </div>
        </div>
    </div>

    <!-- Panel de Filtros -->
    <div class="card-custom p-3 mb-4">
        <form method="GET" action="/" class="row g-2 align-items-end">
            <div class="col-md-3">
                <label class="form-label small fw-semibold text-muted mb-1"><i class="bi bi-search"></i> Buscar</label>
                <input type="text" name="q" class="form-control form-control-sm" placeholder="Texto, título, link..." value="{{ request.args.get('q', '') }}">
            </div>
            <div class="col-md-2">
                <label class="form-label small fw-semibold text-muted mb-1"><i class="bi bi-building"></i> Compañía</label>
                <select name="compania" class="form-select form-select-sm">
                    <option value="">Todas ({{ lista_companias|length }})</option>
                    {% for comp in lista_companias %}
                    <option value="{{ comp }}" {% if request.args.get('compania') == comp %}selected{% endif %}>{{ comp }}</option>
                    {% endfor %}
                </select>
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
            <div class="col-md-3 d-flex gap-2">
                <button type="submit" class="btn btn-sm btn-primary w-100"><i class="bi bi-funnel"></i> Filtrar</button>
                <a href="/" class="btn btn-sm btn-outline-secondary"><i class="bi bi-arrow-counterclockwise"></i></a>
                <a href="/descargar_excel?{{ request.query_string.decode() }}" class="btn btn-sm btn-success text-nowrap"><i class="bi bi-file-earmark-excel"></i> Exportar</a>
            </div>
        </form>
    </div>

    <!-- Pestañas de Navegación -->
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
            <button class="nav-link fw-semibold position-relative" data-bs-toggle="tab" data-bs-target="#tab-new" type="button">
                <i class="bi bi-stars text-warning"></i> Nuevos Anuncios
                {% if total_nuevos > 0 %}
                <span class="badge rounded-pill bg-danger ms-1">{{ total_nuevos }}</span>
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
                        <div style="height: 320px;">
                            <canvas id="timelineChart"></canvas>
                        </div>
                    </div>
                </div>
                <div class="col-lg-4">
                    <div class="card-custom p-3 h-100">
                        <h6 class="fw-bold mb-3"><i class="bi bi-pie-chart"></i> Distribución de Formatos</h6>
                        <div style="height: 320px;">
                            <canvas id="formatChart"></canvas>
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
                                <th>Estado</th>
                                <th>Formato</th>
                                <th>Copia / Texto</th>
                                <th>Fecha Inicio</th>
                                <th>Acción</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for ad in anuncios %}
                            <tr>
                                <td class="fw-bold">{{ ad.compania or 'N/A' }}</td>
                                <td>
                                    <span class="badge {% if ad.estado == 'Activo' %}badge-active{% else %}badge-inactive{% endif %}">
                                        {{ ad.estado or 'Desconocido' }}
                                    </span>
                                </td>
                                <td>
                                    {% if 'video' in (ad.formato|string|lower) %}
                                        <span class="text-danger small fw-semibold"><i class="bi bi-camera-video"></i> Video</span>
                                    {% else %}
                                        <span class="text-warning small fw-semibold"><i class="bi bi-image"></i> Imagen</span>
                                    {% endif %}
                                </td>
                                <td class="small text-muted" style="max-width: 320px;">
                                    {{ (ad.texto or ad.titulo or 'Sin descripción')[:120] }}{% if (ad.texto or ad.titulo or '')|length > 120 %}...{% endif %}
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
                                <td colspan="6" class="text-center py-5 text-muted">
                                    <i class="bi bi-folder-x fs-2 d-block mb-2"></i> No hay registros disponibles
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Panel 3: Nuevos Anuncios Recientes -->
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

        <!-- Panel 4: Términos Más Repetidos -->
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
    // Modo Oscuro / Claro
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

    // Gráficas
    const timelineData = {{ timeline_data|tojson }};
    const formatData = {{ format_data|tojson }};
    const keywordsData = {{ keywords_chart_data|tojson }};

    if (document.getElementById('timelineChart') && timelineData.labels.length > 0) {
        new Chart(document.getElementById('timelineChart'), {
            type: 'line',
            data: {
                labels: timelineData.labels,
                datasets: timelineData.datasets
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { position: 'bottom' } },
                scales: { y: { beginAtZero: true, ticks: { precision: 0 } } }
            }
        });
    }

    if (document.getElementById('formatChart')) {
        new Chart(document.getElementById('formatChart'), {
            type: 'doughnut',
            data: {
                labels: ['Videos', 'Imágenes', 'Otros'],
                datasets: [{
                    data: [formatData.videos, formatData.imagenes, formatData.otros],
                    backgroundColor: ['#ef4444', '#f59e0b', '#64748b']
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { position: 'bottom' } }
            }
        });
    }

    if (document.getElementById('keywordsChart') && keywordsData.labels.length > 0) {
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

@app.route('/')
def index():
    msg = request.args.get('msg')
    q = request.args.get('q', '').strip()
    compania = request.args.get('compania', '').strip()
    estado = request.args.get('estado', '').strip()
    formato = request.args.get('formato', '').strip()

    conn = get_db_connection()
    anuncios = []
    lista_companias = []
    anuncios_nuevos = []

    if conn:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT DISTINCT compania FROM anuncios WHERE compania IS NOT NULL ORDER BY compania ASC")
                lista_companias = [r['compania'] for r in cur.fetchall()]

                query = "SELECT * FROM anuncios WHERE 1=1"
                params = []

                if q:
                    query += " AND (texto ILIKE %s OR titulo ILIKE %s OR link_individual ILIKE %s)"
                    like_val = f"%{q}%"
                    params.extend([like_val, like_val, like_val])
                if compania:
                    query += " AND compania = %s"
                    params.append(compania)
                if estado:
                    query += " AND estado = %s"
                    params.append(estado)
                if formato:
                    query += " AND formato ILIKE %s"
                    params.append(f"%{formato}%")

                query += " ORDER BY fecha_inicio DESC NULLS LAST LIMIT 500"
                cur.execute(query, tuple(params))
                anuncios = cur.fetchall()

                # Anuncios de las últimas 48 horas
                limite_reciente = (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d')
                cur.execute("SELECT * FROM anuncios WHERE fecha_inicio >= %s ORDER BY fecha_inicio DESC LIMIT 100", (limite_reciente,))
                anuncios_nuevos = cur.fetchall()
        except Exception as e:
            print(f"Error consultando BD: {e}")
            anuncios = []
        finally:
            conn.close()

    total_anuncios = len(anuncios)
    companias_set = {a['compania'] for a in anuncios if a.get('compania')}
    total_companias = len(companias_set)
    total_videos = sum(1 for a in anuncios if 'video' in str(a.get('formato', '')).lower())
    total_fotos = sum(1 for a in anuncios if 'imagen' in str(a.get('formato', '')).lower() or 'foto' in str(a.get('formato', '')).lower())
    total_otros = max(0, total_anuncios - (total_videos + total_fotos))
    total_nuevos = len(anuncios_nuevos)

    # 1. Extracción de términos más repetidos (Keywords)
    palabras_encontradas = []
    for a in anuncios:
        texto_completo = f"{a.get('texto') or ''} {a.get('titulo') or ''}".lower()
        palabras = re.findall(r'[a-záéíóúñ]{4,}', texto_completo)
        palabras_limpias = [p for p in palabras if p not in STOPWORDS_ES]
        palabras_encontradas.extend(palabras_limpias)

    contador_palabras = Counter(palabras_encontradas)
    top_palabras = contador_palabras.most_common(12)

    keywords_chart_data = {
        "labels": [p[0].capitalize() for p in top_palabras],
        "values": [p[1] for p in top_palabras]
    }

    # 2. Datos de tendencias de fechas
    timeline_dict = {}
    for a in anuncios:
        f_str = str(a.get('fecha_inicio', ''))[:10]
        if f_str and len(f_str) == 10:
            comp = a.get('compania', 'Otras')
            if f_str not in timeline_dict:
                timeline_dict[f_str] = {}
            timeline_dict[f_str][comp] = timeline_dict[f_str].get(comp, 0) + 1

    sorted_dates = sorted(timeline_dict.keys())
    top_companias = list(companias_set)[:5]
    colors = ['#0284c7', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6']

    datasets = []
    for idx, comp in enumerate(top_companias):
        data = [timeline_dict[d].get(comp, 0) for d in sorted_dates]
        datasets.append({
            "label": comp,
            "data": data,
            "borderColor": colors[idx % len(colors)],
            "backgroundColor": colors[idx % len(colors)],
            "tension": 0.3
        })

    timeline_data = {"labels": sorted_dates, "datasets": datasets}
    format_data = {"videos": total_videos, "imagenes": total_fotos, "otros": total_otros}

    return render_template_string(
        HTML_TEMPLATE,
        anuncios=anuncios,
        anuncios_nuevos=anuncios_nuevos,
        lista_companias=lista_companias,
        total_anuncios=total_anuncios,
        total_companias=total_companias,
        total_videos=total_videos,
        total_fotos=total_fotos,
        total_nuevos=total_nuevos,
        top_palabras=top_palabras,
        keywords_chart_data=keywords_chart_data,
        timeline_data=timeline_data,
        format_data=format_data,
        msg=msg
    )

@app.route('/lanzar_scraper', methods=['POST'])
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
def descargar_excel():
    conn = get_db_connection()
    if not conn:
        return redirect(url_for('index', msg="Error: Base de datos no disponible."))

    try:
        q = request.args.get('q', '').strip()
        compania = request.args.get('compania', '').strip()
        estado = request.args.get('estado', '').strip()
        formato = request.args.get('formato', '').strip()

        query = "SELECT compania, estado, formato, texto, titulo, link_individual, fecha_inicio FROM anuncios WHERE 1=1"
        params = []

        if q:
            query += " AND (texto ILIKE %s OR titulo ILIKE %s OR link_individual ILIKE %s)"
            like_val = f"%{q}%"
            params.extend([like_val, like_val, like_val])
        if compania:
            query += " AND compania = %s"
            params.append(compania)
        if estado:
            query += " AND estado = %s"
            params.append(estado)
        if formato:
            query += " AND formato ILIKE %s"
            params.append(f"%{formato}%")

        df = pd.read_sql_query(query, conn, params=params)
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
