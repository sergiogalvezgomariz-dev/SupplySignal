"""
earnings_briefing_agent.py  —  Earnings Briefing Agent para SupplySignal.

Para cada empresa con earnings en los próximos N días:
  1. Recoge datos reales de Yahoo Finance (EPS history, estimaciones, info)
  2. Identifica qué proveedores/clientes están expuestos (pares.json)
  3. Llama a Claude API para generar un briefing estructurado
  4. Guarda output/briefings.json

Guardrails:
  · Solo analiza tickers en pares.json
  · Nunca recomienda comprar/vender directamente
  · Marca siempre las estimaciones como consenso, no predicción
  · Requiere al menos 2 trimestres de historial para hablar de tendencia
  · Límite de 48h de anticipación por defecto (configurable)

Memoria:
  · Guarda historial de sorpresas EPS acumulado en output/eps_memory.json
  · Si un ticker ha batido estimaciones N trimestres seguidos, lo señala

Uso:
    python earnings_briefing_agent.py            # próximos 7 días
    python earnings_briefing_agent.py --days 2   # próximas 48h
    python earnings_briefing_agent.py --ticker AAPL  # forzar un ticker concreto
"""

import sys
import json
import os
import datetime
import argparse
from pathlib import Path

import yfinance as yf
import anthropic

# Lee .env manualmente (compatible con BOM y encodings de Windows)
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8-sig").splitlines():
        if "=" in _line and not _line.startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# ── Config ────────────────────────────────────────────────────────────────────

PARES_FILE   = Path("pares.json")
EARNINGS_FILE= Path("output/earnings.json")
OUT_FILE     = Path("output/briefings.json")
MEMORY_FILE  = Path("output/eps_memory.json")
Path("output").mkdir(exist_ok=True)

CLAUDE_MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """You are a senior equity research analyst specializing in supply chain intelligence.
Your task is to generate pre-earnings briefings for companies tracked by the SupplySignal platform.

Your briefings must:
- Be factual and grounded in the data provided
- Highlight the supply chain angle: which suppliers or customers are exposed and why
- Describe EPS trend clearly (beat streak, miss streak, or mixed)
- State market expectations without predicting the outcome
- Flag any unusual patterns (e.g., guidance cuts, revenue concentration risk)
- Be written in clear financial English, concise but complete

Your briefings must NOT:
- Recommend buying or selling any security
- Make price targets or return forecasts
- Speculate beyond what the data supports
- Use hyperbolic language ("incredible", "must-watch", etc.)

Format your response as JSON with this exact structure:
{
  "headline": "One-sentence summary of the most important thing to watch",
  "business_context": "2-3 sentences on what this company does and why it matters in the supply chain",
  "market_expectations": "What consensus expects: EPS, revenue, key metrics",
  "eps_trend_analysis": "Analysis of last 4 quarters beat/miss history and what it implies",
  "supply_chain_exposure": "Which suppliers or customers are exposed, quantified where possible",
  "key_risks": "1-2 specific risks that could surprise to the downside",
  "key_catalysts": "1-2 specific factors that could drive an upside surprise",
  "options_read": "1-2 sentences interpreting the implied move vs historical move. Is the market overpricing or underpricing volatility? What does this signal about positioning?",
  "revision_read": "1 sentence on analyst estimate revision trend: are estimates rising or falling into earnings, and what does it signal about consensus positioning?",
  "watch_list": ["list", "of", "specific", "metrics", "to", "watch", "during", "the", "call"]
}"""


# ── Skills ────────────────────────────────────────────────────────────────────

def skill_get_earnings_calendar(days: int) -> list[dict]:
    """Devuelve tickers con earnings en los próximos N días."""
    if not EARNINGS_FILE.exists():
        return []
    data    = json.loads(EARNINGS_FILE.read_text(encoding="utf-8"))
    hoy     = datetime.date.today()
    limite  = hoy + datetime.timedelta(days=days)
    result  = []
    for e in data.get("earnings", []):
        fecha_s = e.get("proximoEarnings")
        if not fecha_s:
            continue
        fecha = datetime.date.fromisoformat(fecha_s)
        if hoy <= fecha <= limite:
            result.append(e)
    return sorted(result, key=lambda x: x["proximoEarnings"])


