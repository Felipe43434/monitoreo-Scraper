import os
import sys
import json
import subprocess
import urllib.parse
import re
from datetime import datetime, timedelta
import pandas as pd
import psycopg2
from flask import Flask, render_template_string, request, Response, redirect, url_for, jsonify
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.environ.get("DATABASE_URL")
JSON_FILE = "anuncios.json"
URLS_FILE = "urls.txt"
PROGRESO_FILE = "progreso.json"

app = Flask(__name__)
app.secret_key = "meta_ads_secret_key_123"

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="es" data-bs-theme="light">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Meta Ads Intelligence Dashboard</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css">
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body { 
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; 
            transition: background-color 0.2s ease, color 0.2s ease; 
        }
        
        .navbar-custom { border-bottom: 1px solid var(--bs-border-color); }
        .card { border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.04); }
        
        .kpi-card { transition: transform 0.15s ease-in-out; border-radius: 12px; }
        .kpi-card:hover { transform: translateY(-2px); }
        .kpi-icon { width: 44px; height: 44px; border-radius: 10px; display: flex; align-items: center; justify-content: center; font-size: 1.3rem; }
        
        .nav-pills .nav-link { color: var(--bs-secondary-color); font-weight: 500; border-radius: 8px; padding: 8px 18px; margin-right: 4px; }
        .nav-pills .nav-link.active { background-color: #0d6efd; color: #ffffff; }
        
        /* Estilos limpios y alineados de la tabla detallada */
        .tabla-detalle {
            width: 100%;
            margin-bottom: 0;
        }
        .tabla-detalle thead th {
            font-size: 0.78rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            background-color: var(--bs-tertiary-bg);
            border-bottom: 2px solid var(--bs-border-color);
            padding: 12px 10px;
            vertical-align: middle;
            white-space: nowrap;
        }
        .tabla-detalle tbody td {
            vertical-align: middle;
            font-size: 0.88rem;
            padding: 10px 10px;
            border-bottom: 1px solid var(--bs-border-color);
        }
        
        .table-resumen th { text-align: center; font-size: 0.82rem; }
        .table-resumen td { text-align: center; font-weight: 500; }
        .table-resumen td:first-child { text-align: left; font-weight: 600; }
        
        .dropdown-menu-custom { max-height: 280px; overflow-y: auto; min-width: 260px; }
        .url-row { background: var(--bs-tertiary-bg); border: 1px solid var(--bs-border-color); border-radius: 8px; padding: 8px 12px; margin-bottom: 6px; }
        
        /* Badges */
        .badge-video { background-color: #fee2e2; color: #dc2626; }
        .badge-foto { background-color: #fef3c7; color: #d97706; }
        .badge-activo { background-color: #dcfce7; color: #15803d; }
        .badge-inactivo { background-color: #f1f5f9; color: #64748b; }
        
        /* AJUSTES MODO OSCURO (DARK MODE) */
        [data-bs-theme="dark"] {
            --bs-body-bg: #0f172a;
            --bs-body-color: #f8fafc;
            --bs-secondary-color: #94a3b8;
            --bs-border-color: #334155;
            --bs-tertiary-bg: #1e293b;
        }
        
        [data-bs-theme="dark"] .card {
            background-color: #1e293b !important;
            border-color: #334155 !important;
        }
        
        [data-bs-theme="dark"] .text-secondary {
            color: #cbd5e1 !important;
        }
        
        [data-bs-theme="dark"] .btn-outline-secondary {
            background-color: #0f172a;
            border-color: #475569;
            color: #f1f5f9;
        }
        [data-bs-theme="dark"] .btn-outline-secondary:hover,
        [data-bs-theme="dark"] .btn-outline-secondary:focus,
        [data-bs-theme="dark"] .btn-outline-secondary:active,
        [data-bs-theme="dark"] .btn-outline-secondary.show {
            background-color: #334155 !important;
            border-color: #64748b !important;
            color: #ffffff !important;
        }

        [data-bs-theme="dark"] #dropComp {
            background-color: #0f172a !important;
            border-color: #475569 !important;
            color: #f8fafc !important;
        }
        [data-bs-theme="dark"] #dropComp:hover,
        [data-bs-theme="dark"] #dropComp:focus,
        [data-bs-theme="dark"] #dropComp[aria-expanded="true"] {
            background-color: #1e293b !important;
            border-color: #38bdf8 !important;
            color: #38bdf8 !important;
        }

        [data-bs-theme="dark"] .dropdown-menu {
            background-color: #1e293b !important;
            border-color: #475569 !important;
            color: #f8fafc !important;
        }
        [data-bs-theme="dark"] .dropdown-menu .border-bottom {
            border-bottom-color: #334155 !important;
        }
        [data-bs-theme="dark"] .dropdown-menu .form-check-label {
            color: #f1f5f9 !important;
        }
        [data-bs-theme="dark"] .dropdown-menu .form-check-input {
            background-color: #0f172a;
            border-color: #64748b;
        }
        [data-bs-theme="dark"] .dropdown-menu .form-check-input:checked {
            background-color: #0d6efd;
            border-color: #0d6efd;
        }
        [data-bs-theme="dark"] .dropdown-menu .btn-link {
            color: #38bdf8 !important;
        }
        [data-bs-theme="dark"] .dropdown-menu .btn-link.text-danger {
            color: #f87171 !important;
        }
        
        [data-bs-theme="dark"] .form-control, 
        [data-bs-theme="dark"] .form-select {
            background-color: #0f172a;
            border-color: #475569;
            color: #f8fafc;
        }
        [data-bs-theme="dark"] .form-control:focus, 
        [data-bs-theme="dark"] .form-select:focus {
            background-color: #0f172a;
            border-color: #38bdf8;
            color: #f8fafc;
        }
        
        [data-bs-theme="dark"] .table {
            color: #f1f5f9 !important;
        }
        [data-bs-theme="dark"] .table thead th {
            background-color: #0f172a !important;
            color: #e2e8f0 !important;
            border-bottom-color: #475569 !important;
        }
        [data-bs-theme="dark"] .table-hover tbody tr:hover {
            background-color: rgba(255, 255, 255, 0.05) !important;
        }
        
        [data-bs-theme="dark"] .badge-video { background-color: #7f1d1d; color: #fecaca; }
        [data-bs-theme="dark"] .badge-foto { background-color: #78350f; color: #fde68a; }
        [data-bs-theme="dark"] .badge-activo { background-color: #064e3b; color: #86efac; }
        [data-bs-theme="dark"] .badge-inactivo { background-color: #334155; color: #cbd5e1; }
        
        #contenedorProgreso { display: none; }
    </style>
</head>
<body class="pb-5">
    <nav class="navbar navbar-expand-lg navbar-custom sticky-top py-2 mb-4 bg-body">
        <div class="container-fluid px-4">
            <div class="d-flex align-items-center gap-2">
                <div class="bg-primary text-white p-2 rounded-3 d-flex align-items-center justify-content-center" style="width: 38px; height: 38px;">
                    <i class="bi bi-graph-up-arrow"></i>
                </div>
                <div>
                    <h5 class="mb-0 fw-bold">Meta Ads Intelligence</h5>
                    <small class="text-secondary" style="font-size: 0.78rem;">Supervisión y Análisis Competitivo</small>
                </div>
            </div>
            
            <div class="d-flex gap-2 align-items-center">
                <button type="button" class="btn btn-sm btn-outline-primary fw-medium" data-bs-toggle="modal" data-bs-target="#modalUrls">
                    <i class="bi bi-building-fill-gear me-1"></i> Empresas ({{ lista_urls|length }})
                </button>
                <button type="button" class="btn btn-sm btn-outline-danger fw-medium" data-bs-toggle="modal" data-bs-target="#modalExcluir">
                    <i class="bi bi-slash-circle me-1"></i> Bloquear
                </button>
                <a href="/descargar?{{ query_string_descarga }}" class="btn btn-sm btn-success fw-medium">
                    <i class="bi bi-download me-1"></i> CSV
                </a>

                <!-- Configuración / Tema -->
                <div class="dropdown">
                    <button class="btn btn-sm btn-outline-secondary dropdown-toggle d-flex align-items-center gap-1" type="button" id="dropdownConfig" data-bs-toggle="dropdown" aria-expanded="false" title="Configuración">
                        <i class="bi bi-gear-fill"></i>
                    </button>
                    <ul class="dropdown-menu dropdown-menu-end shadow-sm p-2" aria-labelledby="dropdownConfig" style="min-width: 190px;">
                        <li class="dropdown-header text-uppercase small fw-bold">Apariencia</li>
                        <li>
                            <button type="button" class="dropdown-item d-flex align-items-center justify-content-between rounded py-1 px-2" onclick="aplicarTema('light')">
                                <span><i class="bi bi-sun-fill text-warning me-2"></i>Modo Claro</span>
                                <i class="bi bi-check2 check-theme text-primary d-none" id="checkLight"></i>
                            </button>
                        </li>
                        <li>
                            <button type="button" class="dropdown-item d-flex align-items-center justify-content-between rounded py-1 px-2" onclick="aplicarTema('dark')">
                                <span><i class="bi bi-moon-stars-fill text-info me-2"></i>Modo Oscuro</span>
                                <i class="bi bi-check2 check-theme text-primary d-none" id="checkDark"></i>
                            </button>
                        </li>
                    </ul>
                </div>
            </div>
        </div>
    </nav>

    <div class="container-fluid px-4">
        <!-- BARRA DE PROGRESO EN VIVO -->
        <div id="contenedorProgreso" class="card p-3 mb-4 border-primary shadow-sm bg-body">
            <div class="d-flex justify-content-between align-items-center mb-2">
                <div class="d-flex align-items-center gap-2">
                    <div class="spinner-border spinner-border-sm text-primary" role="status"></div>
                    <span class="fw-bold small" id="textoProgresoEmpresa">Rastreando empresa...</span>
                </div>
                <div class="badge bg-primary px-3 py-1 fw-bold" id="badgeProgresoPorcentaje">0%</div>
            </div>
            <div class="progress" style="height: 12px; border-radius: 6px;">
                <div id="barraProgresoFill" class="progress-bar progress-bar-striped progress-bar-animated bg-primary" role="progressbar" style="width: 0%;"></div>
            </div>
            <div class="d-flex justify-content-between align-items-center mt-2">
                <small class="text-secondary" id="textoProgresoDetalle">Procesando 0 de 0 empresas</small>
                <small class="text-secondary">Por favor no cierres la ventana</small>
            </div>
        </div>

        {% if mensaje_bot %}
        <div class="alert alert-primary alert-dismissible fade show d-flex align-items-center shadow-sm" role="alert">
            <i class="bi bi-info-circle-fill me-2 fs-5"></i>
            <div>{{ mensaje_bot }}</div>
            <button type="button" class="btn-close ms-auto" data-bs-dismiss="alert" aria-label="Close"></button>
        </div>
        {% endif %}

        <!-- KPIs Cards -->
        <div class="row g-3 mb-4">
            <div class="col-xl-3 col-sm-6">
                <div class="card p-3 kpi-card bg-body">
                    <div class="d-flex align-items-center justify-content-between">
                        <div>
                            <div class="text-secondary small fw-semibold text-uppercase">Total Filtrados</div>
                            <h3 class="fw-bold mb-0 mt-1">{{ total_anuncios }}</h3>
                        </div>
                        <div class="kpi-icon bg-primary bg-opacity-10 text-primary">
                            <i class="bi bi-collection-play"></i>
                        </div>
                    </div>
                </div>
            </div>
            <div class="col-xl-3 col-sm-6">
                <div class="card p-3 kpi-card bg-body">
                    <div class="d-flex align-items-center justify-content-between">
                        <div>
                            <div class="text-secondary small fw-semibold text-uppercase">Compañías</div>
                            <h3 class="fw-bold mb-0 mt-1 text-success">{{ total_marcas }}</h3>
                        </div>
                        <div class="kpi-icon bg-success bg-opacity-10 text-success">
                            <i class="bi bi-buildings"></i>
                        </div>
                    </div>
                </div>
            </div>
            <div class="col-xl-3 col-sm-6">
                <div class="card p-3 kpi-card bg-body">
                    <div class="d-flex align-items-center justify-content-between">
                        <div>
                            <div class="text-secondary small fw-semibold text-uppercase">Videos</div>
                            <h3 class="fw-bold mb-0 mt-1 text-danger">{{ total_videos }}</h3>
                        </div>
                        <div class="kpi-icon bg-danger bg-opacity-10 text-danger">
                            <i class="bi bi-camera-video"></i>
                        </div>
                    </div>
                </div>
            </div>
            <div class="col-xl-3 col-sm-6">
                <div class="card p-3 kpi-card bg-body">
                    <div class="d-flex align-items-center justify-content-between">
                        <div>
                            <div class="text-secondary small fw-semibold text-uppercase">Fotos / Imágenes</div>
                            <h3 class="fw-bold mb-0 mt-1 text-warning">{{ total_fotos }}</h3>
                        </div>
                        <div class="kpi-icon bg-warning bg-opacity-10 text-warning">
                            <i class="bi bi-image"></i>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- Panel de Filtros & Sincronización -->
        <div class="card p-3 mb-4 bg-body">
            <div class="row g-3 align-items-center">
                <div class="col-12">
                    <form method="GET" action="/" class="row g-2 align-items-end" id="formFiltros">
                        <input type="hidden" name="agrupacion" id="inputAgrupacion" value="{{ agrup_sel }}">
                        
                        <div class="col-lg-3 col-md-4">
                            <label class="form-label small fw-semibold text-secondary mb-1"><i class="bi bi-search me-1"></i>Buscar Término / Link / ID:</label>
                            <input type="text" name="busqueda_texto" class="form-control form-control-sm" placeholder="Ej: Fina Partner, crédito..." value="{{ busq_sel }}">
                        </div>

                        <!-- Dropdown de Compañías -->
                        <div class="col-lg-2 col-md-4">
                            <label class="form-label small fw-semibold text-secondary mb-1"><i class="bi bi-building me-1"></i>Compañías:</label>
                            <div class="dropdown">
                                <button class="btn btn-outline-secondary btn-sm dropdown-toggle w-100 text-start text-truncate" type="button" id="dropComp" data-bs-toggle="dropdown" aria-expanded="false">
                                    {% if comp_sel|length == 0 or 'Todas' in comp_sel %}
                                        Todas ({{ lista_companias|length }})
                                    {% elif comp_sel|length == 1 %}
                                        {{ comp_sel[0] }}
                                    {% else %}
                                        {{ comp_sel|length }} seleccionadas
                                    {% endif %}
                                </button>
                                <div class="dropdown-menu dropdown-menu-custom p-2 shadow" aria-labelledby="dropComp">
                                    <div class="d-flex justify-content-between mb-2 pb-1 border-bottom">
                                        <button type="button" class="btn btn-link btn-sm p-0 text-decoration-none fw-semibold" onclick="seleccionarTodas(true)">Todas</button>
                                        <button type="button" class="btn btn-link btn-sm p-0 text-decoration-none text-danger fw-semibold" onclick="seleccionarTodas(false)">Ninguna</button>
                                    </div>
                                    {% for comp in lista_companias %}
                                    <div class="form-check py-1">
                                        <input class="form-check-input check-comp" type="checkbox" name="compania" value="{{ comp }}" id="chk_{{ loop.index }}" {% if comp in comp_sel or 'Todas' in comp_sel %}checked{% endif %}>
                                        <label class="form-check-label small text-truncate d-block" for="chk_{{ loop.index }}">
                                            {{ comp }}
                                        </label>
                                    </div>
                                    {% endfor %}
                                </div>
                            </div>
                        </div>

                        <div class="col-lg-1 col-md-4">
                            <label class="form-label small fw-semibold text-secondary mb-1"><i class="bi bi-activity me-1"></i>Estado:</label>
                            <select name="estado" class="form-select form-select-sm" onchange="this.form.submit()">
                                <option value="Todos" {% if est_sel == 'Todos' %}selected{% endif %}>Todos</option>
                                <option value="Activo" {% if est_sel == 'Activo' %}selected{% endif %}>🟢 Activo</option>
                                <option value="Inactivo" {% if est_sel == 'Inactivo' %}selected{% endif %}>⚪ Inactivo</option>
                            </select>
                        </div>

                        <div class="col-lg-2 col-md-4">
                            <label class="form-label small fw-semibold text-secondary mb-1"><i class="bi bi-calendar3 me-1"></i>Periodo:</label>
                            <select name="rango_dias" class="form-select form-select-sm" onchange="this.form.submit()">
                                <option value="7" {% if rango_sel == '7' %}selected{% endif %}>Últimos 7 días</option>
                                <option value="14" {% if rango_sel == '14' %}selected{% endif %}>Últimos 14 días</option>
                                <option value="30" {% if rango_sel == '30' %}selected{% endif %}>Últimos 30 días</option>
                                <option value="60" {% if rango_sel == '60' %}selected{% endif %}>Últimos 60 días</option>
                                <option value="90" {% if rango_sel == '90' %}selected{% endif %}>Últimos 90 días</option>
                                <option value="todo" {% if rango_sel == 'todo' %}selected{% endif %}>Todo el historial</option>
                            </select>
                        </div>

                        <div class="col-lg-1 col-md-4">
                            <label class="form-label small fw-semibold text-secondary mb-1"><i class="bi bi-layers me-1"></i>Formato:</label>
                            <select name="formato" class="form-select form-select-sm" onchange="this.form.submit()">
                                <option value="Todos" {% if form_sel == 'Todos' %}selected{% endif %}>Todos</option>
                                <option value="Foto" {% if form_sel == 'Foto' %}selected{% endif %}>🖼️ Foto</option>
                                <option value="Video" {% if form_sel == 'Video' %}selected{% endif %}>🎬 Video</option>
                            </select>
                        </div>

                        <div class="col-lg-1 col-md-3">
                            <label class="form-label small fw-semibold text-secondary mb-1"><i class="bi bi-phone me-1"></i>Plataforma:</label>
                            <select name="plataforma" class="form-select form-select-sm" onchange="this.form.submit()">
                                <option value="Todas" {% if plat_sel == 'Todas' %}selected{% endif %}>Todas</option>
                                <option value="Facebook" {% if plat_sel == 'Facebook' %}selected{% endif %}>FB</option>
                                <option value="Instagram" {% if plat_sel == 'Instagram' %}selected{% endif %}>IG</option>
                            </select>
                        </div>

                        <div class="col-lg-1 col-md-3">
                            <label class="form-label small fw-semibold text-secondary mb-1"><i class="bi bi-stopwatch me-1"></i>Duración:</label>
                            <select name="duracion" class="form-select form-select-sm" onchange="this.form.submit()">
                                <option value="Todas" {% if dur_sel == 'Todas' %}selected{% endif %}>Todas</option>
                                <option value="corto" {% if dur_sel == 'corto' %}selected{% endif %}>&lt; 15s</option>
                                <option value="medio" {% if dur_sel == 'medio' %}selected{% endif %}>15s-30s</option>
                                <option value="largo" {% if dur_sel == 'largo' %}selected{% endif %}>30s-80s</option>
                                <option value="extra_largo" {% if dur_sel == 'extra_largo' %}selected{% endif %}>&gt; 80s</option>
                            </select>
                        </div>

                        <div class="col-lg-1 col-md-3 d-flex gap-1">
                            <button type="submit" class="btn btn-sm btn-primary w-100"><i class="bi bi-funnel"></i></button>
                            <a href="/" class="btn btn-sm btn-outline-secondary" title="Limpiar"><i class="bi bi-arrow-counterclockwise"></i></a>
                        </div>
                    </form>
                </div>

                <div class="col-12 pt-2 border-top">
                    <div class="d-flex flex-wrap align-items-center justify-content-between gap-2">
                        <div class="d-flex align-items-center text-secondary small">
                            <i class="bi bi-arrow-repeat me-2 fs-6 text-primary"></i>
                            <span>Rastrear (Activos e Inactivos) en Meta Ads Library:</span>
                        </div>
                        <form action="/lanzar_scraper" method="POST" class="d-flex gap-2 align-items-center m-0">
                            <select name="dias_scraping" class="form-select form-select-sm" style="width: auto;">
                                <option value="7">Últimos 7 días</option>
                                <option value="14">Últimos 14 días</option>
                                <option value="30" selected>Últimos 30 días</option>
                                <option value="60">Últimos 60 días</option>
                                <option value="90">Últimos 90 días</option>
                                <option value="todo">Todo el historial</option>
                            </select>
                            <button type="submit" class="btn btn-sm btn-dark text-nowrap">
                                <i class="bi bi-arrow-repeat me-1"></i> Sincronizar Datos
                            </button>
                        </form>
                    </div>
                </div>
            </div>
        </div>

        <!-- Pestañas -->
        <ul class="nav nav-pills mb-3" id="mainTabs" role="tablist">
            <li class="nav-item" role="presentation">
                <button class="nav-link active" id="overview-tab" data-bs-toggle="pill" data-bs-target="#tab-overview" type="button" role="tab">
                    <i class="bi bi-graph-up me-1"></i> Vista General & Tendencias
                </button>
            </li>
            <li class="nav-item" role="presentation">
                <button class="nav-link" id="details-tab" data-bs-toggle="pill" data-bs-target="#tab-details" type="button" role="tab">
                    <i class="bi bi-list-check me-1"></i> Detalle de Anuncios ({{ total_anuncios }})
                </button>
            </li>
            <li class="nav-item" role="presentation">
                <button class="nav-link position-relative" id="news-tab" data-bs-toggle="pill" data-bs-target="#tab-news" type="button" role="tab">
                    <i class="bi bi-bell me-1"></i> Nuevos Anuncios
                    {% if total_anuncios_nuevos > 0 %}
                    <span class="badge rounded-pill bg-danger ms-1">{{ total_anuncios_nuevos }}</span>
                    {% endif %}
                </button>
            </li>
        </ul>

        <div class="tab-content" id="mainTabsContent">
            <!-- TAB 1: VISTA GENERAL -->
            <div class="tab-pane fade show active" id="tab-overview" role="tabpanel">
                <div class="card p-4 mb-4 bg-body">
                    <div class="d-flex flex-wrap justify-content-between align-items-center mb-3">
                        <div>
                            <h6 class="fw-bold mb-0"><i class="bi bi-graph-up me-2 text-primary"></i>Publicación de Anuncios por Empresa</h6>
                            <small class="text-secondary">Líneas con marcadores únicos por compañía (ordenadas A-Z)</small>
                        </div>
                        <div class="d-flex align-items-center gap-2 mt-2 mt-sm-0">
                            <span class="small fw-semibold text-secondary">Agrupación:</span>
                            <select class="form-select form-select-sm" style="width: auto;" onchange="cambiarAgrupacion(this.value)">
                                <option value="semana" {% if agrup_sel == 'semana' %}selected{% endif %}>Semana</option>
                                <option value="mes" {% if agrup_sel == 'mes' %}selected{% endif %}>Mes</option>
                                <option value="ano" {% if agrup_sel == 'ano' %}selected{% endif %}>Año</option>
                            </select>
                        </div>
                    </div>
                    {% if chart_labels and chart_datasets %}
                        <div style="position: relative; height: 360px; width: 100%;">
                            <canvas id="graficoVideos"></canvas>
                        </div>
                    {% else %}
                        <div class="alert alert-light border text-center my-4 py-4 text-secondary">
                            <i class="bi bi-folder-x fs-2 d-block mb-2"></i>
                            No hay registros disponibles para los filtros seleccionados.
                        </div>
                    {% endif %}
                </div>

                <div class="card p-4 bg-body">
                    <div class="mb-3">
                        <h6 class="fw-bold mb-0"><i class="bi bi-calendar-range me-2 text-primary"></i>Resumen Comparativo de Anuncios por Semana</h6>
                        <small class="text-secondary">Consolidado ordenado alfabéticamente por compañía</small>
                    </div>
                    <div class="table-responsive">
                        {{ tabla_semanal_html | safe }}
                    </div>
                </div>
            </div>

            <!-- TAB 2: DETALLE DE ANUNCIOS (PERFECTAMENTE ALINEADO) -->
            <div class="tab-pane fade" id="tab-details" role="tabpanel">
                <div class="card p-4 bg-body">
                    <div class="d-flex justify-content-between align-items-center mb-3">
                        <h6 class="fw-bold mb-0"><i class="bi bi-table me-2 text-primary"></i>Listado Individual de Anuncios</h6>
                        <span class="badge bg-secondary">{{ total_anuncios }} resultados</span>
                    </div>
                    <div class="table-responsive">
                        {{ tabla_detalle_html | safe }}
                    </div>
                </div>
            </div>

            <!-- TAB 3: ANUNCIOS NUEVOS -->
            <div class="tab-pane fade" id="tab-news" role="tabpanel">
                <div class="card p-4 border-warning bg-body">
                    <div class="d-flex flex-wrap justify-content-between align-items-center mb-3">
                        <div>
                            <h6 class="fw-bold mb-0"><i class="bi bi-bell-fill me-2 text-warning"></i>Anuncios Detectados Recientemente</h6>
                            <small class="text-secondary">Última revisión guardada: <strong>{{ fecha_ultima_revision_str }}</strong></small>
                        </div>
                        <form action="/marcar_revisado" method="POST" class="m-0 mt-2 mt-sm-0">
                            <button type="submit" class="btn btn-sm btn-outline-dark fw-medium">
                                <i class="bi bi-check2-all me-1"></i> Marcar todo como visto
                            </button>
                        </form>
                    </div>

                    {% if total_anuncios_nuevos > 0 %}
                        <div class="table-responsive border rounded bg-body">
                            {{ tabla_nuevos_html | safe }}
                        </div>
                    {% else %}
                        <div class="alert alert-light border text-center py-4 my-2 text-secondary">
                            <i class="bi bi-check-circle fs-3 text-success d-block mb-2"></i>
                            ¡Estás al día! No se han detectado nuevos anuncios desde tu última revisión.
                        </div>
                    {% endif %}
                </div>
            </div>
        </div>
    </div>

    <!-- MODAL GESTIONAR EMPRESAS -->
    <div class="modal fade" id="modalUrls" tabindex="-1" aria-hidden="true">
        <div class="modal-dialog modal-lg modal-dialog-scrollable">
            <div class="modal-content border-0 shadow bg-body">
                <div class="modal-header bg-primary text-white">
                    <h6 class="modal-title fw-bold"><i class="bi bi-gear-fill me-2"></i>Gestión de Empresas y URLs</h6>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body">
                    <form action="/agregar_url" method="POST" class="p-3 rounded-3 border mb-4 bg-body-tertiary">
                        <div class="fw-bold small mb-2">➕ Añadir Nueva Empresa:</div>
                        <div class="row g-2">
                            <div class="col-md-4">
                                <input type="text" name="nombre_empresa" class="form-control form-control-sm" placeholder="Nombre (Ej: Fina Partner)" required>
                            </div>
                            <div class="col-md-8">
                                <div class="input-group input-group-sm">
                                    <input type="url" name="nueva_url" class="form-control" placeholder="https://www.facebook.com/ads/library/?..." required>
                                    <button type="submit" class="btn btn-success fw-medium">Guardar</button>
                                </div>
                            </div>
                        </div>
                    </form>

                    <div class="fw-bold small mb-2">Empresas en Monitoreo ({{ lista_urls|length }}):</div>
                    <div style="max-height: 220px; overflow-y: auto;" class="mb-4">
                        {% if lista_urls %}
                            {% for u in lista_urls %}
                            <div class="d-flex justify-content-between align-items-center url-row">
                                <span class="small font-monospace text-truncate me-2" style="max-width: 85%;">{{ u }}</span>
                                <form action="/borrar_url" method="POST" class="m-0" onsubmit="return confirm('¿Quitar esta empresa?')">
                                    <input type="hidden" name="url_a_borrar" value="{{ u }}">
                                    <button type="submit" class="btn btn-sm btn-outline-danger py-0 px-2" title="Eliminar"><i class="bi bi-trash"></i></button>
                                </form>
                            </div>
                            {% endfor %}
                        {% else %}
                            <p class="text-secondary small">No hay URLs configuradas en <code>urls.txt</code>.</p>
                        {% endif %}
                    </div>

                    <hr>
                    <form action="/guardar_todas_urls" method="POST">
                        <label class="form-label fw-bold small">📝 Editor Directo de <code>urls.txt</code> (Formato: <code>Nombre | URL</code>):</label>
                        <textarea name="texto_urls" class="form-control form-control-sm font-monospace" rows="4">{{ contenido_urls_raw }}</textarea>
                        <div class="text-end mt-2">
                            <button type="submit" class="btn btn-sm btn-secondary">Guardar Todo el Archivo</button>
                        </div>
                    </form>
                </div>
            </div>
        </div>
    </div>

    <!-- MODAL BLOQUEAR COMPAÑÍAS -->
    <div class="modal fade" id="modalExcluir" tabindex="-1" aria-hidden="true">
        <div class="modal-dialog">
            <div class="modal-content border-0 shadow bg-body">
                <div class="modal-header bg-danger text-white">
                    <h6 class="modal-title fw-bold"><i class="bi bi-slash-circle me-2"></i>Bloquear y Eliminar Compañía</h6>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                </div>
                <form action="/eliminar_compania" method="POST">
                    <div class="modal-body">
                        <p class="text-secondary small">
                            La compañía seleccionada será <strong>eliminada permanentemente de la BD</strong> y agregada a la lista negra para evitar nuevos registros.
                        </p>
                        <div class="mb-3">
                            <label class="form-label fw-bold small">Seleccionar Compañía:</label>
                            <select name="compania_eliminar" class="form-select form-select-sm" required>
                                <option value="" disabled selected>-- Elige una compañía --</option>
                                {% for comp in lista_todas_companias %}
                                    <option value="{{ comp }}">{{ comp }}</option>
                                {% endfor %}
                            </select>
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-sm btn-secondary" data-bs-dismiss="modal">Cancelar</button>
                        <button type="submit" class="btn btn-sm btn-danger" onclick="return confirm('¿Confirmas que deseas eliminarla permanentemente?')">
                            Eliminar y Bloquear
                        </button>
                    </div>
                </form>
            </div>
        </div>
    </div>

    <script>
        const coloresLight = ['#0d6efd', '#dc3545', '#198754', '#fd7e14', '#6f42c1', '#0dcaf0', '#d63384', '#20c997', '#ffc107', '#6c757d'];
        const coloresDark = ['#38bdf8', '#fb7185', '#4ade80', '#fb923c', '#c084fc', '#22d3ee', '#f472b6', '#2dd4bf', '#fde047', '#94a3b8'];

        let miChart = null;

        function actualizarColoresGrafico(tema) {
            if (!miChart) return;
            const esDark = tema === 'dark';
            const paleta = esDark ? coloresDark : coloresLight;
            const colorTexto = esDark ? '#e2e8f0' : '#475569';
            const colorGrid = esDark ? '#334155' : '#e2e8f0';

            miChart.data.datasets.forEach((ds, i) => {
                const c = paleta[i % paleta.length];
                ds.borderColor = c;
                ds.backgroundColor = c;
            });

            miChart.options.scales.x.ticks.color = colorTexto;
            miChart.options.scales.y.ticks.color = colorTexto;
            miChart.options.scales.y.title.color = colorTexto;
            miChart.options.scales.y.grid.color = colorGrid;
            miChart.options.plugins.legend.labels.color = colorTexto;

            miChart.update();
        }

        function aplicarTema(tema) {
            document.documentElement.setAttribute('data-bs-theme', tema);
            
            const checkLight = document.getElementById('checkLight');
            const checkDark = document.getElementById('checkDark');
            
            if (tema === 'dark') {
                checkDark.classList.remove('d-none');
                checkLight.classList.add('d-none');
            } else {
                checkLight.classList.remove('d-none');
                checkDark.classList.add('d-none');
            }
            
            localStorage.setItem('meta_ads_theme', tema);
            actualizarColoresGrafico(tema);
        }

        const temaGuardado = localStorage.getItem('meta_ads_theme') || 'light';

        function cambiarAgrupacion(val) {
            document.getElementById('inputAgrupacion').value = val;
            document.getElementById('formFiltros').submit();
        }

        function seleccionarTodas(estado) {
            document.querySelectorAll('.check-comp').forEach(chk => {
                chk.checked = estado;
            });
        }

        {% if chart_labels and chart_datasets %}
        const ctx = document.getElementById('graficoVideos').getContext('2d');
        miChart = new Chart(ctx, {
            type: 'line',
            data: { 
                labels: {{ chart_labels | tojson }}, 
                datasets: {{ chart_datasets | tojson }} 
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: {
                    mode: 'index',
                    intersect: false
                },
                scales: {
                    x: { 
                        grid: { display: false },
                        ticks: { font: { size: 11 } }
                    },
                    y: { 
                        beginAtZero: true, 
                        ticks: { stepSize: 1, precision: 0, font: { size: 11 } }, 
                        title: { display: true, text: 'Cantidad de Anuncios' },
                        grid: { color: '#e2e8f0' }
                    }
                },
                plugins: {
                    legend: { 
                        position: 'top',
                        labels: { 
                            usePointStyle: true, 
                            boxWidth: 10,
                            padding: 15,
                            font: { size: 12, weight: '500' }
                        }
                    },
                    tooltip: {
                        usePointStyle: true
                    }
                }
            }
        });
        {% endif %}

        aplicarTema(temaGuardado);

        let intervalProgreso = null;
        let estabaActivo = false;

        function verificarProgreso() {
            fetch('/api/progreso')
                .then(res => res.json())
                .then(data => {
                    const contenedor = document.getElementById('contenedorProgreso');
                    if (data.activo) {
                        estabaActivo = true;
                        contenedor.style.display = 'block';
                        document.getElementById('barraProgresoFill').style.width = data.porcentaje + '%';
                        document.getElementById('badgeProgresoPorcentaje').innerText = data.porcentaje + '%';
                        document.getElementById('textoProgresoEmpresa').innerText = 'Analizando: ' + (data.empresa || 'Iniciando...');
                        document.getElementById('textoProgresoDetalle').innerText = 'Empresa ' + data.actual + ' de ' + data.total;
                    } else {
                        if (estabaActivo && data.finalizado) {
                            estabaActivo = false;
                            contenedor.style.display = 'none';
                            window.location.reload();
                        } else {
                            contenedor.style.display = 'none';
                        }
                    }
                })
                .catch(() => {});
        }

        intervalProgreso = setInterval(verificarProgreso, 1500);
        verificarProgreso();
    </script>
</body>
</html>
"""

MESES_ES = {
    'ene': 'Jan', 'feb': 'Feb', 'mar': 'Mar', 'abr': 'Apr', 'may': 'May', 'jun': 'Jun',
    'jul': 'Jul', 'ago': 'Aug', 'sep': 'Sep', 'oct': 'Oct', 'nov': 'Nov', 'dic': 'Dec'
}

def formatear_plataformas_badges(plat_str):
    if not plat_str or str(plat_str).lower() == 'nan':
        return '<span class="text-secondary small">-</span>'
    p_low = str(plat_str).lower()
    badges = []
    if 'facebook' in p_low:
        badges.append('<span class="badge bg-primary text-white me-1" title="Facebook"><i class="bi bi-facebook me-1"></i>FB</span>')
    if 'instagram' in p_low:
        badges.append('<span class="badge bg-danger text-white me-1" title="Instagram"><i class="bi bi-instagram me-1"></i>IG</span>')
    if 'messenger' in p_low:
        badges.append('<span class="badge bg-info text-white me-1" title="Messenger"><i class="bi bi-messenger me-1"></i>Mess</span>')
    if 'audience' in p_low:
        badges.append('<span class="badge bg-secondary text-white" title="Audience Network"><i class="bi bi-broadcast me-1"></i>AN</span>')
    if not badges:
        return f'<span class="badge bg-secondary text-white">{plat_str}</span>'
    return "".join(badges)

def generar_html_tabla_detalle(df_filtrado):
    if df_filtrado.empty:
        return "<div class='p-4 text-center text-secondary'><i class='bi bi-search fs-3 d-block mb-2'></i>No hay anuncios registrados con los filtros seleccionados.</div>"
    
    filas_html = []
    for _, r in df_filtrado.iterrows():
        comp = r.get('compania', '')
        fecha = r.get('fecha_subida', '-') or '-'
        estado_raw = str(r.get('estado', 'Activo')).strip().lower()
        if estado_raw == 'activo':
            estado_badge = '<span class="badge badge-activo px-2 py-1"><i class="bi bi-check-circle me-1"></i>Activo</span>'
        else:
            estado_badge = '<span class="badge badge-inactivo px-2 py-1"><i class="bi bi-dash-circle me-1"></i>Inactivo</span>'
        
        plat_badges = formatear_plataformas_badges(r.get('plataformas', ''))
        
        formato_raw = str(r.get('formato', 'Foto')).strip().lower()
        if 'video' in formato_raw:
            formato_badge = '<span class="badge badge-video px-2 py-1"><i class="bi bi-camera-video me-1"></i>Video</span>'
        else:
            formato_badge = '<span class="badge badge-foto px-2 py-1"><i class="bi bi-image me-1"></i>Foto</span>'
        
        dur = r.get('duracion_segundos', 0)
        dur_html = f'<span class="badge bg-body-secondary text-body border">{int(dur)}s</span>' if pd.notnull(dur) and int(dur) > 0 else '<span class="text-secondary">-</span>'
        
        copy_txt = str(r.get('titulo', '') or '')
        copy_escaped = copy_txt.replace('"', '&quot;')
        copy_html = f'<div class="text-truncate" style="max-width: 320px;" title="{copy_escaped}">{copy_txt}</div>'
        
        link = r.get('link_individual', '')
        if link and str(link).startswith('http'):
            accion_html = f'<a href="{link}" target="_blank" class="btn btn-sm btn-primary py-0 px-2 fw-medium text-nowrap"><i class="bi bi-box-arrow-up-right me-1"></i>Ver</a>'
        else:
            accion_html = '-'
        
        filas_html.append(f"""
            <tr>
                <td class="text-start fw-semibold text-truncate" style="max-width: 180px;">{comp}</td>
                <td class="text-center font-monospace small">{fecha}</td>
                <td class="text-center">{estado_badge}</td>
                <td class="text-start">{plat_badges}</td>
                <td class="text-center">{formato_badge}</td>
                <td class="text-center">{dur_html}</td>
                <td class="text-start">{copy_html}</td>
                <td class="text-center">{accion_html}</td>
            </tr>
        """)
    
    return f"""
    <table class="table table-hover align-middle tabla-detalle">
        <thead>
            <tr>
                <th class="text-start" style="width: 16%;">Compañía</th>
                <th class="text-center" style="width: 11%;">Fecha Subida</th>
                <th class="text-center" style="width: 10%;">Estado</th>
                <th class="text-start" style="width: 15%;">Plataformas</th>
                <th class="text-center" style="width: 9%;">Formato</th>
                <th class="text-center" style="width: 8%;">Duración</th>
                <th class="text-start" style="width: 23%;">Texto / Copy</th>
                <th class="text-center" style="width: 8%;">Acción</th>
            </tr>
        </thead>
        <tbody>
            {''.join(filas_html)}
        </tbody>
    </table>
    """

def normalizar_fechas_dataframe(df):
    if df.empty:
        return df

    def parse_fecha(val):
        if pd.isna(val):
            return pd.NaT
        val_str = str(val).strip().lower()
        for es, en in MESES_ES.items():
            val_str = re.sub(rf'\b{es}\b', en, val_str)
        try:
            return pd.to_datetime(val_str, errors='coerce', dayfirst=True)
        except Exception:
            return pd.NaT

    if 'fecha_subida' in df.columns:
        df['fecha_dt'] = df['fecha_subida'].apply(parse_fecha)
    else:
        df['fecha_dt'] = pd.NaT

    if 'fecha_registro' in df.columns:
        df['fecha_registro_dt'] = pd.to_datetime(df['fecha_registro'], errors='coerce')
        df['fecha_dt'] = df['fecha_dt'].fillna(df['fecha_registro_dt'])
    else:
        df['fecha_registro_dt'] = pd.NaT

    return df

def asegurar_tablas():
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS companias_excluidas (
                id SERIAL PRIMARY KEY,
                compania VARCHAR(255) UNIQUE NOT NULL,
                fecha_exclusion TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS estado_revision (
                id INT PRIMARY KEY,
                ultima_revision TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("""
            INSERT INTO estado_revision (id, ultima_revision)
            VALUES (1, CURRENT_TIMESTAMP - INTERVAL '7 days')
            ON CONFLICT (id) DO NOTHING;
        """)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"⚠️ Error al crear tablas de control: {e}")

def obtener_ultima_revision():
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cur = conn.cursor()
        cur.execute("SELECT ultima_revision FROM estado_revision WHERE id = 1;")
        fila = cur.fetchone()
        cur.close()
        conn.close()
        if fila and fila[0]:
            return fila[0]
    except Exception:
        pass
    return datetime.now() - timedelta(days=7)

def actualizar_ultima_revision():
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO estado_revision (id, ultima_revision) 
            VALUES (1, CURRENT_TIMESTAMP)
            ON CONFLICT (id) DO UPDATE SET ultima_revision = CURRENT_TIMESTAMP;
        """)
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"⚠️ Error al actualizar revisión: {e}")
        return False

def cargar_datos_base():
    asegurar_tablas()
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        query = """
            SELECT * FROM anuncios 
            WHERE compania NOT IN (
                SELECT compania FROM companias_excluidas
                UNION
                SELECT 'Empresa anunciante'
                UNION
                SELECT 'Competidor'
            )
            ORDER BY fecha_registro DESC;
        """
        df = pd.read_sql(query, conn)
        conn.close()
        return normalizar_fechas_dataframe(df)
    except Exception as e:
        print(f"⚠️ Error al consultar BD: {e}")
        return pd.DataFrame()

def obtener_lista_completa_companias():
    companias = set()
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT compania FROM anuncios WHERE compania IS NOT NULL;")
        for r in cur.fetchall():
            if r[0]:
                companias.add(r[0])
        cur.close()
        conn.close()
    except Exception:
        pass

    if os.path.exists(JSON_FILE):
        try:
            with open(JSON_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    for item in data:
                        comp = item.get("compania")
                        if comp:
                            companias.add(comp)
        except Exception:
            pass

    return sorted(list(companias), key=lambda s: s.lower())

def leer_urls():
    if not os.path.exists(URLS_FILE):
        return []
    with open(URLS_FILE, "r", encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.startswith("#")]

def escribir_urls(urls):
    with open(URLS_FILE, "w", encoding="utf-8") as f:
        for u in urls:
            f.write(f"{u.strip()}\n")

def calcular_tabla_resumen_semanal(df, comp_seleccionadas, rango_dias, busqueda_texto="", estado="Todos"):
    if df.empty:
        return "<p class='text-secondary p-3 mb-0'>No hay datos disponibles.</p>"
    
    df_data = df.copy()
    if comp_seleccionadas and 'Todas' not in comp_seleccionadas:
        df_data = df_data[df_data['compania'].isin(comp_seleccionadas)]

    if estado != 'Todos' and 'estado' in df_data.columns:
        df_data = df_data[df_data['estado'].astype(str).str.lower() == estado.lower()]

    if busqueda_texto:
        b = busqueda_texto.lower().strip()
        mascara = (
            df_data['compania'].astype(str).str.lower().str.contains(b, na=False) |
            df_data['link_individual'].astype(str).str.lower().str.contains(b, na=False) |
            df_data['titulo'].astype(str).str.lower().str.contains(b, na=False) |
            df_data['id_anuncio'].astype(str).str.lower().str.contains(b, na=False)
        )
        df_data = df_data[mascara]

    df_data = df_data.dropna(subset=['fecha_dt'])

    if rango_dias != 'todo' and not df_data.empty:
        try:
            dias_int = int(rango_dias)
            df_data = df_data[df_data['fecha_dt'] >= (df_data['fecha_dt'].max() - pd.Timedelta(days=dias_int))]
        except Exception:
            pass

    if df_data.empty:
        return "<p class='text-secondary p-3 mb-0'>No hay anuncios en este rango.</p>"

    df_data['clave_semana'] = df_data['fecha_dt'].dt.strftime('%Y-W%W')
    df_data['etiqueta_semana'] = df_data['fecha_dt'].dt.strftime('Sem %W (%b %Y)')

    pivote = df_data.groupby(['compania', 'clave_semana', 'etiqueta_semana']).size().unstack(level=['clave_semana', 'etiqueta_semana'], fill_value=0)
    pivote = pivote.sort_index(axis=1, level='clave_semana')
    pivote.columns = [c[1] for c in pivote.columns]
    pivote['Total Anuncios'] = pivote.sum(axis=1)
    
    pivote = pivote.sort_index(key=lambda s: s.str.lower()).reset_index().rename(columns={'compania': 'Compañía'})

    return pivote.to_html(classes="table table-bordered table-hover table-resumen align-middle mb-0", index=False)

def preparar_grafico_temporal(df, comp_seleccionadas, agrupacion="semana", rango_dias="30", busqueda_texto="", estado="Todos"):
    if df.empty:
        return [], []
    
    df_data = df.copy()
    if comp_seleccionadas and 'Todas' not in comp_seleccionadas:
        df_data = df_data[df_data['compania'].isin(comp_seleccionadas)]

    if estado != 'Todos' and 'estado' in df_data.columns:
        df_data = df_data[df_data['estado'].astype(str).str.lower() == estado.lower()]

    if busqueda_texto:
        b = busqueda_texto.lower().strip()
        mascara = (
            df_data['compania'].astype(str).str.lower().str.contains(b, na=False) |
            df_data['link_individual'].astype(str).str.lower().str.contains(b, na=False) |
            df_data['titulo'].astype(str).str.lower().str.contains(b, na=False) |
            df_data['id_anuncio'].astype(str).str.lower().str.contains(b, na=False)
        )
        df_data = df_data[mascara]
    
    df_data = df_data.dropna(subset=['fecha_dt'])

    if rango_dias != 'todo' and not df_data.empty:
        try:
            dias_int = int(rango_dias)
            df_data = df_data[df_data['fecha_dt'] >= (df_data['fecha_dt'].max() - pd.Timedelta(days=dias_int))]
        except Exception:
            pass

    if df_data.empty:
        return [], []

    if agrupacion == "mes":
        df_data['clave_tiempo'] = df_data['fecha_dt'].dt.strftime('%Y-%m')
        df_data['etiqueta_tiempo'] = df_data['fecha_dt'].dt.strftime('%b %Y')
    elif agrupacion == "ano":
        df_data['clave_tiempo'] = df_data['fecha_dt'].dt.strftime('%Y')
        df_data['etiqueta_tiempo'] = df_data['fecha_dt'].dt.strftime('%Y')
    else:
        df_data['clave_tiempo'] = df_data['fecha_dt'].dt.strftime('%Y-W%W')
        df_data['etiqueta_tiempo'] = df_data['fecha_dt'].dt.strftime('Sem %W (%Y)')

    pivote = df_data.groupby(['clave_tiempo', 'etiqueta_tiempo', 'compania']).size().unstack(fill_value=0).sort_index(level='clave_tiempo')
    pivote = pivote.reindex(sorted(pivote.columns, key=lambda s: s.lower()), axis=1)
    
    labels = [idx[1] for idx in pivote.index]
    
    colores = ['#0d6efd', '#dc3545', '#198754', '#fd7e14', '#6f42c1', '#0dcaf0', '#d63384', '#20c997', '#ffc107', '#6c757d']
    estilos_puntos = ['circle', 'triangle', 'rect', 'crossRot', 'star', 'rectRot', 'cross', 'dash']

    datasets = []
    for i, c in enumerate(pivote.columns):
        col = colores[i % len(colores)]
        simbolo = estilos_puntos[i % len(estilos_puntos)]
        datasets.append({
            'label': c,
            'data': [int(v) for v in pivote[c].tolist()],
            'borderColor': col,
            'backgroundColor': col,
            'pointStyle': simbolo,
            'pointRadius': 6,
            'pointHoverRadius': 9,
            'borderWidth': 2.5,
            'tension': 0.3,
            'fill': False
        })
    return labels, datasets

def aplicar_filtros(df, comp_seleccionadas, form, plat, dur, rango_dias, busqueda_texto="", estado="Todos"):
    df_f = df.copy()

    if estado != 'Todos' and 'estado' in df_f.columns:
        df_f = df_f[df_f['estado'].astype(str).str.lower() == estado.lower()]

    if busqueda_texto:
        b = busqueda_texto.lower().strip()
        mascara = (
            df_f['compania'].astype(str).str.lower().str.contains(b, na=False) |
            df_f['link_individual'].astype(str).str.lower().str.contains(b, na=False) |
            df_f['titulo'].astype(str).str.lower().str.contains(b, na=False) |
            df_f['id_anuncio'].astype(str).str.lower().str.contains(b, na=False)
        )
        df_f = df_f[mascara]

    if rango_dias != 'todo' and not df_f.empty and 'fecha_dt' in df_f.columns:
        f_max = df_f['fecha_dt'].max()
        if pd.notnull(f_max):
            try:
                df_f = df_f[df_f['fecha_dt'] >= (df_f['fecha_dt'].max() - pd.Timedelta(days=int(rango_dias)))]
            except Exception:
                pass

    if 'compania' in df_f.columns and comp_seleccionadas and 'Todas' not in comp_seleccionadas:
        df_f = df_f[df_f['compania'].isin(comp_seleccionadas)]

    if 'formato' in df_f.columns and form != 'Todos':
        if form == 'Video':
            df_f = df_f[df_f['formato'].astype(str).str.contains("Video", case=False, na=False)]
        elif form == 'Foto':
            df_f = df_f[~df_f['formato'].astype(str).str.contains("Video", case=False, na=False)]
    if 'plataformas' in df_f.columns and plat != 'Todas':
        df_f = df_f[df_f['plataformas'].astype(str).str.contains(plat, case=False, na=False)]
    if dur != 'Todas' and 'duracion_segundos' in df_f.columns:
        df_f['duracion_segundos'] = pd.to_numeric(df_f['duracion_segundos'], errors='coerce').fillna(0)
        if dur == 'corto':
            df_f = df_f[(df_f['duracion_segundos'] > 0) & (df_f['duracion_segundos'] < 15)]
        elif dur == 'medio':
            df_f = df_f[(df_f['duracion_segundos'] >= 15) & (df_f['duracion_segundos'] <= 30)]
        elif dur == 'largo':
            df_f = df_f[(df_f['duracion_segundos'] > 30) & (df_f['duracion_segundos'] <= 80)]
        elif dur == 'extra_largo':
            df_f = df_f[df_f['duracion_segundos'] > 80]

    if not df_f.empty and 'compania' in df_f.columns:
        df_f['comp_lower'] = df_f['compania'].astype(str).str.lower()
        if 'fecha_dt' in df_f.columns:
            df_f = df_f.sort_values(by=['comp_lower', 'fecha_dt'], ascending=[True, False]).drop(columns=['comp_lower'])
        else:
            df_f = df_f.sort_values(by=['comp_lower'], ascending=True).drop(columns=['comp_lower'])

    return df_f

# ==========================================
# RUTAS DE CONTROL Y PROGRESO
# ==========================================

@app.route('/api/progreso')
def api_progreso():
    if os.path.exists(PROGRESO_FILE):
        try:
            with open(PROGRESO_FILE, "r", encoding="utf-8") as f:
                datos = json.load(f)
                return jsonify(datos)
        except Exception:
            pass
    return jsonify({"activo": False, "porcentaje": 0, "finalizado": False})

@app.route('/marcar_revisado', methods=['POST'])
def marcar_revisado():
    actualizar_ultima_revision()
    return redirect(url_for('index', msg="Revisión actualizada. Todos los anuncios han quedado marcados como vistos."))

@app.route('/agregar_url', methods=['POST'])
def agregar_url():
    nombre = request.form.get("nombre_empresa", "").strip()
    nueva = request.form.get("nueva_url", "").strip()
    if nombre and nueva:
        urls = leer_urls()
        registro = f"{nombre} | {nueva}"
        if registro not in urls:
            urls.append(registro)
            escribir_urls(urls)
            return redirect(url_for('index', msg=f"Empresa '{nombre}' registrada con su enlace."))
        else:
            return redirect(url_for('index', msg="Este registro ya existe."))
    return redirect(url_for('index', msg="Completa tanto el nombre de la empresa como la URL."))

@app.route('/borrar_url', methods=['POST'])
def borrar_url():
    url_a_borrar = request.form.get("url_a_borrar", "").strip()
    if url_a_borrar:
        urls = leer_urls()
        urls_filtradas = [u for u in urls if u != url_a_borrar]
        escribir_urls(urls_filtradas)
        return redirect(url_for('index', msg="Registro eliminado de urls.txt"))
    return redirect(url_for('index'))

@app.route('/guardar_todas_urls', methods=['POST'])
def guardar_todas_urls():
    texto = request.form.get("texto_urls", "")
    lineas = [l.strip() for l in texto.splitlines() if l.strip()]
    escribir_urls(lineas)
    return redirect(url_for('index', msg=f"Lista actualizada con {len(lineas)} registros."))

@app.route('/eliminar_compania', methods=['POST'])
def eliminar_compania():
    comp_a_borrar = request.form.get("compania_eliminar")
    if not comp_a_borrar:
        return redirect(url_for('index', msg="No se seleccionó ninguna compañía."))

    asegurar_tablas()
    total_bd = 0
    total_json = 0

    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO companias_excluidas (compania) 
            VALUES (%s) 
            ON CONFLICT (compania) DO NOTHING;
        """, (comp_a_borrar,))
        cur.execute("DELETE FROM anuncios WHERE compania = %s;", (comp_a_borrar,))
        total_bd = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"⚠️ Error al eliminar en BD: {e}")

    if os.path.exists(JSON_FILE):
        try:
            with open(JSON_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                original_len = len(data)
                data_filtrada = [item for item in data if item.get("compania") != comp_a_borrar]
                total_json = original_len - len(data_filtrada)
                with open(JSON_FILE, "w", encoding="utf-8") as f:
                    json.dump(data_filtrada, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"⚠️ Error al eliminar en JSON: {e}")

    msg = f"'{comp_a_borrar}' eliminada ({total_bd} registros en BD / {total_json} en JSON) y añadida a la lista negra permanente."
    return redirect(url_for('index', msg=msg))

@app.route('/lanzar_scraper', methods=['POST'])
def lanzar_scraper():
    opcion = request.form.get("dias_scraping", "30")
    if os.path.exists("monitoreo_meta.py"):
        subprocess.Popen([sys.executable, "monitoreo_meta.py", str(opcion)])
    return redirect(url_for('index', msg=f"Sincronización iniciada en segundo plano (rango: {opcion} días - Activos e Inactivos)"))

@app.route('/')
def index():
    df = cargar_datos_base()
    lista_todas_companias = obtener_lista_completa_companias()
    lista_urls = leer_urls()
    contenido_urls_raw = "\n".join(lista_urls)
    
    fecha_revision_dt = obtener_ultima_revision()
    fecha_ultima_revision_str = fecha_revision_dt.strftime('%d/%m/%Y %I:%M %p')

    df_nuevos = pd.DataFrame()
    if not df.empty and 'fecha_registro_dt' in df.columns:
        df_nuevos = df[df['fecha_registro_dt'] > fecha_revision_dt].copy()

    total_anuncios_nuevos = len(df_nuevos)
    
    cols_nuevos = ['compania', 'fecha_subida', 'estado', 'formato', 'titulo', 'link_individual']
    df_nuevos_disp = df_nuevos[[c for c in cols_nuevos if c in df_nuevos.columns]].copy() if not df_nuevos.empty else pd.DataFrame(columns=cols_nuevos)
    df_nuevos_disp.rename(columns={
        'compania': 'Compañía', 'fecha_subida': 'Fecha Inicio', 'estado': 'Estado',
        'formato': 'Formato', 'titulo': 'Copy / Texto', 'link_individual': 'Acción'
    }, inplace=True)
    if 'Acción' in df_nuevos_disp.columns:
        df_nuevos_disp['Acción'] = df_nuevos_disp['Acción'].apply(
            lambda x: f'<a href="{x}" target="_blank" class="btn btn-sm btn-outline-primary py-0 px-2 fw-medium text-nowrap"><i class="bi bi-box-arrow-up-right me-1"></i>Ver</a>' if pd.notnull(x) and str(x).startswith("http") else ''
        )
    tabla_nuevos_html = df_nuevos_disp.to_html(classes="table table-sm table-hover align-middle mb-0", index=False, escape=False)

    if df.empty:
        df = pd.DataFrame(columns=[
            'compania', 'fecha_subida', 'estado', 'plataformas', 
            'formato', 'duracion_segundos', 'titulo', 'link_individual'
        ])

    lista_companias = sorted(list(df['compania'].dropna().unique()), key=lambda s: s.lower()) if 'compania' in df.columns else []

    comp_sel = request.args.getlist('compania')
    if not comp_sel:
        comp_sel = ['Todas']

    busq_sel = request.args.get('busqueda_texto', '').strip()
    est_sel = request.args.get('estado', 'Todos')
    form_sel = request.args.get('formato', 'Todos')
    plat_sel = request.args.get('plataforma', 'Todas')
    dur_sel = request.args.get('duracion', 'Todas')
    rango_sel = request.args.get('rango_dias', '30')
    agrup_sel = request.args.get('agrupacion', 'semana')
    if agrup_sel not in ['semana', 'mes', 'ano']:
        agrup_sel = 'semana'

    mensaje_bot = request.args.get('msg', '')

    chart_labels, chart_datasets = preparar_grafico_temporal(df, comp_sel, agrup_sel, rango_sel, busq_sel, est_sel)
    tabla_semanal_html = calcular_tabla_resumen_semanal(df, comp_sel, rango_sel, busq_sel, est_sel)
    df_filtrado = aplicar_filtros(df, comp_sel, form_sel, plat_sel, dur_sel, rango_sel, busq_sel, est_sel)

    total_anuncios = len(df_filtrado)
    total_marcas = df_filtrado['compania'].nunique() if 'compania' in df_filtrado.columns else 0
    total_videos = len(df_filtrado[df_filtrado['formato'].astype(str).str.contains("Video", case=False, na=False)]) if 'formato' in df_filtrado.columns else 0
    total_fotos = total_anuncios - total_videos

    # Generación de tabla con alineación y badges perfectamente estructurados
    tabla_detalle_html = generar_html_tabla_detalle(df_filtrado)

    params_descarga = []
    for c in comp_sel:
        params_descarga.append(f"compania={urllib.parse.quote(c)}")
    params_descarga.extend([
        f"busqueda_texto={urllib.parse.quote(busq_sel)}",
        f"estado={urllib.parse.quote(est_sel)}",
        f"formato={urllib.parse.quote(form_sel)}",
        f"plataforma={urllib.parse.quote(plat_sel)}",
        f"duracion={urllib.parse.quote(dur_sel)}",
        f"rango_dias={urllib.parse.quote(rango_sel)}"
    ])
    query_string_descarga = "&".join(params_descarga)

    return render_template_string(
        HTML_TEMPLATE,
        tabla_detalle_html=tabla_detalle_html,
        tabla_semanal_html=tabla_semanal_html,
        tabla_nuevos_html=tabla_nuevos_html,
        total_anuncios_nuevos=total_anuncios_nuevos,
        fecha_ultima_revision_str=fecha_ultima_revision_str,
        lista_companias=lista_companias,
        lista_todas_companias=lista_todas_companias,
        lista_urls=lista_urls,
        contenido_urls_raw=contenido_urls_raw,
        chart_labels=chart_labels,
        chart_datasets=chart_datasets,
        comp_sel=comp_sel,
        busq_sel=busq_sel,
        est_sel=est_sel,
        form_sel=form_sel,
        plat_sel=plat_sel,
        dur_sel=dur_sel,
        rango_sel=rango_sel,
        agrup_sel=agrup_sel,
        total_anuncios=total_anuncios,
        total_marcas=total_marcas,
        total_videos=total_videos,
        total_fotos=total_fotos,
        mensaje_bot=mensaje_bot,
        query_string_descarga=query_string_descarga
    )

@app.route('/descargar')
def descargar_csv():
    df = cargar_datos_base()
    comp_sel = request.args.getlist('compania')
    if not comp_sel:
        comp_sel = ['Todas']
    busq_sel = request.args.get('busqueda_texto', '').strip()
    est_sel = request.args.get('estado', 'Todos')
    form = request.args.get('formato', 'Todos')
    plat = request.args.get('plataforma', 'Todas')
    dur = request.args.get('duracion', 'Todas')
    rango = request.args.get('rango_dias', '30')
    df_filtrado = aplicar_filtros(df, comp_sel, form, plat, dur, rango, busq_sel, est_sel)

    csv_data = df_filtrado.to_csv(index=False, encoding='utf-8-sig')
    nombre_seguro = urllib.parse.quote(f"reporte_{rango}dias.csv")

    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{nombre_seguro}"}
    )

if __name__ == '__main__':
    asegurar_tablas()
    print("Iniciando dashboard en http://127.0.0.1:5000 ...")
    app.run(host='127.0.0.1', port=5000, debug=False, threaded=True)