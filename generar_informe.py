"""
generar_informe.py  —  Genera informe HTML de señales activas de SupplySignal.

Para cada señal activa obtiene de Yahoo Finance:
  · Resumen de negocio de cliente y proveedor
  · Precios históricos (6 meses) solapados en una gráfica
  · Próximo earnings call de ambas empresas

Uso:
    python generar_informe.py            → guarda output/informe_senales.html
    python generar_informe.py --json     → imprime JSON de datos (para debug)
"""

import sys
import json
import base64
import io
import datetime
from pathlib import Path

# Fix Windows console encoding
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests
import yfinance as yf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

SERVER  = "http://localhost:3100"
OUT     = Path("output/informe_senales.html")
OUT.parent.mkdir(exist_ok=True)

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_signals():
    r = requests.get(f"{SERVER}/api/signals", timeout=10)
    return r.json().get("senales", [])


def get_info(ticker: str) -> dict:
    try:
        info = yf.Ticker(ticker).info
        return {
            "nombre":    info.get("longName") or info.get("shortName", ticker),
            "resumen":   info.get("longBusinessSummary", "Sin descripción disponible."),
            "sector":    info.get("sector", "—"),
            "industria": info.get("industry", "—"),
            "pais":      info.get("country", "—"),
            "empleados": info.get("fullTimeEmployees"),
            "web":       info.get("website", ""),
        }
    except Exception:
        return {"nombre": ticker, "resumen": "No disponible.", "sector": "—", "industria": "—", "pais": "—"}


def get_earnings(ticker: str) -> str:
    try:
        cal = yf.Ticker(ticker).calendar
        if not cal:
            return "No disponible"
        # yfinance >= 0.2 devuelve dict
        if isinstance(cal, dict):
            dates = cal.get("Earnings Date", [])
            if dates:
                d = dates[0]
                return d.strftime("%d %b %Y") if hasattr(d, "strftime") else str(d)
        # versiones antiguas devuelven DataFrame
        elif hasattr(cal, "columns") and len(cal.columns):
            fecha = cal.columns[0]
            return fecha.strftime("%d %b %Y") if hasattr(fecha, "strftime") else str(fecha)
        return "No disponible"
    except Exception:
        return "No disponible"


def get_history(ticker: str, period: str = "6mo"):
    try:
        df = yf.download(ticker, period=period, progress=False, auto_adjust=True)
        if df.empty:
            return None
        close = df["Close"]
        if hasattr(close, "squeeze"):
            close = close.squeeze()
        return close
    except Exception:
        return None


def make_chart(ticker_cli: str, nombre_cli: str,
               ticker_prov: str, nombre_prov: str) -> str:
    """Genera gráfica solapada y devuelve base64 PNG."""
    serie_cli  = get_history(ticker_cli)
    serie_prov = get_history(ticker_prov)

    fig, ax = plt.subplots(figsize=(9, 3.6))
    fig.patch.set_facecolor("#ffffff")
    ax.set_facecolor("#fafbfd")

    COLOR_CLI  = "#001F5B"   # navy
    COLOR_PROV = "#C8962A"   # gold

    plotted = False
    if serie_cli is not None and not serie_cli.dropna().empty:
        ax2 = ax.twinx()
        ax2.plot(serie_cli.index, serie_cli.values,
                 color=COLOR_CLI, linewidth=1.6, label=f"{ticker_cli} ({nombre_cli})")
        ax2.set_ylabel(ticker_cli, color=COLOR_CLI, fontsize=9)
        ax2.tick_params(axis="y", labelcolor=COLOR_CLI, labelsize=8)
        ax2.spines["right"].set_color(COLOR_CLI)
        ax2.yaxis.label.set_color(COLOR_CLI)
        plotted = True
    else:
        ax2 = None

    if serie_prov is not None and not serie_prov.dropna().empty:
        ax.plot(serie_prov.index, serie_prov.values,
                color=COLOR_PROV, linewidth=1.6, label=f"{ticker_prov} ({nombre_prov})",
                linestyle="--")
        ax.set_ylabel(ticker_prov, color=COLOR_PROV, fontsize=9)
        ax.tick_params(axis="y", labelcolor=COLOR_PROV, labelsize=8)
        ax.spines["left"].set_color(COLOR_PROV)
        plotted = True

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.tick_params(axis="x", labelsize=8, colors="#6b7280")
    for spine in ["top", "bottom"]:
        ax.spines[spine].set_visible(False)
    if ax2:
        for spine in ["top", "bottom"]:
            ax2.spines[spine].set_visible(False)
    ax.grid(axis="x", color="#e5e7eb", linewidth=0.6, linestyle="--")

    # Leyenda combinada
    handles, labels = [], []
    if ax2:
        h, l = ax2.get_legend_handles_labels()
        handles += h; labels += l
    h, l = ax.get_legend_handles_labels()
    handles += h; labels += l
    if handles:
        ax.legend(handles, labels, loc="upper left", fontsize=8.5,
                  framealpha=0.9, edgecolor="#e5e7eb")

    if not plotted:
        ax.text(0.5, 0.5, "Sin datos disponibles", transform=ax.transAxes,
                ha="center", va="center", color="#9ca3af")

    plt.tight_layout(pad=0.8)
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=130, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()


