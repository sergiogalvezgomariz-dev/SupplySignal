"""
earnings_bot.py  —  Obtiene el próximo earnings call de todas las compañías seguidas.

Lee los tickers de pares.json, consulta Yahoo Finance Y SEC EDGAR, y guarda
output/earnings.json con fechas, estimaciones de EPS/revenue y días restantes.

Lógica de fecha:
  1. SEC EDGAR: busca 8-K o Form 4 con "earnings" en el título publicado en los
     últimos 90 días o scheduled. Si encuentra una fecha oficial, la usa.
  2. Yahoo Finance: fecha estimada del calendario. Sirve como fallback o confirmación.
  Si ambas fuentes tienen fecha, se usa EDGAR (oficial). Si sólo una, esa.

Uso:
    python earnings_bot.py          → actualiza output/earnings.json
    python earnings_bot.py --print  → muestra tabla en consola
    python earnings_bot.py --ticker AAPL  → sólo un ticker
"""

import sys
import json
import datetime
import time
import re
import urllib.request
import urllib.error
from pathlib import Path

import yfinance as yf

PARES_FILE = Path("pares.json")
OUT_FILE   = Path("output/earnings.json")
OUT_FILE.parent.mkdir(exist_ok=True)

EDGAR_UA   = "SupplySignal research@supplysignal.com"
EDGAR_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# Cache del mapa ticker→CIK para no pedirlo por cada empresa
_cik_map: dict[str, int] = {}


# ── Helpers EDGAR ─────────────────────────────────────────────────────────────

def edgar_get(url: str) -> dict | None:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": EDGAR_UA, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def load_cik_map() -> None:
    """Carga el mapa ticker→CIK de EDGAR una sola vez."""
    global _cik_map
    if _cik_map:
        return
    data = edgar_get(EDGAR_TICKERS_URL)
    if not data:
        return
    for entry in data.values():
        t = (entry.get("ticker") or "").upper()
        if t:
            _cik_map[t] = int(entry["cik_str"])


def get_cik(ticker: str) -> int | None:
    load_cik_map()
    return _cik_map.get(ticker.upper())


def parse_edgar_date(date_str: str) -> datetime.date | None:
    """Convierte strings como '2025-10-29' o '10/29/2025' a date."""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            pass
    return None


# Palabras clave que aparecen en 8-Ks de anuncio de earnings date
_EARNINGS_KW = re.compile(
    r"(earnings|results|quarterly|financial results|q[1-4]\s*(fiscal|fy)?\s*20\d\d"
    r"|fourth quarter|third quarter|second quarter|first quarter)",
    re.IGNORECASE,
)

# Fechas en texto: "October 29, 2025", "Oct 29, 2025", "2025-10-29"
_DATE_PATTERN = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?"
    r"|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+\d{1,2},?\s+20\d\d"
    r"|\b20\d\d-\d{2}-\d{2}\b",
    re.IGNORECASE,
)


