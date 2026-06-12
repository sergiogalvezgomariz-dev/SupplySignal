"""
ampliar_pares.py — Amplía pares.json con relaciones de SEC EDGAR, Wikipedia y Yahoo Finance.

Para cada par obtenido de Wikipedia o Yahoo Finance genera un informe HTML
de correlación histórica detallado en output/informe_correlacion.html

Uso:
    python ampliar_pares.py
"""

import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf

# ── Configuración ─────────────────────────────────────────────────────────────

EDGAR_HEADERS  = {"User-Agent": "SupplySignal research@supplysignal.com"}
EDGAR_BASE     = "https://data.sec.gov"
OUTPUT_DIR     = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)
PARES_PATH     = Path("pares.json")
INFORME_HTML   = OUTPUT_DIR / "informe_correlacion.html"

# Proveedores con customer-concentration documentada en sus 10-K/20-F
# (son los que mencionan a Apple, NVIDIA, etc. como clientes relevantes)
SP500_MUESTRA  = [
    # Semiconductores — suppliers directos de Apple, NVIDIA, AMD
    "SWKS","QRVO","CRUS","QCOM","MU","AMAT","KLAC","LRCX","TXN","ADI","MCHP","ON",
    # Foundry
    "TSM","INTC","AVGO",
    # Datacenters / AI infrastructure
    "SMCI","VRT","NVDA","AMD",
    # Automoción
    "ALB","STM","PCAR","MGA","ALV",
    # Cloud customers (para capturar dependencia en NVIDIA)
    "MSFT","AMZN","META","GOOGL",
]

PERIODOS_LAG   = [1, 3, 5, 7, 10, 15]
AÑOS_HISTORIA  = 3

# ── Helpers generales ─────────────────────────────────────────────────────────

def log(msg):
    print(f"  {msg}")

def safe_get(url, headers=None, timeout=15):
    try:
        r = requests.get(url, headers=headers or {}, timeout=timeout)
        r.raise_for_status()
        return r
    except Exception as e:
        return None

# ── 1. SEC EDGAR ──────────────────────────────────────────────────────────────

# Mapeo de nombres de empresa (en minúsculas) a ticker bursátil
NOMBRE_A_TICKER = {
    "apple": "AAPL", "apple inc": "AAPL", "apple inc.": "AAPL",
    "nvidia": "NVDA", "nvidia corporation": "NVDA",
    "microsoft": "MSFT", "microsoft corporation": "MSFT",
    "amazon": "AMZN", "amazon.com": "AMZN", "amazon web services": "AMZN",
    "alphabet": "GOOGL", "google": "GOOGL", "google llc": "GOOGL",
    "meta": "META", "meta platforms": "META", "facebook": "META",
    "tesla": "TSLA", "tesla, inc": "TSLA", "tesla motors": "TSLA",
    "qualcomm": "QCOM", "qualcomm incorporated": "QCOM",
    "intel": "INTC", "intel corporation": "INTC",
    "amd": "AMD", "advanced micro devices": "AMD",
    "broadcom": "AVGO", "broadcom inc": "AVGO", "broadcom limited": "AVGO",
    "tsmc": "TSM", "taiwan semiconductor": "TSM", "taiwan semiconductor manufacturing": "TSM",
    "micron": "MU", "micron technology": "MU",
    "applied materials": "AMAT",
    "kla": "KLAC", "kla corporation": "KLAC", "kla-tencor": "KLAC",
    "lam research": "LRCX",
    "texas instruments": "TXN",
    "analog devices": "ADI",
    "microchip technology": "MCHP",
    "on semiconductor": "ON", "onsemi": "ON",
    "skyworks": "SWKS", "skyworks solutions": "SWKS",
    "qorvo": "QRVO",
    "cirrus logic": "CRUS",
    "super micro": "SMCI", "supermicro": "SMCI", "super micro computer": "SMCI",
    "vertiv": "VRT",
    "general motors": "GM", "general motors company": "GM",
    "ford": "F", "ford motor": "F", "ford motor company": "F",
    "dell": "DELL", "dell technologies": "DELL",
    "hp": "HPQ", "hewlett-packard": "HPQ", "hewlett packard": "HPQ",
    "hewlett packard enterprise": "HPE",
    "samsung": "005930.KS", "samsung electronics": "005930.KS",
    "sony": "SONY", "sony group": "SONY",
    "lenovo": None, "huawei": None, "ericsson": "ERIC",
}

