"""
edgar_bot.py  —  Extrae relaciones cliente→proveedor de 10-K/20-F en SEC EDGAR.

Estrategia:
  Para cada proveedor de la lista, descarga su filing más reciente y busca
  el nombre exacto de cada cliente conocido junto a un porcentaje de ingresos.
  Sólo usa datos reales de EDGAR — ningún dato inventado.

Uso:
    python edgar_bot.py
"""

import json
import re
import time
import html as html_mod
from pathlib import Path

import requests

# ── Config ────────────────────────────────────────────────────────────────────

HEADERS  = {"User-Agent": "SupplySignal research@supplysignal.com"}
OUT_FILE = Path("output/edgar_suppliers.json")
OUT_FILE.parent.mkdir(exist_ok=True)

# Clientes que buscamos por nombre en los filings de sus proveedores
# clave: nombre tal como aparece en los 10-K  →  valor: ticker bursátil
CLIENTES = {
    "Apple":                "AAPL",
    "Apple Inc":            "AAPL",
    "NVIDIA":               "NVDA",
    "Nvidia":               "NVDA",
    "Microsoft":            "MSFT",
    "Amazon":               "AMZN",
    "Amazon Web Services":  "AMZN",
    "Alphabet":             "GOOGL",
    "Google":               "GOOGL",
    "Meta":                 "META",
    "Facebook":             "META",
    "Tesla":                "TSLA",
    "Tesla, Inc":           "TSLA",
    "Qualcomm":             "QCOM",
    "Intel":                "INTC",
    "AMD":                  "AMD",
    "Advanced Micro Devices": "AMD",
    "Samsung":              "005930.KS",
    "Samsung Electronics":  "005930.KS",
    "Xiaomi":               None,
    "Ford":                 "F",
    "Ford Motor":           "F",
    "General Motors":       "GM",
    "Dell":                 "DELL",
    "Dell Technologies":    "DELL",
    "HP":                   "HPQ",
    "Hewlett":              "HPQ",
    "Cisco":                "CSCO",
    "Boeing":               "BA",
    "Broadcom":             "AVGO",
    "IBM":                  "IBM",
    "Oracle":               "ORCL",
}

# Proveedores a analizar (sus 10-K dicen quiénes son sus clientes)
PROVEEDORES = [
    "SWKS",   # Skyworks       → Apple ~67%
    "QRVO",   # Qorvo          → Apple ~50%
    "CRUS",   # Cirrus Logic   → Apple ~90%
    "QCOM",   # Qualcomm       → Apple, Samsung, Xiaomi
    "MU",     # Micron         → Apple, NVIDIA, etc.
    "AVGO",   # Broadcom       → Apple ~20%
    "AMAT",   # Applied Materials
    "KLAC",   # KLA Corp
    "LRCX",   # Lam Research
    "TXN",    # Texas Instruments
    "ADI",    # Analog Devices
    "MCHP",   # Microchip Technology
    "ON",     # ON Semiconductor
    "STM",    # STMicroelectronics (20-F)
    "TSM",    # TSMC (20-F)
    "SMCI",   # Super Micro Computer
    "VRT",    # Vertiv
    "JBL",    # Jabil
    "FLEX",   # Flex Ltd
    "MGA",    # Magna International
    "ALV",    # Autoliv
    "BWA",    # BorgWarner
    "LEA",    # Lear Corporation
    "APTV",   # Aptiv
    "SPR",    # Spirit AeroSystems
    "HXL",    # Hexcel
    "TDG",    # TransDigm
    "HEI",    # HEICO
    "WWD",    # Woodward
    "SLB",    # SLB (Schlumberger)
    "HAL",    # Halliburton
    "BKR",    # Baker Hughes
    "MRVL",   # Marvell Technology
    "GLW",    # Corning
    "IFF",    # International Flavors
    "BLL",    # Ball Corporation
    "CTLT",   # Catalent
]

# Patrones de porcentaje de ingresos que pueden aparecer en los 10-K
# Se aplican buscando el nombre del cliente específico en el texto
PATRON_PCT = re.compile(
    r"([\d\.]+)\s*%[^\.]{0,150}"
    r"(?:of\s+(?:the\s+[Cc]ompany[\'’]?s?\s+|our\s+)?)"
    r"(?:net\s+|total\s+|consolidated\s+)?(?:revenues?|net\s+revenues?|sales)",
    re.IGNORECASE,
)

# También para "each comprised X% or more" (QCOM-style)
PATRON_COMPRISED = re.compile(
    r"each\s+comprised\s+([\d\.]+)\s*%\s+or\s+more",
    re.IGNORECASE,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

_cik_map = None

def get_cik(ticker: str) -> str | None:
    global _cik_map
    if _cik_map is None:
        r = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers=HEADERS, timeout=15,
        )
        _cik_map = {
            v["ticker"].upper(): str(v["cik_str"]).zfill(10)
            for v in r.json().values()
        }
    return _cik_map.get(ticker.upper())


