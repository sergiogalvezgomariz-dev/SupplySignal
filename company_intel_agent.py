"""
company_intel_agent.py  —  Company Intelligence Aggregator para SupplySignal.

Recopila datos de tres fuentes para un ticker:
  1. Yahoo Finance  → precio, métricas financieras, estimaciones, noticias
  2. SEC EDGAR      → filings recientes (10-K, 10-Q, 8-K) con URLs directas
  3. pares.json     → relaciones de cadena de suministro

Guarda en output/company_intel/{TICKER}.json con timestamp para caché 24h.

Uso:
    python company_intel_agent.py --ticker AAPL
    python company_intel_agent.py --ticker MU --force   # ignora caché
"""

import sys, json, os, datetime, argparse, re, time
from pathlib import Path

import yfinance as yf

# ── .env ──────────────────────────────────────────────────────────────────────
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8-sig").splitlines():
        if "=" in _line and not _line.startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# ── Config ────────────────────────────────────────────────────────────────────
PARES_FILE  = Path("pares.json")
INTEL_DIR   = Path("output/company_intel")
INTEL_DIR.mkdir(parents=True, exist_ok=True)
CACHE_HOURS = 24
EDGAR_UA    = "SupplySignal research@supplysignal.com"

# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt_num(v, prefix="$", suffix=""):
    if v is None: return "—"
    try:
        v = float(v)
        if abs(v) >= 1e12: return f"{prefix}{v/1e12:.2f}T{suffix}"
        if abs(v) >= 1e9:  return f"{prefix}{v/1e9:.2f}B{suffix}"
        if abs(v) >= 1e6:  return f"{prefix}{v/1e6:.1f}M{suffix}"
        return f"{prefix}{v:,.2f}{suffix}"
    except: return "—"

def fmt_pct(v):
    if v is None: return "—"
    try: return f"{float(v)*100:.1f}%"
    except: return "—"

def strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def edgar_get(url: str) -> dict | str | None:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": EDGAR_UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            ct = r.headers.get("Content-Type", "")
            raw = r.read().decode("utf-8", errors="replace")
            if "json" in ct:
                return json.loads(raw)
            return raw
    except Exception as e:
        return None

# ── Skill 1: Yahoo Finance ────────────────────────────────────────────────────