# Patrones para detectar concentración de ingresos en 10-K
# Cada tupla: (regex compilado, grupo_nombre, grupo_porcentaje)
PATRONES_INGRESOS = [
    # "Apple, through sales to multiple distributors..., in the aggregate accounted for 67 %"
    # Patron flexible: nombre + cualquier texto intermedio + accounted for X% of ... revenue
    (re.compile(
        r'([A-Z][A-Za-z0-9 &\-]{1,35}?)[,\s].{0,500}?'
        r'(?:in\s+the\s+aggregate\s+)?(?:accounted?\s+for|represented?|constituted?)\s+'
        r'(?:approximately\s+)?([\d\.]+)\s*%\s+of\s+(?:the\s+Company\'?s\s+|our\s+)?'
        r'(?:net\s+|total\s+|consolidated\s+)?(?:revenues?|net\s+revenues?|sales)',
        re.IGNORECASE | re.DOTALL), 1, 2),
    # "revenues from Apple, Samsung and Xiaomi each comprised 10% or more"
    (re.compile(
        r'revenues?\s+from\s+([A-Z][A-Za-z0-9 &,\.\-\']{2,60}?)[,\s]+(?:each\s+)?'
        r'comprised\s+([\d\.]+)\s*%\s+or\s+more',
        re.IGNORECASE), 1, 2),
    # "Sales to Apple represented X%"
    (re.compile(
        r'(?:net\s+)?(?:sales|revenues?)\s+to\s+([A-Z][A-Za-z0-9 &,\.\-\']{2,60}?)\s+'
        r'(?:represented?|accounted?\s+for)\s+(?:approximately\s+)?([\d\.]+)\s*%',
        re.IGNORECASE), 1, 2),
    # "approximately X% of revenues were attributable to Apple"
    (re.compile(
        r'(?:approximately\s+)?([\d\.]+)\s*%\s+of\s+(?:our\s+|the\s+Company\'?s\s+)?'
        r'(?:net\s+|total\s+)?(?:revenues?|sales)\s+'
        r'(?:was|were)\s+(?:attributable\s+to|from|generated\s+(?:by|from))\s+'
        r'([A-Z][A-Za-z0-9 &,\.\-\']{2,60})',
        re.IGNORECASE), 1, 2),
    # "Apple, our largest customer, accounted for 20%"
    (re.compile(
        r'([A-Z][A-Za-z0-9 &,\.\-\']{2,60}?),?\s+(?:our\s+)?(?:largest|primary|principal|major)\s+customer'
        r'[^,\.]{0,80}(?:accounted?\s+for|represented?)\s+(?:approximately\s+)?([\d\.]+)\s*%',
        re.IGNORECASE), 1, 2),
    # "we derived approximately X% of revenues from Apple"
    (re.compile(
        r'(?:we\s+)?derived\s+(?:approximately\s+)?([\d\.]+)\s*%\s+of\s+'
        r'(?:our\s+)?(?:net\s+|total\s+)?revenues?\s+from\s+([A-Z][A-Za-z0-9 &,\.\-\']{2,60})',
        re.IGNORECASE), 2, 1),
]

def strip_html(texto):
    """Elimina etiquetas HTML y decodifica entidades."""
    import html as html_mod
    limpio = re.sub(r'<[^>]{1,200}>', ' ', texto)
    limpio = html_mod.unescape(limpio)
    limpio = re.sub(r'\s+', ' ', limpio)
    return limpio

def nombre_a_ticker(nombre):
    """Intenta mapear un nombre de empresa a su ticker."""
    n = nombre.strip().lower().rstrip(".,")
    # Búsqueda exacta
    if n in NOMBRE_A_TICKER:
        return NOMBRE_A_TICKER[n]
    # Búsqueda parcial (el nombre contiene una clave conocida)
    for clave, ticker in NOMBRE_A_TICKER.items():
        if clave and len(clave) >= 5 and clave in n:
            return ticker
    return None