def get_edgar_earnings_date(ticker: str) -> tuple[datetime.date | None, str | None]:
    """
    Busca en SEC EDGAR la fecha oficial del próximo earnings.
    Devuelve (fecha, url_del_8K) o (None, None).

    Estrategia:
      1. Obtiene los últimos 50 filings del ticker.
      2. Filtra 8-K con descripción relacionada con earnings publicados en
         los últimos 180 días.
      3. Descarga el documento primario y busca una fecha futura en el texto.
    """
    cik = get_cik(ticker)
    if not cik:
        return None, None

    cik_padded = f"{cik:010d}"
    subs = edgar_get(f"https://data.sec.gov/submissions/CIK{cik_padded}.json")
    if not subs:
        return None, None

    recent  = (subs.get("filings") or {}).get("recent") or {}
    forms   = recent.get("form",                [])
    dates   = recent.get("filingDate",          [])
    acc_nos = recent.get("accessionNumber",     [])
    docs    = recent.get("primaryDocument",     [])
    desc    = recent.get("primaryDocDescription", [])

    hoy     = datetime.date.today()
    cutoff  = hoy - datetime.timedelta(days=180)

    candidates = []
    for i, form in enumerate(forms):
        if form != "8-K":
            continue
        filing_date_str = dates[i] if i < len(dates) else ""
        try:
            filing_date = datetime.date.fromisoformat(filing_date_str)
        except ValueError:
            continue
        if filing_date < cutoff:
            break  # Los filings están ordenados por fecha desc; podemos parar

        desc_i = (desc[i] if i < len(desc) else "").lower()
        if _EARNINGS_KW.search(desc_i) or _EARNINGS_KW.search(
            (docs[i] if i < len(docs) else "").lower()
        ):
            acc = (acc_nos[i] if i < len(acc_nos) else "").replace("-", "")
            doc = docs[i] if i < len(docs) else ""
            doc_url = (
                f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
                if doc else ""
            )
            candidates.append((filing_date, doc_url))

    # Busca en el texto del 8-K una fecha futura (la del earnings call)
    for _, doc_url in candidates[:5]:  # Máximo 5 documentos
        if not doc_url:
            continue
        try:
            req = urllib.request.Request(
                doc_url,
                headers={"User-Agent": EDGAR_UA},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read().decode("utf-8", errors="replace")
            # Quita HTML
            text = re.sub(r"<[^>]+>", " ", raw)
            text = re.sub(r"\s+", " ", text)

            # Busca fechas futuras en el texto
            for m in _DATE_PATTERN.finditer(text):
                d = parse_edgar_date(m.group())
                if d and d >= hoy:
                    return d, doc_url
        except Exception:
            pass

        time.sleep(0.3)  # Respeta el rate limit de EDGAR

    return None, None


# ── Extrae todos los tickers únicos de pares.json ─────────────────────────────

def get_tickers() -> list[str]:
    pares = json.loads(PARES_FILE.read_text(encoding="utf-8"))["pares"]
    tickers = set()
    for p in pares:
        if p.get("cliente"):   tickers.add(p["cliente"])
        if p.get("proveedor"): tickers.add(p["proveedor"])
    return sorted(tickers)


# ── Consulta Yahoo Finance para un ticker ─────────────────────────────────────

def get_yahoo_earnings_date(ticker: str) -> tuple[datetime.date | None, dict]:
    """Devuelve (fecha, datos_extra) de Yahoo Finance."""
    try:
        t   = yf.Ticker(ticker)
        info = t.info
        cal  = t.calendar

        nombre    = info.get("longName") or info.get("shortName", ticker)
        fecha_yf  = None

        if isinstance(cal, dict):
            dates = cal.get("Earnings Date", [])
            if dates:
                fecha_raw = dates[0]
                if hasattr(fecha_raw, "year"):
                    fecha_yf = fecha_raw if isinstance(fecha_raw, datetime.date) else fecha_raw.date()
                else:
                    try:
                        fecha_yf = datetime.date.fromisoformat(str(fecha_raw)[:10])
                    except ValueError:
                        pass

        extras = {
            "nombre":          nombre,
            "sector":          info.get("sector",    "—"),
            "industria":       info.get("industry",  "—"),
            "marketCap":       info.get("marketCap"),
            "epsEstimado":     round(cal["Earnings Average"], 4) if isinstance(cal, dict) and cal.get("Earnings Average") is not None else None,
            "epsEstimadoAlto": round(cal["Earnings High"],    4) if isinstance(cal, dict) and cal.get("Earnings High")    is not None else None,
            "epsEstimadoBajo": round(cal["Earnings Low"],     4) if isinstance(cal, dict) and cal.get("Earnings Low")     is not None else None,
            "revenueEstimado": int(cal["Revenue Average"])       if isinstance(cal, dict) and cal.get("Revenue Average")  is not None else None,
            "epsActual":       round(info["trailingEps"], 4)     if info.get("trailingEps") is not None else None,
        }
        return fecha_yf, extras

    except Exception as e:
        return None, {
            "nombre": ticker, "sector": "—", "industria": "—",
            "marketCap": None, "epsEstimado": None, "epsEstimadoAlto": None,
            "epsEstimadoBajo": None, "revenueEstimado": None, "epsActual": None,
            "error": str(e),
        }


# ── Pipeline principal por ticker ─────────────────────────────────────────────

def get_earnings_data(ticker: str) -> dict:
    fecha_yf, extras = get_yahoo_earnings_date(ticker)

    # Intenta EDGAR para fecha oficial
    fecha_edgar, edgar_url = None, None
    try:
        fecha_edgar, edgar_url = get_edgar_earnings_date(ticker)
    except Exception:
        pass

    # Resolución: EDGAR gana si tiene fecha futura
    hoy = datetime.date.today()
    fecha_final = None
    source      = "none"

    if fecha_edgar and fecha_edgar >= hoy:
        fecha_final = fecha_edgar
        source      = "edgar"
    elif fecha_yf and fecha_yf >= hoy:
        fecha_final = fecha_yf
        source      = "yahoo"
    elif fecha_yf:
        # Yahoo tiene una fecha pasada (puede ser la última reportada)
        fecha_final = fecha_yf
        source      = "yahoo_past"

    fecha_str      = fecha_final.isoformat() if fecha_final else None
    dias_restantes = (fecha_final - hoy).days if fecha_final else None

    return {
        "ticker":           ticker,
        "nombre":           extras.get("nombre", ticker),
        "sector":           extras.get("sector",    "—"),
        "industria":        extras.get("industria", "—"),
        "marketCap":        extras.get("marketCap"),
        "proximoEarnings":  fecha_str,
        "diasRestantes":    dias_restantes,
        "fechaSource":      source,
        "edgarUrl":         edgar_url,
        "fechaYahoo":       fecha_yf.isoformat()   if fecha_yf   else None,
        "fechaEdgar":       fecha_edgar.isoformat() if fecha_edgar else None,
        "epsEstimado":      extras.get("epsEstimado"),
        "epsEstimadoAlto":  extras.get("epsEstimadoAlto"),
        "epsEstimadoBajo":  extras.get("epsEstimadoBajo"),
        "revenueEstimado":  extras.get("revenueEstimado"),
        "epsActual":        extras.get("epsActual"),
        "ok":               fecha_str is not None,
        **({"error": extras["error"]} if "error" in extras else {}),
    }


# ── Tabla de consola ──────────────────────────────────────────────────────────

def imprimir_tabla(resultados: list[dict]) -> None:
    hoy = datetime.date.today()
    print(f"\nEarnings Calendar  —  {hoy.strftime('%d %b %Y')}")
    print("=" * 95)
    print(f"{'Ticker':<8} {'Fecha':<13} {'Días':<6} {'Fuente':<8} {'EPS est.':<10} {'Nombre'}")
    print("-" * 95)

    con_fecha = [r for r in resultados if r.get("proximoEarnings")]
    sin_fecha = [r for r in resultados if not r.get("proximoEarnings")]

    SOURCE_LABEL = {
        "edgar":      "EDGAR ✓",
        "yahoo":      "Yahoo",
        "yahoo_past": "Yahoo~",
        "none":       "—",
    }

    for r in sorted(con_fecha, key=lambda x: x["proximoEarnings"]):
        fecha   = r["proximoEarnings"]
        dias    = r.get("diasRestantes")
        dias_s  = f"{dias}d" if dias is not None else "—"
        eps_s   = f"${r['epsEstimado']:.2f}" if r.get("epsEstimado") is not None else "—"
        nombre  = (r.get("nombre") or "")[:32]
        src     = SOURCE_LABEL.get(r.get("fechaSource", "none"), "—")

        if dias is not None and dias <= 7:
            prefix = "  *** "
        elif dias is not None and dias <= 30:
            prefix = "  *   "
        else:
            prefix = "      "

        print(f"{prefix}{r['ticker']:<8} {fecha:<13} {dias_s:<6} {src:<8} {eps_s:<10} {nombre}")

    if sin_fecha:
        print(f"\n  Sin fecha disponible: {', '.join(r['ticker'] for r in sin_fecha)}")

    print("=" * 95)
    proximos = [r for r in con_fecha if r.get("diasRestantes") is not None and 0 <= r["diasRestantes"] <= 30]
    if proximos:
        print(f"  Próximos 30 días ({len(proximos)}): {', '.join(r['ticker'] for r in proximos)}")

    edgar_count = sum(1 for r in resultados if r.get("fechaSource") == "edgar")
    yahoo_count = sum(1 for r in resultados if r.get("fechaSource") in ("yahoo", "yahoo_past"))
    print(f"  Fuentes: EDGAR={edgar_count}  Yahoo={yahoo_count}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Soporte para --ticker AAPL (sólo un ticker)
    single = None
    if "--ticker" in sys.argv:
        idx = sys.argv.index("--ticker")
        if idx + 1 < len(sys.argv):
            single = sys.argv[idx + 1].upper()

    tickers = [single] if single else get_tickers()

    print(f"\nEarnings Bot  —  {len(tickers)} tickers  (Yahoo Finance + SEC EDGAR)")
    print("=" * 60)

    # Pre-carga el mapa CIK una sola vez (evita 34 peticiones individuales)
    print("  Cargando mapa CIK de EDGAR...", end=" ", flush=True)
    load_cik_map()
    print(f"OK ({len(_cik_map)} empresas)")

    resultados = []
    for i, ticker in enumerate(tickers, 1):
        print(f"  [{i:2d}/{len(tickers)}] {ticker:<8}", end=" ", flush=True)
        datos = get_earnings_data(ticker)
        resultados.append(datos)

        if datos.get("proximoEarnings"):
            dias   = datos.get("diasRestantes")
            dias_s = f"en {dias}d" if dias is not None else ""
            src    = datos.get("fechaSource", "")
            print(f"{datos['proximoEarnings']}  {dias_s:<8} [{src}]")
        elif datos.get("error"):
            print(f"ERROR: {datos['error'][:60]}")
        else:
            print("Sin fecha")

    salida = {
        "actualizadoEn": datetime.datetime.now().isoformat(),
        "total":         len(resultados),
        "conFecha":      sum(1 for r in resultados if r.get("proximoEarnings")),
        "fuenteEdgar":   sum(1 for r in resultados if r.get("fechaSource") == "edgar"),
        "fuenteYahoo":   sum(1 for r in resultados if r.get("fechaSource") in ("yahoo", "yahoo_past")),
        "earnings":      resultados,
    }

    if single:
        # En modo single ticker, sólo actualiza ese entrada en el JSON existente
        if OUT_FILE.exists():
            try:
                existing = json.loads(OUT_FILE.read_text(encoding="utf-8"))
                existing["earnings"] = [
                    r if r["ticker"] != single else resultados[0]
                    for r in existing["earnings"]
                ]
                if not any(r["ticker"] == single for r in existing["earnings"]):
                    existing["earnings"].append(resultados[0])
                existing["actualizadoEn"] = salida["actualizadoEn"]
                OUT_FILE.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                OUT_FILE.write_text(json.dumps(salida, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            OUT_FILE.write_text(json.dumps(salida, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        OUT_FILE.write_text(json.dumps(salida, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nGuardado en: {OUT_FILE.resolve()}")

    if "--print" in sys.argv or (len(sys.argv) == 1 and not single):
        imprimir_tabla(resultados)


if __name__ == "__main__":
    main()
