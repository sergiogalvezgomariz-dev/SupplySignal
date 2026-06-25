"""
pregenerate_intel.py  —  Pre-genera la caché de Company Intel para todos los tickers.

Lee pares.json, extrae todos los tickers únicos y llama a company_intel_agent
para cada uno. El resultado se guarda en output/company_intel/{TICKER}.json
con TTL de 24h.

Diseñado para correr como cron nocturno (ej. 06:00 antes de la apertura del mercado):

  Windows Task Scheduler:
    python C:\ruta\pregenerate_intel.py

  Linux/Mac cron (06:00 UTC):
    0 6 * * * cd /ruta && python pregenerate_intel.py >> logs/intel_cron.log 2>&1

  Vercel Cron (vía endpoint /api/cron/warmup):
    Se llama automáticamente — ver vercel.json
"""

import json, datetime, time, sys
from pathlib import Path
from company_intel_agent import build_company_intel, INTEL_DIR, CACHE_HOURS

PARES_FILE = Path("pares.json")

def tickers_from_pares() -> list[str]:
    pares = json.loads(PARES_FILE.read_text(encoding="utf-8"))["pares"]
    tickers = set()
    for p in pares:
        if p.get("cliente"):   tickers.add(p["cliente"])
        if p.get("proveedor"): tickers.add(p["proveedor"])
    return sorted(tickers)

def cache_is_fresh(ticker: str) -> bool:
    f = INTEL_DIR / f"{ticker}.json"
    if not f.exists():
        return False
    try:
        data    = json.loads(f.read_text(encoding="utf-8"))
        expires = data.get("cacheExpires", "")
        return bool(expires and datetime.datetime.fromisoformat(expires) > datetime.datetime.now())
    except Exception:
        return False

def main():
    force = "--force" in sys.argv
    tickers = tickers_from_pares()

    print(f"\n{'='*52}")
    print(f"SupplySignal — Company Intel Pre-generation")
    print(f"Started: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Tickers: {len(tickers)}  |  Force refresh: {force}")
    print(f"{'='*52}\n")

    ok = skipped = errors = 0

    for i, ticker in enumerate(tickers, 1):
        prefix = f"[{i:2d}/{len(tickers)}] {ticker:6s}"

        if not force and cache_is_fresh(ticker):
            print(f"{prefix} — cache fresh, skipping")
            skipped += 1
            continue

        try:
            build_company_intel(ticker)
            ok += 1
        except Exception as e:
            print(f"{prefix} — ERROR: {e}")
            errors += 1

        # Pausa entre tickers para no saturar Yahoo Finance
        if i < len(tickers):
            time.sleep(1.5)

    print(f"\n{'='*52}")
    print(f"Done: {ok} OK · {skipped} skipped (fresh) · {errors} errors")
    print(f"Finished: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*52}\n")

if __name__ == "__main__":
    main()
