"""Lectura de precios en textos: anuncios (panel) y páginas de precios de la competencia (bot de precios).

- precios_en_texto: montos ($, USD, Bs, €; al mes o al año; por usuario o empresa), descuentos y pruebas gratis.
- precios_de_bloque: precios de un bloque de texto de una página web, cada uno con el plan al que pertenece.
"""
import re

_MONTO = r'(\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)'
PATRON_PRECIO = re.compile(r'(?<![a-záéíóúñ])(US\$|USD|\$|Bs\.?(?:S\.)?|€|EUR)\s?' + _MONTO + r'(?!\s?%)|' + _MONTO +
                           r'\s?(US\$|USD|\$|d[oó]lares|Bs\.?(?:S\.)?|bol[ií]vares|€|euros?)(?![a-záéíóúñ])', re.I)
PATRON_DESCUENTO = re.compile(r'(\d{1,2})\s?%\s?(?:de\s)?(?:descuento|dcto|dto|off)\b|descuento\s(?:de(?:l)?\s)?(\d{1,2})\s?%', re.I)
PATRON_PRUEBA = re.compile(r'(?:prueba|demo|pru[eé]ba(?:lo|la)?)\s+gratis(?:\s+(?:por|durante)\s+(\d{1,3})\s+d[ií]as)?|(\d{1,3})\s+d[ií]as\s+gratis', re.I)
# Monto sin símbolo al final de una línea ("25,00"): solo cuenta en páginas que lo indican en precios.txt.
PATRON_MONTO_SOLO = re.compile(r'(?:^|\s)(\d{1,3}(?:\.\d{3})*,\d{2})$')
MONEDAS_PRECIO = {'us$': 'USD', 'usd': 'USD', '$': 'USD', 'dólares': 'USD', 'dolares': 'USD', '€': 'EUR', 'eur': 'EUR', 'euro': 'EUR', 'euros': 'EUR'}


def a_monto(texto):
    # "1.200,50", "1,200.50", "25,99" o "1.500": el último separador es decimal si le siguen 1 o 2 cifras.
    t = texto.replace(' ', '')
    decimal = re.search(r'[.,](\d{1,2})$', t)
    if decimal:
        return float(re.sub(r'[.,]', '', t[:decimal.start()]) + '.' + decimal.group(1))
    return float(re.sub(r'[.,]', '', t))


def formato_monto(moneda, monto):
    numero = f"{monto:,.0f}" if monto == int(monto) else f"{monto:,.2f}"
    return f"{moneda} {numero.replace(',', '#').replace('.', ',').replace('#', '.')}"


def _precio(monto, moneda, despues):
    despues = despues[:30].lower()
    periodo = ('al mes' if re.search(r'\b(mes|mensual(es)?)\b', despues) else 'cada 3 meses' if re.search(r'cada 3 meses|trimestr', despues)
               else 'al año' if re.search(r'\b(año|anual(es)?)\b', despues) else '')
    unidad = next((f"por {u}" for u in ('usuario', 'empleado', 'trabajador', 'licencia', 'empresa') if re.search(rf'\bpor {u}', despues)), '')
    return {'tipo': 'precio', 'monto': monto, 'moneda': moneda, 'periodo': periodo, 'unidad': unidad,
            'etiqueta': ' '.join(x for x in (formato_monto(moneda, monto), periodo, unidad) if x)}


def precios_en_texto(texto, limite=None):
    # limite: solo cuentan los datos que empiezan antes de esa posición (el resto del texto sirve de contexto).
    limite = len(texto) if limite is None else limite
    encontrados = []
    for m in PATRON_PRECIO.finditer(texto):
        if m.start() >= limite:
            break
        simbolo = (m.group(1) or m.group(4) or '').lower().replace(' ', '')
        moneda = 'Bs' if simbolo.startswith('b') else MONEDAS_PRECIO.get(simbolo, 'USD')
        try:
            monto = a_monto(m.group(2) or m.group(3))
        except ValueError:
            continue
        if 0 < monto < 10_000_000:
            encontrados.append(dict(_precio(monto, moneda, texto[m.end():]), pos=m.start()))
    for m in PATRON_DESCUENTO.finditer(texto):
        if m.start() < limite:
            encontrados.append({'tipo': 'descuento', 'etiqueta': f"{m.group(1) or m.group(2)} % de descuento", 'pos': m.start()})
    for m in PATRON_PRUEBA.finditer(texto):
        if m.start() < limite:
            dias = m.group(1) or m.group(2)
            encontrados.append({'tipo': 'prueba', 'etiqueta': f"Prueba gratis de {dias} días" if dias else 'Prueba gratis', 'pos': m.start()})
    for p in encontrados:
        # Fragmento alrededor del dato, cortado en palabras completas.
        inicio = max(0, p.pop('pos') - 40)
        palabras = texto[inicio:inicio + 110].split()
        if inicio > 0 and len(palabras) > 1 and not texto[inicio - 1].isspace():
            palabras = palabras[1:]
        if inicio + 110 < len(texto) and len(palabras) > 1:
            palabras = palabras[:-1]
        p['fragmento'] = ('…' if inicio > 0 else '') + ' '.join(palabras) + ('…' if inicio + 110 < len(texto) else '')
    return encontrados


def _es_titulo(linea):
    return len(linea) <= 60 and len(re.findall(r'[a-záéíóúñ]', linea, re.I)) >= 3 and not linea.startswith('/') \
        and not PATRON_PRECIO.search(linea) and not PATRON_MONTO_SOLO.search(linea)


def precios_de_bloque(texto, moneda_sin_simbolo=''):
    # Las páginas suelen partir el precio en varias líneas ("MENSUAL" / "$35" / "/mes + IVA"): el período se
    # busca también en la línea siguiente y el plan es la línea con texto más cercana hacia arriba.
    lineas = [l.strip() for l in texto.splitlines() if l.strip()]
    precios = []
    for i, linea in enumerate(lineas):
        siguiente = lineas[i + 1] if i + 1 < len(lineas) else ''
        encontrados = [p for p in precios_en_texto(f"{linea} {siguiente}", limite=len(linea)) if p['tipo'] == 'precio']
        solo = PATRON_MONTO_SOLO.search(linea) if moneda_sin_simbolo and not encontrados else None
        if solo and a_monto(solo.group(1)) > 0:
            encontrados = [_precio(a_monto(solo.group(1)), moneda_sin_simbolo, siguiente)]
        if not encontrados:
            continue
        # Si la línea tiene texto además del monto ("$70 por tus primeros 3 meses"), ella misma describe el plan.
        resto = PATRON_PRECIO.sub('', PATRON_MONTO_SOLO.sub('', linea))
        plan = linea if len(re.findall(r'[a-záéíóúñ]', resto, re.I)) >= 8 else next((l for l in reversed(lineas[max(0, i - 5):i]) if _es_titulo(l)), '')
        for p in encontrados:
            precios.append({'plan': plan[:60], 'etiqueta': p['etiqueta'], 'monto': p['monto'], 'moneda': p['moneda'], 'periodo': p['periodo']})
    return precios