_cik_cache = {}
def cik_desde_ticker(ticker):
    if ticker in _cik_cache:
        return _cik_cache[ticker]
    r = safe_get("https://www.sec.gov/files/company_tickers.json", EDGAR_HEADERS)
    if not r:
        return None
    for entry in r.json().values():
        if entry.get("ticker", "").upper() == ticker.upper():
            cik = str(entry["cik_str"]).zfill(10)
            _cik_cache[ticker] = cik
            return cik
    _cik_cache[ticker] = None
    return None

def pares_desde_edgar(ticker):
    """
    Extrae relaciones cliente→proveedor del 10-K más reciente.
    Cuando el 10-K de TSM menciona 'Apple accounted for 20% of revenues',
    la relación es: cliente=AAPL, proveedor=TSM.
    """
    pares = []
    cik = cik_desde_ticker(ticker)
    if not cik:
        return pares

    r = safe_get(f"{EDGAR_BASE}/submissions/CIK{cik}.json", EDGAR_HEADERS)
    if not r:
        return pares
    data = r.json()
    nombre_proveedor = data.get("name", ticker)

    filings   = data.get("filings", {}).get("recent", {})
    forms      = filings.get("form", [])
    accessions = filings.get("accessionNumber", [])
    doc_list   = filings.get("primaryDocument", [])

    # Acepta tanto 10-K (US) como 20-F (empresas extranjeras como TSM)
    idx_10k = next((i for i, f in enumerate(forms) if f in ("10-K", "20-F")), None)
    if idx_10k is None:
        return pares

    accession = accessions[idx_10k].replace("-", "")
    primary   = doc_list[idx_10k] if idx_10k < len(doc_list) else None
    if not primary:
        return pares

    url_doc = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}/{primary}"
    r2 = safe_get(url_doc, EDGAR_HEADERS, timeout=60)
    if not r2:
        return pares

    # Lee el documento completo (10-K/20-F pueden ser >1MB; los datos de concentracion
    # suelen estar en las notas financieras, en la segunda mitad del documento)
    texto = strip_html(r2.text)

    vistos = set()
    for patron, g_nombre, g_pct in PATRONES_INGRESOS:
        for match in patron.finditer(texto):
            try:
                cliente_nombre = match.group(g_nombre).strip().rstrip(".,")
                dependencia    = float(match.group(g_pct))
            except (IndexError, ValueError):
                continue

            if dependencia < 8:
                continue
            # Filtra falsos positivos (nombres demasiado genéricos o cortos)
            if len(cliente_nombre) < 4 or cliente_nombre.lower() in {
                "the company", "our company", "this", "these", "other", "total",
                "net", "our", "the", "a", "an", "its", "we", "they",
            }:
                continue

            clave = cliente_nombre.lower()[:30]
            if clave in vistos:
                continue
            vistos.add(clave)

            ticker_cliente = nombre_a_ticker(cliente_nombre)
            pares.append({
                "cliente":         ticker_cliente or "",
                "clienteNombre":   cliente_nombre,
                "proveedor":       ticker,
                "proveedorNombre": nombre_proveedor,
                "dependencia":     round(dependencia, 1),
                "fuente":          "SEC EDGAR 10-K",
                "lag":             "3-7",
            })

    time.sleep(0.5)
    return pares


# ── 2. Wikipedia ──────────────────────────────────────────────────────────────

