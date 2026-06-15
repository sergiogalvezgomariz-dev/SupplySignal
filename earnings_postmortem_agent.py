"""
earnings_postmortem_agent.py  —  Post-earnings analysis for SupplySignal.

Corre DESPUÉS de que una empresa publica resultados.
Para cada ticker en briefings.json donde earningsDate <= hoy:

  1. Calcula el movimiento real del precio (T vs T+1 tras earnings)
  2. Compara con el implied move que tenía el mercado pre-earnings
  3. Detecta si la empresa batió o decepcionó al mercado (vs implied, no vs consensus)
  4. Intenta leer EPS reportado de yfinance earnings_history
  5. Genera señales de supply chain: lag trade para proveedores expuestos
  6. Claude escribe un postmortem de 2-3 párrafos con lectura institucional
  7. Guarda output/postmortem.json

Uso:
    python earnings_postmortem_agent.py              # todos los earnings pasados <7d
    python earnings_postmortem_agent.py --ticker MU  # forzar un ticker
    python earnings_postmortem_agent.py --days 14    # ventana más amplia
"""

import sys, json, os, datetime, argparse
from pathlib import Path

import yfinance as yf
import anthropic

# ── .env ──────────────────────────────────────────────────────────────────────
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8-sig").splitlines():
        if "=" in _line and not _line.startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# ── Config ────────────────────────────────────────────────────────────────────
PARES_FILE      = Path("pares.json")
BRIEFINGS_FILE  = Path("output/briefings.json")
OUT_FILE        = Path("output/postmortem.json")
CLAUDE_MODEL    = "claude-sonnet-4-6"
Path("output").mkdir(exist_ok=True)

POSTMORTEM_PROMPT = """You are a senior equity research analyst writing post-earnings notes.

You receive:
- Pre-earnings briefing (what the market expected)
- Actual price move vs options-implied move
- EPS reported vs consensus (if available)
- Supply chain exposure data

Write a concise post-earnings note covering:
1. Did the stock beat or disappoint the OPTIONS market (not just consensus)?
   This is the key distinction: a company can beat EPS consensus but still fall
   if it missed the "whisper" number priced into options.
2. What does this move signal for suppliers in the supply chain?
3. Is the lag trade still valid given the magnitude of the move?

Rules:
- Never recommend buying or selling
- Be specific with numbers from the data provided
- If EPS data is missing, focus on the price action and implied move comparison
- Supply chain read-through is mandatory if relationships exist

Format as JSON:
{
  "verdict": "BEAT_MARKET | MISSED_MARKET | IN_LINE",
  "headline": "One sentence post-earnings summary",
  "price_action_read": "2-3 sentences on actual vs implied move and what it signals",
  "eps_read": "1-2 sentences on reported EPS vs consensus, if data available",
  "supply_chain_signal": "2-3 sentences on implications for suppliers/customers and lag trade validity",
  "positioning_note": "1-2 sentences on what institutional investors likely do next"
}"""


# ── Skills ────────────────────────────────────────────────────────────────────

def get_actual_move(ticker: str, earnings_date: str) -> dict:
    """
    Calcula el movimiento real de precio tras earnings.

    Earnings después de cierre → comparamos cierre del día de earnings vs
    cierre del día siguiente (T+1). Esto es lo que los traders llaman
    "the earnings gap": cuánto abrió la acción al día siguiente.
    """
    try:
        t    = yf.Ticker(ticker)
        # Pedimos 5 días desde la fecha de earnings para tener margen
        hist = t.history(
            start=earnings_date,
            end=(datetime.date.fromisoformat(earnings_date) + datetime.timedelta(days=7)).isoformat(),
            interval="1d",
        )
        if hist.empty or len(hist) < 2:
            return {"available": False, "error": "Not enough price data yet"}

        hist.index = hist.index.tz_localize(None) if hist.index.tzinfo else hist.index

        close_t0 = round(float(hist["Close"].iloc[0]), 2)
        close_t1 = round(float(hist["Close"].iloc[1]), 2)
        move_pct  = round(((close_t1 - close_t0) / close_t0) * 100, 2)

        # Open del T+1 (el gap de apertura, lo más relevante)
        open_t1   = round(float(hist["Open"].iloc[1]), 2) if "Open" in hist.columns else None
        gap_pct   = round(((open_t1 - close_t0) / close_t0) * 100, 2) if open_t1 else None

        return {
            "available":   True,
            "earningsDate":earnings_date,
            "closeT0":     close_t0,
            "closeT1":     close_t1,
            "openT1":      open_t1,
            "movePct":     move_pct,       # close-to-close
            "gapPct":      gap_pct,        # overnight gap (más relevante para earnings)
            "direction":   "up" if move_pct > 0 else "down",
        }
    except Exception as e:
        return {"available": False, "error": str(e)}


