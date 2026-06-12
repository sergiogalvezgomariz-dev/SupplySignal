"""
earnings_bot.py  —  Obtiene el próximo earnings call de todas las compañías seguidas.

Lee los tickers de pares.json, consulta Yahoo Finance y guarda
output/earnings.json con fechas, estimaciones de EPS/revenue y días restantes.

Uso:
    python earnings_bot.py          → actualiza output/earnings.json
    python earnings_bot.py --print  → muestra tabla en consola
"""

import sys
import json
import datetime
from pathlib import Path

import yfinance as yf

PARES_FILE = Path("pares.json")
OUT_FILE   = Path("output/earnings.json")
OUT_FILE.parent.mkdir(exist_ok=True)

# ── Extrae todos los tickers únicos de pares.json ─────────────────────────────

def get_tickers() -> list[str]:
    pares = json.loads(PARES_FILE.read_text(encoding="utf-8"))["pares"]
    tickers = set()
    for p in pares:
        if p.get("cliente"):  tickers.add(p["cliente"])
        if p.get("proveedor"): tickers.add(p["proveedor"])
    return sorted(tickers)


# ── Consulta Yahoo Finance para un ticker ────────────────────────────────────

def get_earnings_data(ticker: str) -> dict:
    try:
        t    = yf.Ticker(ticker)
        info = t.info
        cal  = t.calendar

        nombre = info.get("longName") or info.get("shortName", ticker)

        # Fecha del próximo earnings
        fecha_str = None
        dias_restantes = None
        if isinstance(cal, dict):
            dates = cal.get("Earnings Date", [])
            if dates:
                fecha = dates[0]
                fecha_str = fecha.strftime("%Y-%m-%d") if hasattr(fecha, "strftime") else str(fecha)
                hoy = datetime.date.today()
                delta = (fecha - hoy).days if hasattr(fecha, "year") else None
                dias_restantes = delta

        # Estimaciones de consenso
        eps_medio    = None
        eps_alto     = None
        eps_bajo     = None
        rev_medio    = None
        if isinstance(cal, dict):
            eps_medio = cal.get("Earnings Average")
            eps_alto  = cal.get("Earnings High")
            eps_bajo  = cal.get("Earnings Low")
            rev_medio = cal.get("Revenue Average")

        # EPS del último trimestre real (para contexto)
        eps_actual   = info.get("trailingEps")
        sector       = info.get("sector", "—")
        industria    = info.get("industry", "—")
        market_cap   = info.get("marketCap")

        return {
            "ticker":           ticker,
            "nombre":           nombre,
            "sector":           sector,
            "industria":        industria,
            "marketCap":        market_cap,
            "proximoEarnings":  fecha_str,
            "diasRestantes":    dias_restantes,
            "epsEstimado":      round(eps_medio, 4) if eps_medio is not None else None,
            "epsEstimadoAlto":  round(eps_alto,  4) if eps_alto  is not None else None,
            "epsEstimadoBajo":  round(eps_bajo,  4) if eps_bajo  is not None else None,
            "revenueEstimado":  int(rev_medio)      if rev_medio is not None else None,
            "epsActual":        round(eps_actual, 4) if eps_actual is not None else None,
            "ok":               True,
        }

    except Exception as e:
        return {
            "ticker":          ticker,
            "nombre":          ticker,
            "proximoEarnings": None,
            "diasRestantes":   None,
            "error":           str(e),
            "ok":              False,
        }


# ── Tabla de consola ──────────────────────────────────────────────────────────

def imprimir_tabla(resultados: list[dict]) -> None:
    hoy = datetime.date.today()
    print(f"\nEarnings Calendar  —  {hoy.strftime('%d %b %Y')}")
    print("=" * 80)
    print(f"{'Ticker':<8} {'Próximo earnings':<18} {'Días':<6} {'EPS est.':<10} {'Nombre'}")
    print("-" * 80)

    con_fecha = [r for r in resultados if r.get("proximoEarnings")]
    sin_fecha = [r for r in resultados if not r.get("proximoEarnings")]

    for r in sorted(con_fecha, key=lambda x: x["proximoEarnings"]):
        fecha   = r["proximoEarnings"]
        dias    = r.get("diasRestantes")
        dias_s  = f"{dias}d" if dias is not None else "—"
        eps_s   = f"${r['epsEstimado']:.2f}" if r.get("epsEstimado") is not None else "—"
        nombre  = (r.get("nombre") or "")[:35]

        # Color: verde si <= 7 días, amarillo si <= 30
        if dias is not None and dias <= 7:
            prefix = "  *** "
        elif dias is not None and dias <= 30:
            prefix = "  *   "
        else:
            prefix = "      "

        print(f"{prefix}{r['ticker']:<8} {fecha:<18} {dias_s:<6} {eps_s:<10} {nombre}")

    if sin_fecha:
        print(f"\n  Sin fecha disponible: {', '.join(r['ticker'] for r in sin_fecha)}")

    print("=" * 80)
    proximos = [r for r in con_fecha if r.get("diasRestantes") is not None and 0 <= r["diasRestantes"] <= 30]
    if proximos:
        print(f"  Próximos 30 días ({len(proximos)}): {', '.join(r['ticker'] for r in proximos)}")
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    tickers = get_tickers()
    print(f"\nEarnings Bot  —  {len(tickers)} tickers")
    print("=" * 50)

    resultados = []
    for i, ticker in enumerate(tickers, 1):
        print(f"  [{i:2d}/{len(tickers)}] {ticker:<8}", end=" ", flush=True)
        datos = get_earnings_data(ticker)
        resultados.append(datos)
        if datos.get("proximoEarnings"):
            dias = datos.get("diasRestantes")
            dias_s = f"en {dias}d" if dias is not None else ""
            print(f"{datos['proximoEarnings']}  {dias_s}")
        elif datos.get("error"):
            print(f"ERROR: {datos['error'][:60]}")
        else:
            print("Sin fecha")

    salida = {
        "actualizadoEn": datetime.datetime.now().isoformat(),
        "total":         len(resultados),
        "conFecha":      sum(1 for r in resultados if r.get("proximoEarnings")),
        "earnings":      resultados,
    }
    OUT_FILE.write_text(json.dumps(salida, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nGuardado en: {OUT_FILE.resolve()}")

    if "--print" in sys.argv or len(sys.argv) == 1:
        imprimir_tabla(resultados)


if __name__ == "__main__":
    main()