def get_yahoo_financials(ticker: str) -> dict:
    t    = yf.Ticker(ticker)
    info = t.info or {}

    price       = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose")
    prev_close  = info.get("previousClose") or info.get("regularMarketPreviousClose")
    chg_pct     = round(((price - prev_close) / prev_close) * 100, 2) if price and prev_close else None

    # EPS history
    quarters = []
    try:
        hist = t.earnings_history
        if hist is not None and not hist.empty:
            for dt_idx, row in hist.iterrows():
                actual   = row.get("epsActual")
                estimate = row.get("epsEstimate")
                surprise = row.get("surprisePercent")
                beat     = bool(actual > estimate) if (actual is not None and estimate is not None) else None
                quarters.append({
                    "quarter":     str(dt_idx)[:10],
                    "epsActual":   round(float(actual),   3) if actual   is not None else None,
                    "epsEstimate": round(float(estimate), 3) if estimate is not None else None,
                    "surprisePct": round(float(surprise) * 100, 1) if surprise is not None else None,
                    "beat":        beat,
                })
    except Exception: pass

    # Estimaciones próximo trimestre
    cal       = t.calendar or {}
    eps_next  = cal.get("Earnings Average") if isinstance(cal, dict) else None
    rev_next  = cal.get("Revenue Average")  if isinstance(cal, dict) else None
    earn_date = None
    if isinstance(cal, dict):
        ed = cal.get("Earnings Date")
        if ed:
            earn_date = str(ed[0])[:10] if isinstance(ed, list) else str(ed)[:10]

    # Noticias recientes
    news = []
    try:
        raw_news = t.news or []
        for n in raw_news[:6]:
            ct = n.get("content") or {}
            title = ct.get("title") or n.get("title", "")
            url   = (ct.get("canonicalUrl") or {}).get("url") or n.get("link") or ""
            prov  = (ct.get("provider") or {}).get("displayName") or n.get("publisher", "")
            pub   = ct.get("pubDate") or n.get("providerPublishTime", "")
            if isinstance(pub, int):
                pub = datetime.datetime.fromtimestamp(pub).strftime("%Y-%m-%d")
            elif pub:
                pub = str(pub)[:10]
            if title:
                news.append({"title": title, "url": url, "provider": prov, "date": pub})
    except Exception: pass

    yahoo_url = f"https://finance.yahoo.com/quote/{ticker}"

    return {
        "source":       "Yahoo Finance",
        "sourceUrl":    yahoo_url,
        "nombre":       info.get("longName") or info.get("shortName", ticker),
        "sector":       info.get("sector", "—"),
        "industria":    info.get("industry", "—"),
        "pais":         info.get("country", "—"),
        "descripcion":  (info.get("longBusinessSummary") or "")[:800],
        "precio":       round(float(price), 2) if price else None,
        "changePct":    chg_pct,
        "currency":     info.get("currency", "USD"),
        "marketState":  info.get("marketState", "UNKNOWN"),
        "marketCap":    info.get("marketCap"),
        "marketCapFmt": fmt_num(info.get("marketCap")),
        "enterpriseValue": info.get("enterpriseValue"),
        "evFmt":        fmt_num(info.get("enterpriseValue")),
        "revenue":      info.get("totalRevenue"),
        "revenueFmt":   fmt_num(info.get("totalRevenue")),
        "ebitda":       info.get("ebitda"),
        "ebitdaFmt":    fmt_num(info.get("ebitda")),
        "netIncome":    info.get("netIncomeToCommon"),
        "netIncomeFmt": fmt_num(info.get("netIncomeToCommon")),
        "grossMargin":  fmt_pct(info.get("grossMargins")),
        "opMargin":     fmt_pct(info.get("operatingMargins")),
        "netMargin":    fmt_pct(info.get("profitMargins")),
        "peRatio":      round(float(info["trailingPE"]), 1) if info.get("trailingPE") else None,
        "fwdPE":        round(float(info["forwardPE"]), 1) if info.get("forwardPE") else None,
        "pbRatio":      round(float(info["priceToBook"]), 2) if info.get("priceToBook") else None,
        "evEbitda":     round(float(info["enterpriseToEbitda"]), 1) if info.get("enterpriseToEbitda") else None,
        "roe":          fmt_pct(info.get("returnOnEquity")),
        "roa":          fmt_pct(info.get("returnOnAssets")),
        "debtEquity":   round(float(info["debtToEquity"]) / 100, 2) if info.get("debtToEquity") else None,
        "currentRatio": round(float(info["currentRatio"]), 2) if info.get("currentRatio") else None,
        "beta":         round(float(info["beta"]), 2) if info.get("beta") else None,
        "w52High":      info.get("fiftyTwoWeekHigh"),
        "w52Low":       info.get("fiftyTwoWeekLow"),
        "avgVolume":    info.get("averageVolume"),
        "employees":    info.get("fullTimeEmployees"),
        "epsTrailing":  info.get("trailingEps"),
        "epsForward":   info.get("forwardEps"),
        "epsNextQ":     round(float(eps_next), 3) if eps_next else None,
        "revNextQ":     int(rev_next) if rev_next else None,
        "earningsDate": earn_date,
        "dividendYield":fmt_pct(info.get("dividendYield")),
        "quarters":     quarters[-4:],
        "news":         news,
    }

# ── Skill 2: SEC EDGAR ────────────────────────────────────────────────────────

def get_edgar_filings(ticker: str) -> dict:
    # 1. Obtener CIK
    tickers_data = edgar_get("https://www.sec.gov/files/company_tickers.json")
    if not tickers_data:
        return {"available": False, "error": "Could not reach SEC EDGAR"}

    cik = None
    for entry in tickers_data.values():
        if entry.get("ticker", "").upper() == ticker.upper():
            cik = int(entry["cik_str"])
            company_name = entry.get("title", ticker)
            break
    if not cik:
        return {"available": False, "error": f"Ticker {ticker} not found in EDGAR"}

    cik_padded = f"{cik:010d}"

    # 2. Obtener submissions
    subs = edgar_get(f"https://data.sec.gov/submissions/CIK{cik_padded}.json")
    if not subs:
        return {"available": False, "error": "Could not fetch submissions"}

    recent = subs.get("filings", {}).get("recent", {})
    forms      = recent.get("form",            [])
    dates      = recent.get("filingDate",      [])
    acc_nums   = recent.get("accessionNumber", [])
    primary    = recent.get("primaryDocument", [])
    descriptions = recent.get("primaryDocDescription", [])

    edgar_base    = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={{form}}&dateb=&owner=include&count=10"
    company_page  = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=&dateb=&owner=include&count=40"

    target_forms = {"10-K", "10-Q", "8-K", "DEF 14A", "S-1"}
    filings = []

    for i, form in enumerate(forms):
        if form not in target_forms:
            continue
        if len(filings) >= 15:
            break
        acc   = acc_nums[i].replace("-", "")
        doc   = primary[i] if i < len(primary) else ""
        desc  = descriptions[i] if i < len(descriptions) else ""
        date  = dates[i] if i < len(dates) else ""
        doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}" if doc else ""
        idx_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"

        filings.append({
            "form":        form,
            "date":        date,
            "description": desc,
            "docUrl":      doc_url,
            "indexUrl":    idx_url,
            "accession":   acc_nums[i],
        })

    # Últimos de cada tipo
    by_type = {}
    for f in filings:
        if f["form"] not in by_type:
            by_type[f["form"]] = f

    return {
        "available":    True,
        "cik":          cik,
        "cikPadded":    cik_padded,
        "companyName":  company_name,
        "companyPage":  company_page,
        "edgarSearch":  f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22&dateRange=custom&startdt=2023-01-01&forms=10-K,10-Q,8-K",
        "filings":      filings,
        "latestByType": by_type,
    }