def skill_get_eps_history(ticker: str) -> dict:
    """Obtiene historial EPS de los últimos 4 trimestres + estimación actual."""
    try:
        t   = yf.Ticker(ticker)
        hist = t.earnings_history

        quarters = []
        if hist is not None and not hist.empty:
            for dt_idx, row in hist.iterrows():
                actual   = row.get("epsActual")
                estimate = row.get("epsEstimate")
                diff     = row.get("epsDifference")
                surprise = row.get("surprisePercent")
                beat     = bool(actual > estimate) if (actual is not None and estimate is not None) else None
                quarters.append({
                    "quarter":         str(dt_idx)[:10],
                    "epsActual":       round(float(actual),   4) if actual   is not None else None,
                    "epsEstimate":     round(float(estimate), 4) if estimate is not None else None,
                    "epsDifference":   round(float(diff),     4) if diff     is not None else None,
                    "surprisePct":     round(float(surprise) * 100, 2) if surprise is not None else None,
                    "beat":            beat,
                })

        # Estimación próximo trimestre
        cal = t.calendar
        eps_next = None
        eps_low  = None
        eps_high = None
        rev_next = None
        if isinstance(cal, dict):
            eps_next = cal.get("Earnings Average")
            eps_low  = cal.get("Earnings Low")
            eps_high = cal.get("Earnings High")
            rev_next = cal.get("Revenue Average")

        # Tendencia EPS anual
        trend = t.eps_trend
        eps_current_q = None
        if trend is not None and not trend.empty and "0q" in trend.index:
            eps_current_q = float(trend.loc["0q", "current"])

        # Beat streak
        beats = [q["beat"] for q in quarters if q["beat"] is not None]
        streak = 0
        if beats:
            last = beats[-1]
            for b in reversed(beats):
                if b == last:
                    streak += 1
                else:
                    break
            streak = streak if last else -streak  # positivo = beat streak, negativo = miss streak

        return {
            "quarters":    quarters[-4:],  # últimos 4
            "beatStreak":  streak,
            "epsNextQ":    round(eps_next, 4) if eps_next is not None else None,
            "epsLow":      round(eps_low,  4) if eps_low  is not None else None,
            "epsHigh":     round(eps_high, 4) if eps_high is not None else None,
            "revNextQ":    int(rev_next)       if rev_next is not None else None,
            "epsTrendCurrentQ": round(eps_current_q, 4) if eps_current_q is not None else None,
        }
    except Exception as e:
        return {"error": str(e), "quarters": [], "beatStreak": 0}


def skill_get_company_info(ticker: str) -> dict:
    """Info fundamental de la empresa."""
    try:
        info = yf.Ticker(ticker).info
        return {
            "nombre":        info.get("longName") or info.get("shortName", ticker),
            "sector":        info.get("sector", "—"),
            "industria":     info.get("industry", "—"),
            "pais":          info.get("country", "—"),
            "resumen":       (info.get("longBusinessSummary") or "")[:600],
            "empleados":     info.get("fullTimeEmployees"),
            "marketCap":     info.get("marketCap"),
            "revenue_ttm":   info.get("totalRevenue"),
            "margen_bruto":  info.get("grossMargins"),
            "margen_neto":   info.get("profitMargins"),
            "pe_ratio":      info.get("trailingPE"),
            "precio":        info.get("currentPrice") or info.get("regularMarketPrice"),
            "52w_high":      info.get("fiftyTwoWeekHigh"),
            "52w_low":       info.get("fiftyTwoWeekLow"),
        }
    except Exception as e:
        return {"nombre": ticker, "error": str(e)}


def skill_get_supply_chain_exposure(ticker: str, pares: list[dict]) -> dict:
    """Identifica clientes y proveedores del ticker en pares.json."""
    como_cliente   = [p for p in pares if p.get("cliente") == ticker and p.get("proveedor")]
    como_proveedor = [p for p in pares if p.get("proveedor") == ticker and p.get("cliente")]

    return {
        "esCliente":   len(como_cliente) > 0,
        "esProveedor": len(como_proveedor) > 0,
        "proveedores": [
            {"ticker": p["proveedor"], "nombre": p["proveedorNombre"],
             "dependencia": p.get("dependencia"), "lag": p.get("lag")}
            for p in como_cliente
        ],
        "clientes": [
            {"ticker": p["cliente"], "nombre": p["clienteNombre"],
             "dependencia": p.get("dependencia"), "lag": p.get("lag")}
            for p in como_proveedor
        ],
    }