def get_reported_eps(ticker: str, earnings_date: str) -> dict:
    """
    Intenta leer el EPS reportado de yfinance earnings_history.
    yfinance se actualiza pocas horas después del earnings call.
    """
    try:
        t    = yf.Ticker(ticker)
        hist = t.earnings_history
        if hist is None or hist.empty:
            return {"available": False}

        # Busca el trimestre más cercano a la earnings_date
        earn_dt = datetime.date.fromisoformat(earnings_date)
        best    = None
        best_delta = 999
        for idx, row in hist.iterrows():
            try:
                row_dt = datetime.date.fromisoformat(str(idx)[:10])
                delta  = abs((row_dt - earn_dt).days)
                if delta < best_delta:
                    best_delta = delta
                    best = {"date": str(idx)[:10], "row": row, "delta": delta}
            except Exception:
                continue

        if not best or best["delta"] > 10:
            return {"available": False, "message": "No matching quarter in earnings history"}

        row = best["row"]
        def sf(v):
            try: return round(float(v), 4)
            except: return None

        actual   = sf(row.get("epsActual"))
        estimate = sf(row.get("epsEstimate"))
        surprise = sf(row.get("surprisePercent"))
        if surprise is not None:
            surprise = round(surprise * 100, 2)

        return {
            "available":    True,
            "quarter":      best["date"],
            "epsActual":    actual,
            "epsEstimate":  estimate,
            "surprise":     surprise,  # %
            "beat":         (actual > estimate) if (actual and estimate) else None,
        }
    except Exception as e:
        return {"available": False, "error": str(e)}


def generate_supply_chain_signals(ticker: str, actual_move: dict, pares: list) -> list:
    """
    Genera señales de lag trade para los proveedores/clientes expuestos.

    Lógica: si el cliente (ticker) se mueve significativamente en earnings,
    los proveedores con alta dependencia reaccionarán con lag de N días.
    Umbral: |move| > 3% (el mismo que usa SupplySignal para shocks diarios).
    """
    signals = []
    move = actual_move.get("movePct") or 0
    if abs(move) < 2:
        return []  # movimiento insignificante, no genera señal

    direction = "COMPRA" if move > 0 else "VENTA"

    # Proveedores expuestos al cliente (ticker)
    for p in pares:
        if p.get("cliente") != ticker or not p.get("proveedor"):
            continue
        score = round(abs(move) * p.get("dependencia", 0) / 10, 1)
        signals.append({
            "tipo":             "post_earnings_lag",
            "accion":           direction,
            "proveedor":        p["proveedor"],
            "proveedorNombre":  p["proveedorNombre"],
            "cliente":          ticker,
            "triggerMove":      move,
            "dependencia":      p.get("dependencia"),
            "lagDays":          p.get("lag"),
            "puntuacion":       score,
            "generadoEn":       datetime.datetime.now().isoformat(),
            "validoHasta":      (datetime.date.today() + datetime.timedelta(days=10)).isoformat(),
        })

    signals.sort(key=lambda x: x["puntuacion"], reverse=True)
    return signals