# ── Skill 3: Supply Chain ─────────────────────────────────────────────────────

def get_supply_chain(ticker: str, pares: list) -> dict:
    as_customer  = [p for p in pares if p.get("cliente") == ticker and p.get("proveedor")]
    as_supplier  = [p for p in pares if p.get("proveedor") == ticker]

    suppliers = [
        {
            "ticker":      p["proveedor"],
            "nombre":      p["proveedorNombre"],
            "dependencia": p.get("dependencia"),
            "lag":         p.get("lag"),
            "fuente":      p.get("fuente", "Manual"),
            "yahooUrl":    f"https://finance.yahoo.com/quote/{p['proveedor']}",
        }
        for p in as_customer
    ]
    customers = [
        {
            "ticker":      p["cliente"],
            "nombre":      p["clienteNombre"],
            "dependencia": p.get("dependencia"),
            "lag":         p.get("lag"),
            "fuente":      p.get("fuente", "Manual"),
            "yahooUrl":    f"https://finance.yahoo.com/quote/{p['cliente']}",
        }
        for p in as_supplier
    ]

    return {
        "suppliers":       suppliers,
        "customers":       customers,
        "totalRelaciones": len(suppliers) + len(customers),
        "inPares":         len(suppliers) + len(customers) > 0,
    }

# ── Pipeline principal ────────────────────────────────────────────────────────

def build_company_intel(ticker: str) -> dict:
    ticker = ticker.upper().strip()
    print(f"\n[{ticker}] Building Company Intel...")

    pares = json.loads(PARES_FILE.read_text(encoding="utf-8"))["pares"]

    print(f"  · Yahoo Finance...", end=" ", flush=True)
    yf_data = get_yahoo_financials(ticker)
    print(f"OK — {yf_data.get('nombre', ticker)}")

    print(f"  · SEC EDGAR...", end=" ", flush=True)
    edgar_data = get_edgar_filings(ticker)
    if edgar_data.get("available"):
        print(f"OK — {len(edgar_data['filings'])} filings found")
    else:
        print(f"N/A — {edgar_data.get('error','')}")

    print(f"  · Supply chain...", end=" ", flush=True)
    sc_data = get_supply_chain(ticker, pares)
    print(f"OK — {sc_data['totalRelaciones']} relaciones")

    result = {
        "ticker":       ticker,
        "generadoEn":   datetime.datetime.now().isoformat(),
        "cacheExpires": (datetime.datetime.now() + datetime.timedelta(hours=CACHE_HOURS)).isoformat(),
        "financials":   yf_data,
        "edgar":        edgar_data,
        "supplyChain":  sc_data,
    }

    out = INTEL_DIR / f"{ticker}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Saved → {out}")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--force", action="store_true", help="Ignore cache")
    args = parser.parse_args()

    ticker = args.ticker.upper()
    out    = INTEL_DIR / f"{ticker}.json"

    if not args.force and out.exists():
        data = json.loads(out.read_text(encoding="utf-8"))
        expires = data.get("cacheExpires", "")
        if expires and datetime.datetime.fromisoformat(expires) > datetime.datetime.now():
            print(f"[{ticker}] Cache válido hasta {expires[:16]} — usa --force para actualizar")
            print(json.dumps(data, ensure_ascii=False, indent=2)[:500] + "...")
            return

    result = build_company_intel(ticker)
    print(f"\nDone. {result['financials'].get('nombre', ticker)} — {result['financials'].get('marketCapFmt', '—')}")


if __name__ == "__main__":
    main()