def skill_get_implied_move(ticker: str, earnings_date: str | None = None) -> dict:
    """
    Calcula el movimiento implícito del mercado para la próxima publicación de resultados.

    Metodología profesional:
      1. Obtiene el precio actual del subyacente
      2. Busca el vencimiento de opciones más cercano DESPUÉS de la fecha de earnings
      3. Encuentra el strike ATM (más cercano al precio actual)
      4. Calcula el precio medio del straddle: (call_mid + put_mid)
      5. Implied move % = straddle / stock_price

    El implied move representa lo que el mercado está pagando para cubrirse:
    si el straddle vale $5 y la acción cotiza a $100 → el mercado espera ±5% de movimiento.
    """
    try:
        import pandas as pd
        t = yf.Ticker(ticker)

        # Precio actual
        info        = t.info
        stock_price = (info.get("regularMarketPrice") or info.get("currentPrice")
                       or info.get("previousClose"))
        if not stock_price:
            return {"error": "No price data available", "available": False}

        # Vencimientos disponibles
        expirations = t.options
        if not expirations:
            return {"error": "No options data available", "available": False}

        # Selecciona el vencimiento más cercano DESPUÉS del earnings
        target_expiry = None
        if earnings_date and earnings_date != "—":
            try:
                earn_dt = datetime.date.fromisoformat(earnings_date)
                for exp in expirations:
                    if datetime.date.fromisoformat(exp) >= earn_dt:
                        target_expiry = exp
                        break
            except Exception:
                pass
        if not target_expiry:
            target_expiry = expirations[0]  # fallback: vencimiento más próximo

        # Cadena de opciones
        chain = t.option_chain(target_expiry)
        calls = chain.calls
        puts  = chain.puts

        if calls.empty or puts.empty:
            return {"error": "Empty option chain", "available": False}

        # Strike ATM: el más cercano al precio actual
        atm_strike = float(calls["strike"].iloc[
            (calls["strike"] - stock_price).abs().argsort().iloc[0]
        ])

        # Precio mid = (bid + ask) / 2
        def mid(df, strike):
            row = df[df["strike"] == strike]
            if row.empty:
                return None
            b = float(row["bid"].iloc[0])
            a = float(row["ask"].iloc[0])
            return (b + a) / 2 if (b + a) > 0 else float(row["lastPrice"].iloc[0])

        call_mid = mid(calls, atm_strike)
        put_mid  = mid(puts,  atm_strike)

        if call_mid is None or put_mid is None:
            return {"error": "Cannot price straddle", "available": False}

        straddle         = call_mid + put_mid
        implied_move_pct = round((straddle / stock_price) * 100, 2)

        return {
            "available":        True,
            "stockPrice":       round(stock_price, 2),
            "expiry":           target_expiry,
            "atmStrike":        atm_strike,
            "callMid":          round(call_mid, 2),
            "putMid":           round(put_mid,  2),
            "straddle":         round(straddle, 2),
            "impliedMovePct":   implied_move_pct,
            "label":            f"±{implied_move_pct}%",
        }
    except Exception as e:
        return {"error": str(e), "available": False}


def skill_get_historical_earnings_moves(ticker: str, eps_quarters: list[dict]) -> dict:
    """
    Calcula el movimiento histórico REAL del precio en los días de publicación de resultados.

    Compara el implied move del mercado con lo que la acción HA movido históricamente
    en earnings. Si implied > histórico, las opciones son caras. Si implied < histórico,
    son baratas (oportunidad de volatilidad).

    Retorna:
      - moves: lista de movimientos reales (%)
      - avg_abs_move: media del valor absoluto
      - max_move: el mayor movimiento observado
      - opciones_caras: True si implied > avg_hist (el mercado sobreestima el movimiento)
    """
    try:
        import pandas as pd
        if not eps_quarters:
            return {"available": False, "moves": []}

        t    = yf.Ticker(ticker)
        hist = t.history(period="2y", interval="1d")
        if hist.empty:
            return {"available": False, "moves": []}

        hist.index = hist.index.tz_localize(None) if hist.index.tzinfo else hist.index

        moves = []
        for q in eps_quarters:
            fecha_s = q.get("quarter", "")
            if not fecha_s:
                continue
            try:
                fecha = datetime.datetime.fromisoformat(fecha_s)
                # Busca el precio de cierre del día siguiente al earnings
                loc   = hist.index.searchsorted(fecha)
                if loc + 1 < len(hist):
                    close_before = float(hist["Close"].iloc[loc])
                    close_after  = float(hist["Close"].iloc[loc + 1])
                    move_pct     = round(((close_after - close_before) / close_before) * 100, 2)
                    moves.append({
                        "quarter": fecha_s[:10],
                        "movePct": move_pct,
                        "direction": "up" if move_pct > 0 else "down",
                    })
            except Exception:
                continue

        if not moves:
            return {"available": False, "moves": []}

        abs_moves   = [abs(m["movePct"]) for m in moves]
        avg_abs     = round(sum(abs_moves) / len(abs_moves), 2)
        max_move    = round(max(abs_moves), 2)

        return {
            "available":    True,
            "moves":        moves,
            "avgAbsMove":   avg_abs,
            "maxMove":      max_move,
            "nSamples":     len(moves),
        }
    except Exception as e:
        return {"available": False, "moves": [], "error": str(e)}


def skill_get_news(ticker: str, max_items: int = 6) -> list[dict]:
    """Obtiene noticias recientes de Yahoo Finance para el ticker."""
    try:
        raw = yf.Ticker(ticker).news or []
        noticias = []
        for n in raw[:max_items]:
            c        = n.get("content", {})
            title    = c.get("title") or n.get("title", "")
            provider = c.get("provider", {}).get("displayName") or n.get("publisher", "")
            url      = (c.get("canonicalUrl") or {}).get("url") or n.get("link", "")
            pub_raw  = c.get("pubDate") or ""
            # Normaliza fecha: "2026-06-12T12:39:31Z" → "Jun 12"
            try:
                dt      = datetime.datetime.fromisoformat(pub_raw.replace("Z", "+00:00"))
                fecha_s = dt.strftime("%b %d")
            except Exception:
                fecha_s = ""
            if title and url:
                noticias.append({"title": title, "provider": provider, "url": url, "fecha": fecha_s})
        return noticias
    except Exception:
        return []