def generate_postmortem_with_claude(ticker: str, briefing_data: dict,
                                     actual_move: dict, reported_eps: dict,
                                     supply_signals: list) -> dict:
    client = anthropic.Anthropic()

    im = briefing_data.get("impliedMove") or {}
    hm = briefing_data.get("histMoves")   or {}
    bf = briefing_data.get("briefing")    or {}
    sc = briefing_data.get("supplyChain") or {}

    # Veredicto automático (Claude puede refinarlo)
    move      = actual_move.get("movePct", 0) or 0
    implied   = im.get("impliedMovePct") or 0
    if implied > 0:
        if abs(move) > implied * 1.1:
            auto_verdict = "BEAT_MARKET"
        elif abs(move) < implied * 0.8:
            auto_verdict = "MISSED_MARKET"
        else:
            auto_verdict = "IN_LINE"
    else:
        auto_verdict = "BEAT_MARKET" if abs(move) > 3 else "IN_LINE"

    ctx = f"""
TICKER: {ticker}
COMPANY: {briefing_data.get('nombre', ticker)}
EARNINGS DATE: {briefing_data.get('earningsDate')}

PRE-EARNINGS BRIEFING HEADLINE:
"{bf.get('headline', '—')}"

ACTUAL PRICE ACTION:
- Move (close-to-close T to T+1): {actual_move.get('movePct', '—'):+.2f}% {actual_move.get('direction','').upper()}
- Opening gap T+1: {actual_move.get('gapPct', '—'):+.2f}% (if available)
- Options-implied move (pre-earnings): ±{implied:.1f}%
- Historical avg earnings move: ±{hm.get('avgAbsMove','—')}%
- Preliminary verdict: {auto_verdict}

"""
    if reported_eps.get("available"):
        e = reported_eps
        ctx += f"""REPORTED EPS:
- Actual: ${e.get('epsActual','—')}  vs  Estimate: ${e.get('epsEstimate','—')}
- Surprise: {e.get('surprise','—')}%  →  {'BEAT' if e.get('beat') else 'MISS' if e.get('beat') is False else '—'}
"""
    else:
        ctx += "REPORTED EPS: Not yet available in database.\n"

    if supply_signals:
        ctx += f"\nSUPPLY CHAIN SIGNALS GENERATED ({len(supply_signals)}):\n"
        for s in supply_signals[:4]:
            ctx += f"  · {s['accion']} {s['proveedor']} ({s['proveedorNombre']}): {s['dependencia']}% dep, lag {s['lagDays']}d, score {s['puntuacion']}\n"

    if sc.get("proveedores"):
        ctx += "\nKEY SUPPLIERS IN SUPPLY CHAIN:\n"
        for p in sc["proveedores"][:3]:
            ctx += f"  · {p['ticker']} ({p['nombre']}): {p['dependencia']}% revenue, {p['lag']}d lag\n"

    ctx += "\nGenerate the post-earnings note as JSON per the format specified."

    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=900,
        system=POSTMORTEM_PROMPT,
        messages=[{"role": "user", "content": ctx}],
    )

    texto = msg.content[0].text.strip()
    for delim in ("```json", "```"):
        if delim in texto:
            texto = texto.split(delim)[1].split("```")[0].strip()
            break

    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        # Fix con segundo llamado
        fix = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1000,
            messages=[{"role": "user", "content": f"Fix this JSON:\n\n{texto}"}],
        )
        t2 = fix.content[0].text.strip()
        for delim in ("```json", "```"):
            if delim in t2:
                t2 = t2.split(delim)[1].split("```")[0].strip()
                break
        return json.loads(t2)


# ── Pipeline ──────────────────────────────────────────────────────────────────