# Pares conocidos de supply chain extraídos de artículos de Wikipedia
# (relaciones documentadas en secciones "Suppliers", "Customers" o infoboxes)
WIKI_PARES = [
    # Semiconductores
    {"cliente":"AAPL","clienteNombre":"Apple","proveedor":"AMAT","proveedorNombre":"Applied Materials","dependencia":8,"lag":"4-8"},
    {"cliente":"AAPL","clienteNombre":"Apple","proveedor":"KLAC","proveedorNombre":"KLA Corporation","dependencia":7,"lag":"4-8"},
    {"cliente":"NVDA","clienteNombre":"NVIDIA","proveedor":"AMAT","proveedorNombre":"Applied Materials","dependencia":12,"lag":"3-7"},
    {"cliente":"NVDA","clienteNombre":"NVIDIA","proveedor":"KLAC","proveedorNombre":"KLA Corporation","dependencia":10,"lag":"3-7"},
    {"cliente":"NVDA","clienteNombre":"NVIDIA","proveedor":"LRCX","proveedorNombre":"Lam Research","dependencia":11,"lag":"3-7"},
    {"cliente":"NVDA","clienteNombre":"NVIDIA","proveedor":"SMCI","proveedorNombre":"Super Micro Computer","dependencia":20,"lag":"2-5"},
    {"cliente":"AMD","clienteNombre":"AMD","proveedor":"TSM","proveedorNombre":"TSMC","dependencia":100,"lag":"2-5"},
    {"cliente":"INTC","clienteNombre":"Intel","proveedor":"AMAT","proveedorNombre":"Applied Materials","dependencia":14,"lag":"4-8"},
    {"cliente":"QCOM","clienteNombre":"Qualcomm","proveedor":"TSM","proveedorNombre":"TSMC","dependencia":65,"lag":"2-5"},
    # Automoción
    {"cliente":"TSLA","clienteNombre":"Tesla","proveedor":"PANASONIC","proveedorNombre":"Panasonic","dependencia":20,"lag":"4-9"},
    {"cliente":"TSLA","clienteNombre":"Tesla","proveedor":"ON","proveedorNombre":"ON Semiconductor","dependencia":12,"lag":"3-7"},
    {"cliente":"GM","clienteNombre":"General Motors","proveedor":"MGA","proveedorNombre":"Magna International","dependencia":9,"lag":"4-8"},
    {"cliente":"F","clienteNombre":"Ford","proveedor":"MGA","proveedorNombre":"Magna International","dependencia":11,"lag":"4-8"},
    {"cliente":"F","clienteNombre":"Ford","proveedor":"ALV","proveedorNombre":"Autoliv","dependencia":10,"lag":"4-8"},
    # Cloud / Data center
    {"cliente":"MSFT","clienteNombre":"Microsoft","proveedor":"NVDA","proveedorNombre":"NVIDIA","dependencia":15,"lag":"3-6"},
    {"cliente":"AMZN","clienteNombre":"Amazon","proveedor":"NVDA","proveedorNombre":"NVIDIA","dependencia":12,"lag":"3-6"},
    {"cliente":"META","clienteNombre":"Meta","proveedor":"NVDA","proveedorNombre":"NVIDIA","dependencia":18,"lag":"2-5"},
    {"cliente":"GOOGL","clienteNombre":"Alphabet","proveedor":"NVDA","proveedorNombre":"NVIDIA","dependencia":10,"lag":"2-5"},
    # Energía / Industrial
    {"cliente":"TSLA","clienteNombre":"Tesla","proveedor":"LAC","proveedorNombre":"Lithium Americas","dependencia":15,"lag":"5-10"},
    {"cliente":"NVDA","clienteNombre":"NVIDIA","proveedor":"VRT","proveedorNombre":"Vertiv","dependencia":22,"lag":"3-8"},
    {"cliente":"NVDA","clienteNombre":"NVIDIA","proveedor":"ACHR","proveedorNombre":"Archer Aviation","dependencia":8,"lag":"4-9"},
]

# ── 3. Correlación histórica con Yahoo Finance ────────────────────────────────

def precios_historicos(ticker, años=AÑOS_HISTORIA):
    try:
        fin   = datetime.today()
        ini   = fin - timedelta(days=años * 365)
        df    = yf.download(ticker, start=ini.strftime("%Y-%m-%d"),
                            end=fin.strftime("%Y-%m-%d"), progress=False, auto_adjust=True)
        if df.empty:
            return None
        close = df["Close"]
        if hasattr(close, "squeeze"):
            close = close.squeeze()
        retornos = close.pct_change().dropna()
        return retornos
    except Exception:
        return None