def skill_get_estimate_revisions(ticker: str) -> dict:
    """
    Fase 2 — Revisiones de estimaciones de analistas en los últimos 7/30/90 días.

    Un setup alcista clásico: más analistas subiendo estimaciones que bajándolas
    en los 30 días previos a earnings = "positive revision momentum".
    Si además el precio sube con las revisiones = confirmación técnica.

    Fuente: yfinance eps_revisions, earnings_estimate, analyst_price_targets
    """
    try:
        t = yf.Ticker(ticker)

        result = {"available": False, "trend": "unknown"}

        # EPS revisions: cuántos analistas subieron/bajaron en 7/30/90d
        eps_rev = t.eps_revisions
        if eps_rev is not None and not eps_rev.empty:
            row = None
            for idx in ["0q", "0Q"]:
                if idx in eps_rev.index:
                    row = eps_rev.loc[idx]
                    break
            if row is None and len(eps_rev) > 0:
                row = eps_rev.iloc[0]
            if row is not None:
                def safe_int(v):
                    try: return int(v) if v is not None and str(v) != "nan" else 0
                    except: return 0
                result["up7d"]  = safe_int(row.get("upLast7days"))
                result["up30d"] = safe_int(row.get("upLast30days"))
                result["dn30d"] = safe_int(row.get("downLast30days"))
                result["dn90d"] = safe_int(row.get("downLast90days"))

        # Earnings estimate: número de analistas + crecimiento esperado
        est = t.earnings_estimate
        if est is not None and not est.empty:
            row = None
            for idx in ["0q", "0Q"]:
                if idx in est.index:
                    row = est.loc[idx]
                    break
            if row is None and len(est) > 0:
                row = est.iloc[0]
            if row is not None:
                def safe_float(v):
                    try: return round(float(v), 4) if v is not None and str(v) != "nan" else None
                    except: return None
                result["analysts"]  = safe_int(row.get("numberOfAnalysts"))
                result["epsGrowth"] = safe_float(row.get("growth"))

        # Price targets de analistas
        try:
            pt = t.analyst_price_targets
            if isinstance(pt, dict) and pt:
                def sn(v):
                    try: return round(float(v), 2) if v is not None else None
                    except: return None
                result["ptMean"]   = sn(pt.get("mean"))
                result["ptHigh"]   = sn(pt.get("high"))
                result["ptLow"]    = sn(pt.get("low"))
                result["ptMedian"] = sn(pt.get("median"))
        except Exception:
            pass

        # Tendencia: ¿están subiendo o bajando estimaciones?
        up   = result.get("up30d", 0)
        dn   = result.get("dn30d", 0)
        if up == 0 and dn == 0:
            result["trend"] = "unknown"
        elif up > dn * 1.5:
            result["trend"] = "rising"   # bullish setup
        elif dn > up * 1.5:
            result["trend"] = "falling"  # bearish setup
        else:
            result["trend"] = "stable"

        result["available"] = True
        return result

    except Exception as e:
        return {"available": False, "trend": "unknown", "error": str(e)}