# ── Construcción del HTML ─────────────────────────────────────────────────────

HTML_HEAD = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SupplySignal — Informe de señales</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Playfair+Display:wght@600;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap');
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'IBM Plex Sans', sans-serif; background: #f4f6fb; color: #1f2937; font-size: 14px; }

  header { background: #001F5B; color: #fff; padding: 28px 40px; }
  header h1 { font-family: 'Playfair Display', serif; font-size: 1.7rem; letter-spacing: -.3px; }
  header p  { color: rgba(255,255,255,.65); font-size: .85rem; margin-top: 6px; }

  main { max-width: 960px; margin: 32px auto; padding: 0 20px; }

  .signal-card {
    background: #fff;
    border-radius: 12px;
    box-shadow: 0 2px 8px rgba(0,0,0,.07);
    margin-bottom: 36px;
    overflow: hidden;
  }
  .card-head {
    background: #001F5B;
    color: #fff;
    padding: 18px 24px;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .card-head-left h2 { font-family: 'Playfair Display', serif; font-size: 1.15rem; }
  .card-head-left p  { color: rgba(255,255,255,.6); font-size: .82rem; margin-top: 3px; }
  .badge {
    font-family: 'IBM Plex Mono', monospace;
    font-size: .75rem;
    font-weight: 600;
    padding: 4px 12px;
    border-radius: 4px;
    letter-spacing: .8px;
  }
  .badge-buy  { background: #dcfce7; color: #15803d; }
  .badge-sell { background: #fee2e2; color: #b91c1c; }

  .card-body { padding: 24px; }

  .companies-grid {
    display: grid;
    grid-template-columns: 1fr 40px 1fr;
    gap: 0;
    margin-bottom: 24px;
    align-items: start;
  }
  .company-box { padding: 16px; background: #f8f9fc; border-radius: 8px; }
  .company-box .role { font-size: 10px; font-weight: 600; letter-spacing: 1px; text-transform: uppercase; color: #9ca3af; margin-bottom: 4px; }
  .company-box .ticker { font-family: 'IBM Plex Mono', monospace; font-size: 1.2rem; font-weight: 600; color: #001F5B; }
  .company-box .name   { font-size: .85rem; color: #6b7280; margin-bottom: 10px; }
  .company-box .meta   { font-size: .8rem; color: #6b7280; line-height: 1.7; }
  .company-box .summary{ font-size: .82rem; color: #374151; line-height: 1.6; margin-top: 10px;
                          display: -webkit-box; -webkit-line-clamp: 5; -webkit-box-orient: vertical; overflow: hidden; }
  .arrow-col { display: flex; align-items: center; justify-content: center; font-size: 1.5rem; color: #C8962A; padding-top: 24px; }

  .earnings-row {
    display: grid;
    grid-template-columns: 1fr 40px 1fr;
    gap: 0;
    margin-bottom: 20px;
  }
  .earnings-box {
    background: #fffbeb;
    border: 1px solid #fde68a;
    border-radius: 8px;
    padding: 12px 16px;
    font-size: .83rem;
  }
  .earnings-box .elabel { font-size: .75rem; font-weight: 600; letter-spacing: .8px; text-transform: uppercase; color: #92400e; margin-bottom: 4px; }
  .earnings-box .edate  { font-family: 'IBM Plex Mono', monospace; font-size: .95rem; font-weight: 600; color: #78350f; }

  .chart-section h3 { font-size: .82rem; font-weight: 600; letter-spacing: .6px; text-transform: uppercase; color: #9ca3af; margin-bottom: 12px; }
  .chart-section img { width: 100%; border-radius: 8px; border: 1px solid #e5e7eb; }

  .signal-box {
    background: #f0f4ff;
    border-left: 3px solid #001F5B;
    padding: 14px 18px;
    border-radius: 0 8px 8px 0;
    margin-top: 20px;
    font-size: .86rem;
    color: #374151;
    line-height: 1.6;
  }
  .signal-box strong { color: #001F5B; }

  .score-badge {
    display: inline-block;
    font-family: 'IBM Plex Mono', monospace;
    font-size: .8rem;
    font-weight: 600;
    background: #C8962A;
    color: #fff;
    padding: 2px 10px;
    border-radius: 4px;
    margin-left: 10px;
  }

  footer { text-align: center; color: #9ca3af; font-size: .75rem; padding: 30px 20px; }
  .no-signals { text-align: center; padding: 60px 20px; color: #6b7280; }
  .no-signals h2 { font-size: 1.3rem; margin-bottom: 8px; color: #374151; }
</style>
</head>
<body>
<header>
  <h1>SupplySignal — Signal Report</h1>
  <p>Generated: FECHA_PLACEHOLDER · Active signals based on supply-chain relationships · Cohen &amp; Frazzini (2008)</p>
</header>
<main>
"""

HTML_FOOT = """
</main>
<footer>SupplySignal · Data: Yahoo Finance · For research purposes only. Not investment advice.</footer>
</body></html>"""


def render_signal(s: dict) -> str:
    ticker_cli  = s.get("cliente", "")
    ticker_prov = s.get("proveedor", "")
    accion      = s.get("accion", "")
    puntuacion  = s.get("puntuacion", 0)
    explicacion = s.get("explicacion", "")

    print(f"  -> Procesando {ticker_cli} / {ticker_prov}...", end=" ", flush=True)

    info_cli  = get_info(ticker_cli)
    info_prov = get_info(ticker_prov)
    earn_cli  = get_earnings(ticker_cli)
    earn_prov = get_earnings(ticker_prov)
    chart_b64 = make_chart(ticker_cli, info_cli["nombre"],
                           ticker_prov, info_prov["nombre"])

    print("OK")

    badge_cls = "badge-buy" if accion == "COMPRA" else "badge-sell"
    badge_txt = "BUY SIGNAL" if accion == "COMPRA" else "SELL SIGNAL"

    emp_cli  = f"{info_cli['empleados']:,}" if info_cli.get("empleados") else "—"
    emp_prov = f"{info_prov['empleados']:,}" if info_prov.get("empleados") else "—"

    def company_html(ticker, info, emp):
        return f"""
        <div class="company-box">
          <div class="role">{"Customer" if ticker == ticker_cli else "Supplier"}</div>
          <div class="ticker">{ticker}</div>
          <div class="name">{info['nombre']}</div>
          <div class="meta">
            Sector: {info['sector']}<br>
            Industry: {info['industria']}<br>
            Country: {info['pais']}<br>
            Employees: {emp}
          </div>
          <div class="summary">{info['resumen']}</div>
        </div>"""

    return f"""
  <div class="signal-card">
    <div class="card-head">
      <div class="card-head-left">
        <h2>{info_cli['nombre']} → {info_prov['nombre']}</h2>
        <p>{ticker_cli} supplies chain impact on {ticker_prov} · Dependency: {s.get('dependencia','—')}% · Lag: {s.get('lag','—')} days</p>
      </div>
      <span class="badge {badge_cls}">{badge_txt} <span class="score-badge">Score {puntuacion}</span></span>
    </div>
    <div class="card-body">

      <div class="companies-grid">
        {company_html(ticker_cli, info_cli, emp_cli)}
        <div class="arrow-col">→</div>
        {company_html(ticker_prov, info_prov, emp_prov)}
      </div>

      <div class="earnings-row">
        <div class="earnings-box">
          <div class="elabel">Next Earnings — {ticker_cli}</div>
          <div class="edate">{earn_cli}</div>
        </div>
        <div></div>
        <div class="earnings-box">
          <div class="elabel">Next Earnings — {ticker_prov}</div>
          <div class="edate">{earn_prov}</div>
        </div>
      </div>

      <div class="chart-section">
        <h3>6-Month Historical Price — {ticker_cli} vs {ticker_prov}</h3>
        <img src="data:image/png;base64,{chart_b64}" alt="Price chart">
      </div>

      <div class="signal-box">
        <strong>Signal rationale:</strong> {explicacion}
      </div>

    </div>
  </div>"""


def main():
    print("\nSupplySignal — Generando informe de señales")
    print("=" * 50)

    signals = get_signals()
    if not signals:
        print("Sin señales activas en este momento.")
        html = (HTML_HEAD.replace("FECHA_PLACEHOLDER", datetime.datetime.now().strftime("%d/%m/%Y %H:%M"))
                + '<div class="no-signals"><h2>No active signals</h2><p>No companies have moved more than 3% today.</p></div>'
                + HTML_FOOT)
        OUT.write_text(html, encoding="utf-8")
        print(f"Informe guardado en: {OUT}")
        return

    print(f"{len(signals)} señal(es) activa(s). Generando informe...\n")
    fecha = datetime.datetime.now().strftime("%d/%m/%Y %H:%M")
    cuerpo = ""
    for s in signals:
        cuerpo += render_signal(s)

    html = HTML_HEAD.replace("FECHA_PLACEHOLDER", fecha) + cuerpo + HTML_FOOT
    OUT.write_text(html, encoding="utf-8")
    print(f"\n{'='*50}")
    print(f"Informe guardado en: {OUT.resolve()}")
    print(f"Abre en el navegador: file:///{OUT.resolve().as_posix()}")


if __name__ == "__main__":
    main()