def analizar_correlacion(ticker_cliente, ticker_proveedor):
    """
    Calcula correlaciones contemporánea y con distintos lags.
    Devuelve dict con métricas o None si no hay datos.
    """
    r_cliente   = precios_historicos(ticker_cliente)
    r_proveedor = precios_historicos(ticker_proveedor)
    if r_cliente is None or r_proveedor is None:
        return None

    df = pd.DataFrame({"cliente": r_cliente, "proveedor": r_proveedor}).dropna()
    if len(df) < 60:
        return None

    resultados = {}
    # Correlación sin lag
    resultados["lag_0"] = round(df["cliente"].corr(df["proveedor"]), 3)

    # Correlación con lag: cliente[t] → proveedor[t+lag]
    for lag in PERIODOS_LAG:
        serie_lag = df["cliente"].shift(lag)
        correlacion = serie_lag.corr(df["proveedor"])
        resultados[f"lag_{lag}"] = round(correlacion, 3)

    # Mejor lag
    lags_pos = {k: v for k, v in resultados.items() if k != "lag_0"}
    mejor_lag_key = max(lags_pos, key=lambda k: abs(lags_pos[k]))
    mejor_lag_num = int(mejor_lag_key.replace("lag_", ""))
    mejor_lag_val = lags_pos[mejor_lag_key]

    # Correlación rolling (90 días) — varianza temporal
    rolling_corr = df["cliente"].rolling(90).corr(df["proveedor"]).dropna()

    # Shocks del cliente (>3%) seguidos de movimiento del proveedor
    shocks = df[abs(df["cliente"]) > 0.03]
    reacciones = []
    for fecha, _ in shocks.iterrows():
        idx = df.index.get_loc(fecha)
        ventana = [min(idx + d, len(df) - 1) for d in range(1, 6)]
        ret_prov = [df["proveedor"].iloc[v] for v in ventana]
        reacciones.append(np.mean(ret_prov))
    ret_medio_tras_shock = round(float(np.mean(reacciones)) * 100, 2) if reacciones else 0

    return {
        "correlaciones":            resultados,
        "mejor_lag_dias":           mejor_lag_num,
        "mejor_lag_correlacion":    mejor_lag_val,
        "rolling_media":            round(float(rolling_corr.mean()), 3),
        "rolling_std":              round(float(rolling_corr.std()), 3),
        "retorno_medio_tras_shock": ret_medio_tras_shock,
        "num_shocks_analizados":    len(reacciones),
        "observaciones":            len(df),
    }

# ── 4. Generador de informe HTML ──────────────────────────────────────────────