def skill_get_latest_8k(ticker: str, days_back: int = 5) -> dict:
    """
    Fase 3 — Detecta y extrae el 8-K más reciente de SEC EDGAR.

    Un 8-K de resultados (Item 2.02) se publica antes o simultáneamente con
    la earnings call. Contiene EPS, revenue y guidance antes de que los analistas
    hayan podido procesarlo. Primera lectura = ventaja informacional.

    Flujo:
      1. CIK del ticker via company_tickers.json
      2. Submissions recientes de la empresa
      3. Detecta 8-K de los últimos N días
      4. Descarga el documento principal y extrae texto plano
      5. Devuelve texto y URL para análisis posterior con Claude
    """
    import re
    import urllib.request

    UA = "SupplySignal research@supplysignal.com"

    def get_url(url: str, timeout: int = 12) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    def strip_html(html: str) -> str:
        html = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<style[^>]*>.*?</style>',  ' ', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<!--.*?-->',                ' ', html, flags=re.DOTALL)
        html = re.sub(r'<[^>]+>',                  ' ', html)
        html = re.sub(r'&nbsp;', ' ', html)
        html = re.sub(r'&amp;',  '&', html)
        html = re.sub(r'&lt;',   '<', html)
        html = re.sub(r'&gt;',   '>', html)
        return re.sub(r'\s+', ' ', html).strip()

    try:
        # 1. CIK lookup
        tickers_json = json.loads(get_url("https://www.sec.gov/files/company_tickers.json"))
        cik = None
        for entry in tickers_json.values():
            if entry.get("ticker", "").upper() == ticker.upper():
                cik = int(entry["cik_str"])
                break
        if not cik:
            return {"available": False, "error": f"CIK not found for {ticker}"}

        cik_padded = f"{cik:010d}"

        # 2. Recent submissions
        subs    = json.loads(get_url(f"https://data.sec.gov/submissions/CIK{cik_padded}.json"))
        recent  = subs.get("filings", {}).get("recent", {})
        forms   = recent.get("form", [])
        dates   = recent.get("filingDate", [])
        accnums = recent.get("accessionNumber", [])
        docs    = recent.get("primaryDocument", [])

        # 3. Busca 8-K de los últimos N días
        cutoff = (datetime.date.today() - datetime.timedelta(days=days_back)).isoformat()
        found  = None
        for i, form in enumerate(forms):
            if form in ("8-K", "8-K/A") and dates[i] >= cutoff:
                found = {
                    "filingDate":      dates[i],
                    "accessionNumber": accnums[i],
                    "primaryDoc":      docs[i] if i < len(docs) else "",
                    "cik":             cik,
                    "cikPadded":       cik_padded,
                }
                break

        if not found:
            return {"available": False, "message": f"No 8-K filed in last {days_back} days for {ticker}"}

        # 4. URL del documento principal
        acc_clean = found["accessionNumber"].replace("-", "")
        doc_url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik}"
            f"/{acc_clean}/{found['primaryDoc']}"
        )
        edgar_url = (
            f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
            f"&CIK={cik}&type=8-K&dateb=&owner=include&count=5"
        )

        # 5. Descarga y extrae texto
        try:
            raw  = get_url(doc_url).decode("utf-8", errors="replace")
            text = strip_html(raw)
            # Limita a los primeros 8000 chars (donde suelen estar los resultados)
            text = text[:8000]
        except Exception as e:
            text = f"[Could not fetch document: {e}]"

        return {
            "available":    True,
            "ticker":       ticker,
            "filingDate":   found["filingDate"],
            "docUrl":       doc_url,
            "edgarUrl":     edgar_url,
            "textExtract":  text,
        }

    except Exception as e:
        return {"available": False, "error": str(e)}


def skill_load_eps_memory() -> dict:
    """Carga memoria de sorpresas EPS acumuladas."""
    if MEMORY_FILE.exists():
        return json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
    return {}


def skill_update_eps_memory(memory: dict, ticker: str, eps_data: dict) -> dict:
    """Actualiza la memoria con los últimos quarters conocidos."""
    if ticker not in memory:
        memory[ticker] = {"quarters": [], "updatedAt": None}
    # Merge quarters nuevos
    existing_dates = {q["quarter"] for q in memory[ticker]["quarters"]}
    for q in eps_data.get("quarters", []):
        if q["quarter"] not in existing_dates:
            memory[ticker]["quarters"].append(q)
    memory[ticker]["quarters"] = sorted(memory[ticker]["quarters"], key=lambda x: x["quarter"])[-8:]
    memory[ticker]["updatedAt"] = datetime.datetime.now().isoformat()
    MEMORY_FILE.write_text(json.dumps(memory, ensure_ascii=False, indent=2), encoding="utf-8")
    return memory


# ── Llamada a Claude ──────────────────────────────────────────────────────────

