import os
import re
import json
import io
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta
import pandas as pd
from flask import Flask, render_template_string, request, redirect, url_for, send_file, flash

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "super-secret-key-meta-ads-2026")

# Configuración de Entorno
DATABASE_URL = os.environ.get("DATABASE_URL")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")
WORKFLOW_FILE = "scraper.yml"

def get_db_connection():
    """Establece conexión con la base de datos PostgreSQL de Neon."""
    if not DATABASE_URL:
        return None
    # Neon usa SSL requerido
    url = DATABASE_URL
    if "sslmode=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode=require"
    return psycopg2.connect(url)

# Plantilla HTML / Bootstrap / Chart.js integrada
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Meta Ads Intelligence Dashboard</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.0/font/bootstrap-icons.css" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body { background-color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; color: #1e293b; }
        .navbar { background-color: #0f172a; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        .card-stat { border-radius: 12px; border: 1px solid #e2e8f0; background: white; transition: all 0.2s; }
        .card-stat:hover { transform: translateY(-2px); box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); }
        .stat-value { font-size: 1.8rem; font-weight: 700; color: #0f172a; }
        .stat-label { font-size: 0.8rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: #64748b; }
        .filter-panel { background: white; border-radius: 12px; border: 1px solid #e2e8f0; padding: 20px; margin-bottom: 24px; }
        .ad-card { border: 1px solid #e2e8f0; border-radius: 10px; background: white; margin-bottom: 16px; overflow: hidden; }
        .badge-active { background-color: #10b981; color: white; }
        .badge-inactive { background-color: #64748b; color: white; }
    </style>
</head>
<body>

<nav class="navbar navbar-dark px-4 py-3">
    <div class="container-fluid">
        <a class="navbar-brand d-flex align-items-center gap-2" href="/">
            <i class="bi bi-graph-up-arrow text-primary fs-4"></i>
            <span class="fw-bold">Meta Ads Intelligence</span>
            <small class="text-secondary ms-2 d-none d-md-inline">Supervisión y Análisis Competitivo</small>
        </a>
        <div class="d-flex align-items-center gap-3">
            <form action="/lanzar_scraper" method="POST" class="d-flex align-items-center gap-2 m-0">
                <select name="dias_scraping" class="form-select form-select-sm bg-dark text-light border-secondary" style="width: auto;">
                    <option value="7">Últimos 7 días</option>
                    <option value="15">Últimos 15 días</option>
                    <option value="30" selected>Últimos 30 días</option>
                    <option value="60">Últimos 60 días</option>
                </select>
                <button type="submit" class="btn btn-sm btn-primary d-flex align-items-center gap-1">
                    <i class="bi bi-arrow-repeat"></i> Sincronizar Datos
                </button>
            </form>
        </div>
    </div>
</nav>

<div class="container-fluid px-4 py-4">

    <!-- Mensajes de Notificación -->
    {% if msg %}
    <div class="alert alert-info alert-dismissible fade show" role="alert">
        {{ msg }}
        <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
    </div>
    {% endif %}

    <!-- KPIs Principales -->
    <div class="row g-3 mb-4">
        <div class="col-6 col-md-3">
            <div class="card-stat p-3">
                <div class="d-flex justify-content-between align-items-center">
                    <span class="stat-label">Total Filtrados</span>
                    <i class="bi bi-collection-play text-primary fs-5"></i>
                </div>
                <div class="stat-value">{{ total_anuncios }}</div>
            </div>
        </div>
        <div class="col-6 col-md-3">
            <div class="card-stat p-3">
                <div class="d-flex justify-content-between align-items-center">
                    <span class="stat-label">Compañías</span>
                    <i class="bi bi-building text-success fs-5"></i>
                </div>
                <div class="stat-value">{{ total_companias }}</div>
            </div>
        </div>
        <div class="col-6 col-md-3">
            <div class="card-stat p-3">
                <div class="d-flex justify-content-between align-items-center">
                    <span class="stat-label">Videos</span>
                    <i class="bi bi-camera-video text-danger fs-5"></i>
                </div>
                <div class="stat-value">{{ total_videos }}</div>
            </div>
        </div>
        <div class="col-6 col-md-3">
            <div class="card-stat p-3">
                <div class="d-flex justify-content-between align-items-center">
                    <span class="stat-label">Fotos / Imágenes</span>
                    <i class="bi bi-image text-warning fs-5"></i>
                </div>
                <div class="stat-value">{{ total_fotos }}</div>
            </div>
        </div>
    </div>

    <!-- Panel de Filtros -->
    <div class="filter-panel shadow-sm">
        <form method="GET" action="/" class="row g-3">
            <div class="col-md-3">
                <label class="form-label small fw-semibold"><i class="bi bi-search"></i> Buscar Término / Link / ID</label>
                <input type="text" name="q" class="form-control form-control-sm" placeholder="Ej: crédito, préstamo..." value="{{ request.args.get('q', '') }}">
            </div>
            <div class="col-md-2">
                <label class="form-label small fw-semibold"><i class="bi bi-building"></i> Compañías</label>
                <select name="compania" class="form-select form-select-sm">
                    <option value="">Todas ({{ lista_companias|length }})</option>
                    {% for comp in lista_companias %}
                    <option value="{{ comp }}" {% if request.args.get('compania') == comp %}selected{% endif %}>{{ comp }}</option>
                    {% endfor %}
                </select>
            </div>
            <div class="col-md-2">
                <label class="form-label small fw-semibold"><i class="bi bi-toggle-on"></i> Estado</label>
                <select name="estado" class="form-select form-select-sm">
                    <option value="">Todos</option>
                    <option value="Activo" {% if request.args.get('estado') == 'Activo' %}selected{% endif %}>Activo</option>
                    <option value="Inactivo" {% if request.args.get('estado') == 'Inactivo' %}selected{% endif %}>Inactivo</option>
                </select>
            </div>
            <div class="col-md-2">
                <label class="form-label small fw-semibold"><i class="bi bi-file-earmark-play"></i> Formato</label>
                <select name="formato" class="form-select form-select-sm">
                    <option value="">Todos</option>
                    <option value="video" {% if request.args.get('formato') == 'video' %}selected{% endif %}>Video</option>
                    <option value="imagen" {% if request.args.get('formato') == 'imagen' %}selected{% endif %}>Imagen</option>
                </select>
            </div>
            <div class="col-md-3 d-flex align-items-end gap-2">
                <button type="submit" class="btn btn-sm btn-dark w-100"><i class="bi bi-funnel"></i> Filtrar</button>
                <a href="/" class="btn btn-sm btn-outline-secondary"><i class="bi bi-arrow-counterclockwise"></i></a>
                <a href="/descargar_excel?{{ request.query_string.decode() }}" class="btn btn-sm btn-success text-nowrap"><i class="bi bi-file-earmark-excel"></i> Exportar</a>
            </div>
        </form>
    </div>

    <!-- Pestañas de Vista -->
    <ul class="nav nav-tabs mb-4" id="viewTab" role="tablist">
        <li class="nav-item" role="presentation">
            <button class="nav-link active fw-semibold" id="charts-tab" data-bs-toggle="tab" data-bs-target="#charts" type="button" role="tab">
                <i class="bi bi-bar-chart-line"></i> Vista General & Tendencias
            </button>
        </li>
        <li class="nav-item" role="presentation">
            <button class="nav-link fw-semibold" id="ads-tab" data-bs-toggle="tab" data-bs-target="#ads" type="button" role="tab">
                <i class="bi bi-list-columns"></i> Detalle de Anuncios ({{ anuncios|length }})
            </button>
        </li>
    </ul>

    <div class="tab-content" id="viewTabContent">
        <!-- Gráficos -->
        <div class="tab-pane fade show active" id="charts" role="tabpanel">
            <div class="row g-4">
                <div class="col-lg-8">
                    <div class="card p-3 shadow-sm h-100">
                        <h6 class="fw-bold mb-3"><i class="bi bi-graph-up"></i> Publicación de Anuncios por Empresa</h6>
                        <div style="height: 320px;">
                            <canvas id="timelineChart"></canvas>
                        </div>
                    </div>
                </div>
                <div class="col-lg-4">
                    <div class="card p-3 shadow-sm h-100">
                        <h6 class="fw-bold mb-3"><i class="bi bi-pie-chart"></i> Distribución de Formatos</h6>
                        <div style="height: 320px;">
                            <canvas id="formatChart"></canvas>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- Tabla / Lista de Anuncios -->
        <div class="tab-pane fade" id="ads" role="tabpanel">
            <div class="card shadow-sm">
                <div class="table-responsive">
                    <table class="table table-hover align-middle mb-0">
                        <thead class="table-light">
                            <tr class="small text-secondary">
                                <th>Empresa</th>
                                <th>Estado</th>
                                <th>Formato</th>
                                <th>Texto / Copia</th>
                                <th>Fecha Inicio</th>
                                <th>Acción</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for ad in anuncios %}
                            <tr>
                                <td class="fw-bold text-dark">{{ ad.compania or 'N/A' }}</td>
                                <td>
                                    <span class="badge {% if ad.estado == 'Activo' %}badge-active{% else %}badge-inactive{% endif %}">
                                        {{ ad.estado or 'Desconocido' }}
                                    </span>
                                </td>
                                <td>
                                    {% if 'video' in (ad.formato|string|lower) %}
                                        <span class="text-danger small"><i class="bi bi-camera-video"></i> Video</span>
                                    {% else %}
                                        <span class="text-warning small"><i class="bi bi-image"></i> Imagen</span>
                                    {% endif %}
                                </td>
                                <td class="small text-muted" style="max-width: 350px;">
                                    {{ (ad.texto or ad.titulo or 'Sin descripción')[:140] }}{% if (ad.texto or ad.titulo or '')|length > 140 %}...{% endif %}
                                </td>
                                <td class="small">{{ ad.fecha_inicio or 'N/A' }}</td>
                                <td>
                                    {% if ad.link_individual %}
                                    <a href="{{ ad.link_individual }}" target="_blank" class="btn btn-xs btn-outline-primary py-0 px-2" style="font-size: 0.75rem;">
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
                                    <i class="bi bi-folder-x fs-1 d-block mb-2"></i>
                                    No hay registros disponibles para los filtros seleccionados
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>

</div>

<script>
    // Datos dinámicos para Chart.js
    const timelineData = {{ timeline_data|tojson }};
    const formatData = {{ format_data|tojson }};

    // Gráfico de Líneas / Tendencias
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

    // Gráfico de Dona (Formatos)
    if (document.getElementById('formatChart')) {
        new Chart(document.getElementById('formatChart'), {
            type: 'doughnut',
            data: {
                labels: ['Videos', 'Imágenes', 'Otros'],
                datasets: [{
                    data: [formatData.videos, formatData.imagenes, formatData.otros],
                    backgroundColor: ['#ef4444', '#f59e0b', '#94a3b8']
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { position: 'bottom' } }
            }
        });
    }
</script>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
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

    if conn:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Obtener listado de empresas únicas para el filtro
                cur.execute("SELECT DISTINCT compania FROM anuncios WHERE compania IS NOT NULL ORDER BY compania ASC")
                lista_companias = [r['compania'] for r in cur.fetchall()]

                # Construir consulta con filtros
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
        except Exception as e:
            print(f"Error consultando base de datos: {e}")
            anuncios = []
        finally:
            conn.close()

    # Cálculos y KPIs
    total_anuncios = len(anuncios)
    companias_set = {a['compania'] for a in anuncios if a.get('compania')}
    total_companias = len(companias_set)
    total_videos = sum(1 for a in anuncios if 'video' in str(a.get('formato', '')).lower())
    total_fotos = sum(1 for a in anuncios if 'imagen' in str(a.get('formato', '')).lower() or 'foto' in str(a.get('formato', '')).lower())
    total_otros = total_anuncios - (total_videos + total_fotos)

    # Preparar datos de gráfico de tendencias
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
    format_data = {"videos": total_videos, "imagenes": total_fotos, "otros": max(0, total_otros)}

    return render_template_string(
        HTML_TEMPLATE,
        anuncios=anuncios,
        lista_companias=lista_companias,
        total_anuncios=total_anuncios,
        total_companias=total_companias,
        total_videos=total_videos,
        total_fotos=total_fotos,
        timeline_data=timeline_data,
        format_data=format_data,
        msg=msg
    )

@app.route('/lanzar_scraper', methods=['POST'])
def lanzar_scraper():
    """Dispara el flujo de trabajo de Playwright en GitHub Actions."""
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
        "inputs": {
            "dias": str(dias)
        }
    }

    try:
        response = requests.post(url_api, json=payload, headers=headers, timeout=10)
        if response.status_code == 204:
            return redirect(url_for('index', msg=f"🚀 Scraping iniciado en GitHub Actions ({dias} días). Los datos se actualizarán automáticamente."))
        else:
            return redirect(url_for('index', msg=f"⚠️ GitHub respondió con código {response.status_code}: {response.text}"))
    except Exception as e:
        return redirect(url_for('index', msg=f"❌ Error al conectar con GitHub Actions: {e}"))

@app.route('/descargar_excel')
def descargar_excel():
    """Exporta los anuncios filtrados directamente a un archivo Excel."""
    conn = get_db_connection()
    if not conn:
        return redirect(url_for('index', msg="Error: No hay conexión con la base de datos."))

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

        nombre_archivo = f"Reporte_Meta_Ads_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        return send_file(
            output,
            download_name=nombre_archivo,
            as_attachment=True,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
    except Exception as e:
        return redirect(url_for('index', msg=f"Error generando reporte Excel: {e}"))
    finally:
        conn.close()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=True)