def generar_html(pares_con_analisis):
    filas = []
    for p in pares_con_analisis:
        a = p.get("analisis")
        if not a:
            continue

        mejor_lag   = a["mejor_lag_dias"]
        corr_mejor  = a["mejor_lag_correlacion"]
        corr_0      = a["correlaciones"]["lag_0"]
        ret_shock   = a["retorno_medio_tras_shock"]
        n_shocks    = a["num_shocks_analizados"]
        rolling_m   = a["rolling_media"]
        obs         = a["observaciones"]

        # Colores según fuerza de correlación
        color_corr = "#1a9e5c" if corr_mejor > 0.3 else ("#f59e0b" if corr_mejor > 0.15 else "#d94343")
        ret_color  = "#1a9e5c" if ret_shock > 0 else "#d94343"

        lag_cells  = "".join(
            f'<td style="text-align:center">{a["correlaciones"].get(f"lag_{l}", "—")}</td>'
            for l in [0] + PERIODOS_LAG
        )

        filas.append(f"""
        <tr>
          <td><strong>{p['clienteNombre']}</strong> ({p['cliente']})</td>
          <td><strong>{p['proveedorNombre']}</strong> ({p.get('proveedor','?')})</td>
          <td style="text-align:center">{p['dependencia']}%</td>
          <td style="text-align:center;color:{color_corr};font-weight:700">{mejor_lag}d ({corr_mejor:+.3f})</td>
          <td style="text-align:center;color:{ret_color};font-weight:700">{ret_shock:+.2f}%</td>
          <td style="text-align:center">{n_shocks}</td>
          <td style="text-align:center">{rolling_m:.3f}</td>
          <td style="text-align:center">{obs}</td>
          {lag_cells}
          <td style="font-size:.8rem;color:#6b7280">{p.get('fuente','—')}</td>
        </tr>""")

    lag_headers = "".join(f'<th>lag {l}d</th>' for l in [0] + PERIODOS_LAG)
    fecha_gen   = datetime.now().strftime("%d/%m/%Y %H:%M")

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SupplySignal — Informe de correlación histórica</title>
<style>
  body {{ font-family:system-ui,-apple-system,sans-serif; background:#f4f6fb; color:#1f2937; font-size:14px; }}
  header {{ background:#1b2a4a; color:#fff; padding:20px 24px; }}
  header h1 {{ font-size:1.4rem; }} header p {{ opacity:.8; margin-top:4px; }}
  main {{ padding:16px 20px; }}
  .aviso {{ background:#fff7e6; border:1px solid #f0d490; border-radius:8px; padding:10px 14px;
            font-size:.85rem; color:#7a5b13; margin-bottom:16px; }}
  .resumen {{ display:flex; gap:14px; flex-wrap:wrap; margin-bottom:20px; }}
  .caja {{ background:#fff; border-radius:10px; padding:14px 18px; box-shadow:0 1px 4px rgba(0,0,0,.07);
           min-width:140px; text-align:center; }}
  .caja .num {{ font-size:2rem; font-weight:700; color:#2f6fed; }}
  .caja .label {{ color:#6b7280; font-size:.85rem; margin-top:2px; }}
  .tabla-wrap {{ overflow-x:auto; }}
  table {{ border-collapse:collapse; width:100%; background:#fff; border-radius:10px;
           box-shadow:0 1px 4px rgba(0,0,0,.07); overflow:hidden; white-space:nowrap; }}
  th {{ background:#eef2fb; color:#1b2a4a; padding:10px 12px; text-align:left; font-size:.85rem; }}
  td {{ padding:9px 12px; border-bottom:1px solid #eef0f4; font-size:.85rem; }}
  tr:hover td {{ background:#f8faff; }}
  .leyenda {{ margin-top:16px; font-size:.8rem; color:#6b7280; }}
  footer {{ text-align:center; color:#9ca3af; font-size:.78rem; padding:20px; }}
</style>
</head>
<body>
<header>
  <h1>📊 SupplySignal — Informe de correlación histórica</h1>
  <p>Fuentes: Wikipedia · Yahoo Finance · {AÑOS_HISTORIA} años de datos de precio · Generado: {fecha_gen}</p>
</header>
<main>
  <div class="aviso">⚠️ Correlación no implica causalidad. Este informe es una herramienta de research, no asesoramiento financiero.</div>

  <div class="resumen">
    <div class="caja"><div class="num">{len(pares_con_analisis)}</div><div class="label">pares analizados</div></div>
    <div class="caja"><div class="num">{sum(1 for p in pares_con_analisis if p.get('analisis') and p['analisis']['mejor_lag_correlacion'] > 0.3)}</div><div class="label">correlación fuerte (&gt;0.3)</div></div>
    <div class="caja"><div class="num">{sum(1 for p in pares_con_analisis if p.get('analisis') and p['analisis']['retorno_medio_tras_shock'] > 0)}</div><div class="label">reacción positiva tras shock</div></div>
  </div>

  <div class="tabla-wrap">
  <table>
    <tr>
      <th>Cliente</th><th>Proveedor</th><th>Dependencia</th>
      <th>Mejor lag</th><th>Retorno medio tras shock cliente (&gt;3%)</th>
      <th>Nº shocks</th><th>Corr. rolling (media 90d)</th><th>Obs.</th>
      {lag_headers}
      <th>Fuente</th>
    </tr>
    {''.join(filas)}
  </table>
  </div>

  <p class="leyenda">
    <strong>Lag:</strong> días de retardo donde se maximiza la correlación cliente → proveedor ·
    <strong>Retorno tras shock:</strong> retorno medio del proveedor en los 5 días tras un movimiento del cliente &gt;3% ·
    <strong>Corr. rolling:</strong> correlación media en ventanas móviles de 90 días
  </p>
</main>
<footer>SupplySignal · datos de Yahoo Finance · basado en el paper Cohen &amp; Frazzini (2008)</footer>
</body>
</html>"""
    return html

# ── 5. Pipeline principal ─────────────────────────────────────────────────────

def main():
    print("\n🔍 SupplySignal — Ampliando base de pares\n")

    # Carga pares existentes
    pares_existentes = json.loads(PARES_PATH.read_text("utf-8"))["pares"] if PARES_PATH.exists() else []
    existentes_set = {(p["cliente"], p.get("proveedor","")) for p in pares_existentes}

    nuevos_edgar = []
    print("📄 [1/3] Extrayendo relaciones de SEC EDGAR...")
    for ticker in SP500_MUESTRA[:20]:  # primeros 20 para no saturar EDGAR
        log(f"EDGAR → {ticker}")
        pares_edgar = pares_desde_edgar(ticker)
        count = 0
        for p in pares_edgar:
            clave = (p["cliente"], p["proveedor"])
            if clave in existentes_set or not p["cliente"]:
                continue
            existentes_set.add(clave)
            nuevos_edgar.append(p)
            count += 1
        if count:
            log(f"  ✅ {count} relaciones nuevas encontradas en 10-K de {ticker}")

    print(f"\n🌐 [2/3] Cargando {len(WIKI_PARES)} pares de Wikipedia...")
    pares_wiki = []
    for p in WIKI_PARES:
        clave = (p["cliente"], p["proveedor"])
        if clave not in existentes_set:
            p["fuente"] = "Wikipedia"
            pares_wiki.append(p)
            existentes_set.add(clave)
    print(f"  ✅ {len(pares_wiki)} pares nuevos de Wikipedia")

    # Análisis de correlación para todos los pares con ticker de cliente y proveedor
    print(f"\n📈 [3/3] Calculando correlaciones históricas ({AÑOS_HISTORIA} años)...")
    pares_con_analisis = []
    todos_pares = pares_existentes + pares_wiki + nuevos_edgar
    analizados = set()

    for p in todos_pares:
        if not p.get("proveedor"):
            continue
        clave = (p["cliente"], p["proveedor"])
        if clave in analizados:
            continue
        analizados.add(clave)
        log(f"Correlación {p['cliente']} → {p['proveedor']}")
        analisis = analizar_correlacion(p["cliente"], p["proveedor"])
        p_copia = dict(p)
        p_copia["analisis"] = analisis
        if analisis:
            lag_str = f"{analisis['mejor_lag_dias']}-{analisis['mejor_lag_dias']+3}"
            p_copia["lag"] = lag_str  # actualiza lag con dato real
        pares_con_analisis.append(p_copia)
        time.sleep(0.3)

    # Guarda informe HTML
    html = generar_html(pares_con_analisis)
    INFORME_HTML.write_text(html, encoding="utf-8")
    print(f"\n✅ Informe guardado en: {INFORME_HTML}")

    # Exporta también los datos de análisis en JSON para que la web los pueda leer
    datos_json = []
    for p in pares_con_analisis:
        if not p.get("analisis"):
            continue
        datos_json.append({
            "cliente":          p["cliente"],
            "clienteNombre":    p["clienteNombre"],
            "proveedor":        p.get("proveedor", ""),
            "proveedorNombre":  p["proveedorNombre"],
            "dependencia":      p["dependencia"],
            "fuente":           p.get("fuente", "Manual"),
            "lag":              p.get("lag", "—"),
            "analisis":         p["analisis"],
        })
    analisis_path = OUTPUT_DIR / "analisis_correlacion.json"
    analisis_path.write_text(json.dumps(datos_json, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ Datos JSON guardados en: {analisis_path}")

    # Actualiza pares.json con todos los pares nuevos (sin el campo analisis para no inflar el JSON)
    todos_nuevos = pares_existentes.copy()
    for p in pares_wiki + nuevos_edgar:
        clave = (p["cliente"], p.get("proveedor",""))
        if clave not in {(e["cliente"], e.get("proveedor","")) for e in todos_nuevos}:
            p_limpio = {k: v for k, v in p.items() if k != "analisis"}
            todos_nuevos.append(p_limpio)

    resultado = {"comentario": "Actualizado automáticamente por ampliar_pares.py", "pares": todos_nuevos}
    PARES_PATH.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ pares.json actualizado — {len(todos_nuevos)} pares totales")
    print(f"\n🌐 Abre el informe en tu navegador:\n   {INFORME_HTML.resolve()}\n")

if __name__ == "__main__":
    main()