def generate_briefing_with_claude(ticker: str, earnings_date: str, dias: int,
                                   company_info: dict, eps_data: dict,
                                   supply_chain: dict, memory: dict,
                                   implied_move: dict | None = None,
                                   hist_moves: dict | None = None,
                                   revisions: dict | None = None,
                                   edgar_8k: dict | None = None) -> dict:
    """Llama a Claude API con todos los datos y obtiene el briefing estructurado."""

    client = anthropic.Anthropic()  # usa ANTHROPIC_API_KEY del entorno

    # Formatea memoria histórica
    memoria_hist = memory.get(ticker, {}).get("quarters", [])
    memoria_txt  = ""
    if len(memoria_hist) > 4:
        extras = memoria_hist[:-4]  # los anteriores al último año
        beats_ext = sum(1 for q in extras if q.get("beat") is True)
        memoria_txt = f"\nHistorical memory (quarters before last 4): {len(extras)} quarters, {beats_ext} beats ({100*beats_ext//len(extras)}% beat rate)."

    # Construye el contexto completo para el LLM
    contexto = f"""
TICKER: {ticker}
EARNINGS DATE: {earnings_date} (in {dias} days)
COMPANY: {company_info.get('nombre', ticker)}
SECTOR: {company_info.get('sector')} / {company_info.get('industria')}
COUNTRY: {company_info.get('pais')}
MARKET CAP: ${company_info.get('marketCap', 0)/1e9:.1f}B
CURRENT PRICE: ${company_info.get('precio', '—')}
52W RANGE: ${company_info.get('52w_low', '—')} – ${company_info.get('52w_high', '—')}
GROSS MARGIN: {(company_info.get('margen_bruto') or 0)*100:.1f}%
NET MARGIN: {(company_info.get('margen_neto') or 0)*100:.1f}%
P/E RATIO: {company_info.get('pe_ratio', '—')}

BUSINESS SUMMARY:
{company_info.get('resumen', 'Not available')}

CONSENSUS ESTIMATES FOR NEXT QUARTER:
- EPS estimate: ${eps_data.get('epsNextQ', '—')}
- EPS range: ${eps_data.get('epsLow', '—')} – ${eps_data.get('epsHigh', '—')}
- Revenue estimate: ${(eps_data.get('revNextQ') or 0)/1e9:.2f}B

LAST 4 QUARTERS EPS HISTORY (actual vs estimate):
"""
    for q in eps_data.get("quarters", []):
        beat_s = "BEAT" if q.get("beat") else ("MISS" if q.get("beat") is False else "—")
        contexto += f"  {q['quarter']}: actual ${q['epsActual']} vs est ${q['epsEstimate']} → {beat_s} ({q.get('surprisePct', '—')}%)\n"

    streak = eps_data.get("beatStreak", 0)
    if streak > 1:
        contexto += f"\nBEAT STREAK: {streak} consecutive beats\n"
    elif streak < -1:
        contexto += f"\nMISS STREAK: {abs(streak)} consecutive misses\n"
    contexto += memoria_txt

    contexto += f"""
SUPPLY CHAIN EXPOSURE:
- Role: {'Customer (has suppliers)' if supply_chain['esCliente'] else ''} {'Supplier (has customers)' if supply_chain['esProveedor'] else ''}
"""
    if supply_chain["proveedores"]:
        contexto += "Key suppliers:\n"
        for p in supply_chain["proveedores"]:
            contexto += f"  - {p['ticker']} ({p['nombre']}): {p['dependencia']}% revenue dependency, ~{p['lag']} day lag\n"
    if supply_chain["clientes"]:
        contexto += "Key customers:\n"
        for c in supply_chain["clientes"]:
            contexto += f"  - {c['ticker']} ({c['nombre']}): {c['dependencia']}% of supplier revenue, ~{c['lag']} day lag\n"

    # Estimate revisions (Phase 2)
    if revisions and revisions.get("available"):
        rv = revisions
        trend_str = {"rising": "RISING ↑ (bullish setup)", "falling": "FALLING ↓ (bearish setup)", "stable": "STABLE →"}.get(rv.get("trend", ""), "unknown")
        contexto += f"""
ANALYST ESTIMATE REVISIONS (last 30 days):
- Trend: {trend_str}
- Estimates raised (up 30d): {rv.get('up30d', '—')} analysts
- Estimates cut (dn 30d): {rv.get('dn30d', '—')} analysts
- Estimates raised (up 7d): {rv.get('up7d', '—')} analysts
- Total analysts covering: {rv.get('analysts', '—')}
- EPS growth expected vs YoY: {(rv.get('epsGrowth') or 0)*100:.1f}%
"""
        if rv.get("ptMean"):
            contexto += f"- Analyst price target: mean ${rv['ptMean']} (range ${rv.get('ptLow','—')} – ${rv.get('ptHigh','—')})\n"
    else:
        contexto += "\nANALYST REVISIONS: Not available.\n"

    # 8-K filing (Phase 3) — si ya hay resultados publicados
    if edgar_8k and edgar_8k.get("available") and edgar_8k.get("textExtract"):
        contexto += f"""
SEC EDGAR 8-K FILING (filed {edgar_8k['filingDate']}):
— Results already published. Extract of filing text:
{edgar_8k['textExtract'][:3000]}
[...truncated]

NOTE: The above is the actual earnings release. Use it to update market_expectations
with reported figures vs consensus, and note any guidance provided.
"""

    # Implied move (options market)
    if implied_move and implied_move.get("available"):
        im = implied_move
        contexto += f"""
OPTIONS MARKET — IMPLIED MOVE:
- ATM straddle price: ${im['straddle']} (call ${im['callMid']} + put ${im['putMid']})
- Stock price: ${im['stockPrice']}  |  ATM strike: ${im['atmStrike']}
- Implied move for earnings: {im['label']}  (expiry: {im['expiry']})
"""
    else:
        contexto += "\nOPTIONS MARKET: No options data available.\n"

    # Historical earnings moves
    if hist_moves and hist_moves.get("available"):
        hm = hist_moves
        contexto += f"""
HISTORICAL EARNINGS MOVES (last {hm['nSamples']} quarters):
- Average absolute move: ±{hm['avgAbsMove']}%
- Max observed move: ±{hm['maxMove']}%
- Individual moves: {', '.join(f"{m['movePct']:+.1f}%" for m in hm['moves'][-4:])}
"""
        if implied_move and implied_move.get("available"):
            delta = round(implied_move["impliedMovePct"] - hm["avgAbsMove"], 2)
            if delta > 1.5:
                contexto += f"→ Options are EXPENSIVE: market is pricing {delta:.1f}pp MORE than the historical average move.\n"
            elif delta < -1.5:
                contexto += f"→ Options are CHEAP: market is pricing {abs(delta):.1f}pp LESS than the historical average move.\n"
            else:
                contexto += f"→ Options are FAIRLY PRICED relative to historical earnings volatility.\n"
    else:
        contexto += "\nHISTORICAL MOVES: Insufficient data.\n"

    contexto += "\nGenerate the pre-earnings briefing as JSON per the format specified."

    # Llamada a Claude
    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": contexto}],
    )

    # Extrae JSON de la respuesta
    texto = message.content[0].text.strip()
    if "```json" in texto:
        texto = texto.split("```json")[1].split("```")[0].strip()
    elif "```" in texto:
        texto = texto.split("```")[1].split("```")[0].strip()

    # Intento 1: parse directo
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        pass

    # Intento 2: pide a Claude que corrija el JSON
    fix_msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1200,
        messages=[
            {"role": "user", "content": f"Fix this JSON so it is valid. Return only the corrected JSON, no explanation:\n\n{texto}"}
        ],
    )
    texto2 = fix_msg.content[0].text.strip()
    if "```json" in texto2:
        texto2 = texto2.split("```json")[1].split("```")[0].strip()
    elif "```" in texto2:
        texto2 = texto2.split("```")[1].split("```")[0].strip()
    return json.loads(texto2)


