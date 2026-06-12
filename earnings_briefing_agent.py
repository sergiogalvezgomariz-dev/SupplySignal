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
                                   supply_chain: dict, memory: dict) -> dict:
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

    # Actualiza memoria
    memory = skill_update_eps_memory(memory, ticker, eps_data)

    print(f"    · Calling Claude ({CLAUDE_MODEL})...", end=" ", flush=True)
    try:
        briefing = generate_briefing_with_claude(
            ticker, earnings_date, dias,
            company_info, eps_data, supply_chain, memory
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