def get_filing(cik: str) -> tuple[str, str] | None:
    """Devuelve (url_documento, tipo_form) del 10-K o 20-F más reciente."""
    r = requests.get(
        f"https://data.sec.gov/submissions/CIK{cik}.json",
        headers=HEADERS, timeout=15,
    )
    data     = r.json()
    filings  = data.get("filings", {}).get("recent", {})
    forms    = filings.get("form", [])
    accs     = filings.get("accessionNumber", [])
    docs     = filings.get("primaryDocument", [])
    nombre   = data.get("name", "")

    idx = next((i for i, f in enumerate(forms) if f in ("10-K", "20-F")), None)
    if idx is None:
        return None

    acc  = accs[idx].replace("-", "")
    doc  = docs[idx]
    form = forms[idx]
    url  = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{doc}"
    return url, form, nombre


def fetch_text(url: str) -> str:
    """Descarga el HTML y devuelve texto plano."""
    r     = requests.get(url, headers=HEADERS, timeout=90)
    clean = re.sub(r"<[^>]{1,800}>", " ", r.text)
    clean = html_mod.unescape(clean)
    clean = re.sub(r"\s+", " ", clean)
    return clean


# ── Extracción ────────────────────────────────────────────────────────────────

def buscar_cliente_en_texto(nombre_cliente: str, texto: str) -> float | None:
    """
    Busca 'nombre_cliente' en el texto y extrae el porcentaje de ingresos
    que representa. Devuelve el mayor porcentaje encontrado (fiscal year más reciente)
    o None si no hay match.
    """
    # Escapamos el nombre para búsqueda literal
    patron_nombre = re.compile(re.escape(nombre_cliente), re.IGNORECASE)

    porcentajes = []
    for m in patron_nombre.finditer(texto):
        pos   = m.end()
        # Ventana: 800 chars después del nombre
        ventana = texto[pos : pos + 800]

        # Patrón 1: "X% of ... revenues/sales"
        hit = PATRON_PCT.search(ventana)
        if hit:
            try:
                porcentajes.append(float(hit.group(1)))
            except ValueError:
                pass

        # Patrón 2: "each comprised X% or more" (cuando el nombre está en una lista)
        # Buscar también 800 chars ANTES del nombre
        ventana_antes = texto[max(0, m.start() - 200) : m.start()]
        hit2 = PATRON_COMPRISED.search(ventana + ventana_antes)
        if hit2:
            try:
                porcentajes.append(float(hit2.group(1)))
            except ValueError:
                pass

    if not porcentajes:
        return None

    # Filtra valores poco creíbles (>99% o <5%)
    validos = [p for p in porcentajes if 5 <= p <= 99]
    return max(validos) if validos else None


# ── Pipeline principal ────────────────────────────────────────────────────────

def main():
    print("\nSEC EDGAR Supplier Bot — datos 100% de EDGAR")
    print("=" * 55)

    resultados = []

    for ticker in PROVEEDORES:
        print(f"\n[{ticker}]", end=" ", flush=True)

        cik = get_cik(ticker)
        if not cik:
            print("CIK no encontrado")
            continue

        filing = get_filing(cik)
        if not filing:
            print("Sin 10-K/20-F en EDGAR")
            continue

        url, form, nombre_empresa = filing
        print(f"{form} | {nombre_empresa}")
        print(f"  {url}")
        print(f"  Descargando...", end=" ", flush=True)

        try:
            texto = fetch_text(url)
            print(f"{len(texto):,} chars")
        except Exception as e:
            print(f"ERROR: {e}")
            continue

        encontrados = []
        for nombre_cliente, ticker_cliente in CLIENTES.items():
            pct = buscar_cliente_en_texto(nombre_cliente, texto)
            if pct is None:
                continue

            # Evitar duplicados (mismo ticker cliente ya encontrado con otro nombre)
            ya_tenemos = any(
                r["cliente"] == ticker_cliente and r["proveedor"] == ticker
                for r in encontrados
            )
            if ya_tenemos:
                # Actualizar si el nuevo pct es mayor
                for r in encontrados:
                    if r["cliente"] == ticker_cliente and r["proveedor"] == ticker:
                        if pct > r["dependencia"]:
                            r["dependencia"] = round(pct, 1)
                continue

            encontrados.append({
                "cliente":         ticker_cliente or "",
                "clienteNombre":   nombre_cliente,
                "proveedor":       ticker,
                "proveedorNombre": nombre_empresa,
                "dependencia":     round(pct, 1),
                "fuente":          f"SEC EDGAR {form}",
                "lag":             "3-7",
            })

        if encontrados:
            print(f"  Clientes encontrados: {len(encontrados)}")
            for r in encontrados:
                tc = f"[{r['cliente']}]" if r["cliente"] else "[sin ticker]"
                print(f"    {tc} {r['clienteNombre']:30s}  {r['dependencia']}%")
            resultados.extend(encontrados)
        else:
            print("  Sin relaciones encontradas")

        time.sleep(0.6)  # Rate limit EDGAR

    # Guardar
    OUT_FILE.write_text(
        json.dumps(resultados, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\n{'='*55}")
    print(f"Total relaciones: {len(resultados)}")
    print(f"Guardado en:      {OUT_FILE}")

    from collections import Counter
    print("\nPor proveedor:")
    for t, n in Counter(r["proveedor"] for r in resultados).most_common():
        clientes = [r["clienteNombre"] for r in resultados if r["proveedor"] == t]
        print(f"  {t:6s}  {n} cliente(s): {', '.join(clientes)}")


if __name__ == "__main__":
    main()