# ── Pipeline principal ────────────────────────────────────────────────────────

def procesar_ticker(ticker: str, earnings_date: str, dias: int,
                    pares: list[dict], memory: dict) -> dict | None:
    print(f"\n  [{ticker}] {earnings_date} (en {dias}d)")

    print(f"    · Fetching company info...", end=" ", flush=True)
    company_info = skill_get_company_info(ticker)
    print("OK")

    print(f"    · Fetching EPS history...", end=" ", flush=True)
    eps_data = skill_get_eps_history(ticker)
    print(f"OK — {len(eps_data.get('quarters',[]))} quarters, streak={eps_data.get('beatStreak',0)}")

    print(f"    · Supply chain exposure...", end=" ", flush=True)
    supply_chain = skill_get_supply_chain_exposure(ticker, pares)
    n_rel = len(supply_chain["proveedores"]) + len(supply_chain["clientes"])
    print(f"OK — {n_rel} relationships")

    print(f"    · Fetching news...", end=" ", flush=True)
    noticias = skill_get_news(ticker)
    print(f"OK — {len(noticias)} articles")

    print(f"    · Estimate revisions...", end=" ", flush=True)
    revisions = skill_get_estimate_revisions(ticker)
    if revisions.get("available"):
        trend = revisions.get("trend", "unknown")
        up, dn = revisions.get("up30d", 0), revisions.get("dn30d", 0)
        print(f"OK — {trend.upper()} ({up}↑ / {dn}↓ in 30d)")
    else:
        print(f"N/A — {revisions.get('error','')}")

    print(f"    · SEC EDGAR 8-K check...", end=" ", flush=True)
    edgar_8k = skill_get_latest_8k(ticker, days_back=3)
    if edgar_8k.get("available"):
        print(f"OK — 8-K filed {edgar_8k['filingDate']} (results already out!)")
    else:
        print(f"None — {edgar_8k.get('message', edgar_8k.get('error',''))}")

    print(f"    · Options implied move...", end=" ", flush=True)
    implied_move = skill_get_implied_move(ticker, earnings_date)
    if implied_move.get("available"):
        print(f"OK — straddle {implied_move['label']} (exp {implied_move['expiry']})")
    else:
        print(f"N/A — {implied_move.get('error', 'no data')}")

    print(f"    · Historical earnings moves...", end=" ", flush=True)
    hist_moves = skill_get_historical_earnings_moves(ticker, eps_data.get("quarters", []))
    if hist_moves.get("available"):
        print(f"OK — avg ±{hist_moves['avgAbsMove']}% ({hist_moves['nSamples']} quarters)")
    else:
        print("N/A")

    # Diagnóstico implied vs histórico
    if implied_move.get("available") and hist_moves.get("available"):
        delta = implied_move["impliedMovePct"] - hist_moves["avgAbsMove"]
        if delta > 1.5:
            print(f"    ⚠  Options EXPENSIVE: implied {implied_move['impliedMovePct']}% vs hist avg {hist_moves['avgAbsMove']}%")
        elif delta < -1.5:
            print(f"    ✓  Options CHEAP: implied {implied_move['impliedMovePct']}% vs hist avg {hist_moves['avgAbsMove']}%")
        else:
            print(f"    ·  Options fairly priced ({implied_move['impliedMovePct']}% implied vs {hist_moves['avgAbsMove']}% hist)")

    # Actualiza memoria
    memory = skill_update_eps_memory(memory, ticker, eps_data)

    print(f"    · Calling Claude ({CLAUDE_MODEL})...", end=" ", flush=True)
    try:
        briefing = generate_briefing_with_claude(
            ticker, earnings_date, dias,
            company_info, eps_data, supply_chain, memory,
            implied_move, hist_moves, revisions, edgar_8k,
        )
        print("OK")
    except Exception as e:
        print(f"ERROR: {e}")
        return None

    return {
        "ticker":        ticker,
        "nombre":        company_info.get("nombre", ticker),
        "earningsDate":  earnings_date,
        "diasRestantes": dias,
        "epsNextQ":      eps_data.get("epsNextQ"),
        "epsLow":        eps_data.get("epsLow"),
        "epsHigh":       eps_data.get("epsHigh"),
        "revNextQ":      eps_data.get("revNextQ"),
        "beatStreak":    eps_data.get("beatStreak", 0),
        "quarters":      eps_data.get("quarters", []),
        "supplyChain":   supply_chain,
        "noticias":      noticias,
        "impliedMove":   implied_move,
        "histMoves":     hist_moves,
        "revisions":     revisions,
        "edgar8k":       {"available": edgar_8k.get("available"), "filingDate": edgar_8k.get("filingDate"), "edgarUrl": edgar_8k.get("edgarUrl")} if edgar_8k else None,
        "briefing":      briefing,
        "generadoEn":    datetime.datetime.now().isoformat(),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days",   type=int, default=7,  help="Ventana de días hacia adelante")
    parser.add_argument("--ticker", type=str, default=None, help="Forzar un ticker específico")
    args = parser.parse_args()

    print("\nEarnings Briefing Agent")
    print("=" * 50)

    # Carga pares
    pares = json.loads(PARES_FILE.read_text(encoding="utf-8"))["pares"]

    # Tickers a procesar
    if args.ticker:
        ticker_upper = args.ticker.upper()
        # Busca en earnings.json o usa fecha placeholder
        if EARNINGS_FILE.exists():
            earnings_raw = json.loads(EARNINGS_FILE.read_text(encoding="utf-8"))
            match = next((e for e in earnings_raw.get("earnings", []) if e["ticker"] == ticker_upper), None)
        else:
            match = None
        if match:
            pendientes = [match]
        else:
            pendientes = [{"ticker": ticker_upper, "proximoEarnings": "—", "diasRestantes": 0}]
    else:
        pendientes = skill_get_earnings_calendar(args.days)

    if not pendientes:
        print(f"No hay earnings en los próximos {args.days} días.")
        print("Usa --days N para ampliar la ventana o --ticker XXXX para forzar.")
        return

    print(f"{len(pendientes)} empresa(s) con earnings en los próximos {args.days} días:\n")
    for e in pendientes:
        print(f"  {e['ticker']:8s}  {e.get('proximoEarnings','—')}  ({e.get('diasRestantes','?')}d)")

    # Carga memoria
    memory = skill_load_eps_memory()

    # Carga briefings existentes para no regenrar los que ya están frescos
    briefings_existentes = {}
    if OUT_FILE.exists():
        try:
            prev = json.loads(OUT_FILE.read_text(encoding="utf-8"))
            for b in prev.get("briefings", []):
                briefings_existentes[b["ticker"]] = b
        except Exception:
            pass

    # Procesa cada ticker
    resultados = []
    for e in pendientes:
        ticker   = e["ticker"]
        fecha    = e.get("proximoEarnings", "—")
        dias     = e.get("diasRestantes") or 0

        # Skip si ya tenemos un briefing generado hoy
        existing = briefings_existentes.get(ticker)
        if existing:
            gen_dt = existing.get("generadoEn", "")
            if gen_dt.startswith(datetime.date.today().isoformat()):
                print(f"\n  [{ticker}] Ya tiene briefing de hoy, reutilizando.")
                resultados.append(existing)
                continue

        result = procesar_ticker(ticker, fecha, dias, pares, memory)
        if result:
            resultados.append(result)
            briefings_existentes[ticker] = result

    # Fusiona con briefings existentes (de otros días aún relevantes)
    for ticker, b in briefings_existentes.items():
        if not any(r["ticker"] == ticker for r in resultados):
            # Mantener si el earnings aún no ha pasado
            fecha_s = b.get("earningsDate", "")
            if fecha_s and fecha_s >= datetime.date.today().isoformat():
                resultados.append(b)

    resultados.sort(key=lambda x: x.get("earningsDate", ""))

    salida = {
        "actualizadoEn": datetime.datetime.now().isoformat(),
        "ventanaDias":   args.days,
        "total":         len(resultados),
        "briefings":     resultados,
    }
    OUT_FILE.write_text(json.dumps(salida, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'='*50}")
    print(f"Briefings generados: {len(resultados)}")
    print(f"Guardado en: {OUT_FILE.resolve()}")


if __name__ == "__main__":
    main()