def procesar_postmortem(briefing_data: dict, pares: list) -> dict | None:
    ticker       = briefing_data["ticker"]
    earnings_date = briefing_data.get("earningsDate", "")

    print(f"\n  [{ticker}] Earnings: {earnings_date}")

    print(f"    · Actual price move...", end=" ", flush=True)
    actual_move = get_actual_move(ticker, earnings_date)
    if actual_move.get("available"):
        print(f"OK — {actual_move['movePct']:+.2f}% (gap {actual_move.get('gapPct','—'):+}%)")
    else:
        print(f"N/A — {actual_move.get('error','')}")
        return None

    print(f"    · Reported EPS...", end=" ", flush=True)
    reported_eps = get_reported_eps(ticker, earnings_date)
    if reported_eps.get("available"):
        beat = "BEAT" if reported_eps.get("beat") else "MISS" if reported_eps.get("beat") is False else "—"
        print(f"OK — ${reported_eps.get('epsActual','—')} vs ${reported_eps.get('epsEstimate','—')} ({beat})")
    else:
        print(f"N/A — {reported_eps.get('message', reported_eps.get('error',''))}")

    print(f"    · Supply chain signals...", end=" ", flush=True)
    signals = generate_supply_chain_signals(ticker, actual_move, pares)
    print(f"{len(signals)} signal(s) generated")

    print(f"    · Claude postmortem...", end=" ", flush=True)
    try:
        analysis = generate_postmortem_with_claude(
            ticker, briefing_data, actual_move, reported_eps, signals
        )
        print(f"OK — {analysis.get('verdict','—')}")
    except Exception as e:
        print(f"ERROR: {e}")
        return None

    # Compara actual vs implied
    im      = briefing_data.get("impliedMove") or {}
    implied = im.get("impliedMovePct") or 0
    move    = actual_move.get("movePct", 0) or 0

    return {
        "ticker":        ticker,
        "nombre":        briefing_data.get("nombre", ticker),
        "earningsDate":  earnings_date,
        "actualMove":    actual_move,
        "reportedEps":   reported_eps,
        "impliedMove":   im,
        "beatImplied":   abs(move) > implied if implied else None,
        "deltaVsImplied":round(abs(move) - implied, 2) if implied else None,
        "supplySignals": signals,
        "analysis":      analysis,
        "processedAt":   datetime.datetime.now().isoformat(),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days",   type=int, default=7,   help="Días atrás a procesar")
    parser.add_argument("--ticker", type=str, default=None, help="Forzar ticker")
    args = parser.parse_args()

    print("\nEarnings Postmortem Agent")
    print("=" * 50)

    if not BRIEFINGS_FILE.exists():
        print("No briefings.json found. Run earnings_briefing_agent.py first.")
        return

    pares = json.loads(PARES_FILE.read_text(encoding="utf-8"))["pares"]
    all_briefings = json.loads(BRIEFINGS_FILE.read_text(encoding="utf-8")).get("briefings", [])

    hoy    = datetime.date.today()
    cutoff = (hoy - datetime.timedelta(days=args.days)).isoformat()

    if args.ticker:
        pendientes = [b for b in all_briefings if b["ticker"] == args.ticker.upper()]
    else:
        # Solo earnings que ya pasaron (earningsDate < hoy) y son recientes
        pendientes = [
            b for b in all_briefings
            if b.get("earningsDate", "9999") < hoy.isoformat()
            and b.get("earningsDate", "0000") >= cutoff
        ]

    if not pendientes:
        print(f"No earnings to process in the last {args.days} days.")
        print("Use --ticker XXXX to force a specific ticker or --days N to expand window.")
        return

    print(f"{len(pendientes)} ticker(s) to process:\n")
    for b in pendientes:
        print(f"  {b['ticker']:8s}  earnings: {b.get('earningsDate','—')}")

    # Carga postmortem existentes
    existing = {}
    if OUT_FILE.exists():
        try:
            prev = json.loads(OUT_FILE.read_text(encoding="utf-8"))
            for p in prev.get("postmortems", []):
                existing[p["ticker"]] = p
        except Exception:
            pass

    resultados = []
    for b in pendientes:
        ticker = b["ticker"]
        # Skip si ya procesado hoy
        ex = existing.get(ticker)
        if ex and ex.get("processedAt", "").startswith(hoy.isoformat()):
            print(f"\n  [{ticker}] Already processed today, reusing.")
            resultados.append(ex)
            continue

        result = procesar_postmortem(b, pares)
        if result:
            resultados.append(result)
            existing[ticker] = result

    # Merge con existentes relevantes
    for ticker, pm in existing.items():
        if not any(r["ticker"] == ticker for r in resultados):
            if pm.get("earningsDate", "0") >= cutoff:
                resultados.append(pm)

    resultados.sort(key=lambda x: x.get("earningsDate", ""), reverse=True)

    salida = {
        "actualizadoEn": datetime.datetime.now().isoformat(),
        "total":         len(resultados),
        "postmortems":   resultados,
    }
    OUT_FILE.write_text(json.dumps(salida, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'='*50}")
    print(f"Postmortems: {len(resultados)}")
    print(f"Guardado en: {OUT_FILE.resolve()}")

    # Muestra señales de supply chain generadas
    all_signals = [s for r in resultados for s in r.get("supplySignals", [])]
    if all_signals:
        print(f"\nSupply chain signals generadas ({len(all_signals)}):")
        for s in sorted(all_signals, key=lambda x: x["puntuacion"], reverse=True)[:8]:
            print(f"  {s['accion']:6s} {s['proveedor']:6s} ({s['proveedorNombre']}) "
                  f"— dep {s['dependencia']}% lag {s['lagDays']}d score {s['puntuacion']}")


if __name__ == "__main__":
    main()
