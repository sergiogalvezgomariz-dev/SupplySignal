/**
 * update-earnings.js
 * Fetches enriched earnings data from Yahoo Finance for all tickers in pares.json.
 * Run by GitHub Actions daily; commits output/earnings.json back to the repo.
 */

const fs   = require("fs");
const path = require("path");

async function main() {
  const yf2 = require("yahoo-finance2");
  const yf  = new yf2.default({ suppressNotices: ["yahooSurvey"] });

  const pares   = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "pares.json"), "utf8")).pares;
  const tickers = [...new Set([
    ...pares.map(p => p.cliente).filter(Boolean),
    ...pares.map(p => p.proveedor).filter(Boolean),
  ])].sort();

  console.log(`Fetching ${tickers.length} tickers from Yahoo Finance…`);

  const today = new Date(); today.setHours(0, 0, 0, 0);
  const results = [];

  for (const ticker of tickers) {
    process.stdout.write(`  ${ticker} `);
    try {
      const modules = [
        "calendarEvents",
        "earningsHistory",
        "earningsTrend",
        "financialData",
        "recommendationTrend",
        "price",
      ];
      const summary = await yf.quoteSummary(ticker, { modules }).catch(() => ({}));

      // ── Próxima fecha de earnings ──────────────────────────────────────────
      const cal   = summary.calendarEvents || {};
      const dates = cal.earnings?.earningsDate || [];
      let fechaStr = null, diasRestantes = null, callTime = null;
      if (dates.length) {
        const d = new Date(typeof dates[0] === "object" && dates[0].toISOString ? dates[0] : dates[0]);
        if (!isNaN(d) && d >= today) {
          fechaStr      = d.toISOString().slice(0, 10);
          diasRestantes = Math.round((d - today) / 86400000);
          // Yahoo da 2 fechas cuando la hora exacta es TBD (rango de días)
          callTime = dates.length > 1 ? "TBD" : "TNS";
        }
      }

      // ── EPS / Revenue estimates (calendarEvents) ───────────────────────────
      const epsEstimadoAlto = cal.earnings?.earningsHigh   ?? null;
      const epsEstimadoBajo = cal.earnings?.earningsLow    ?? null;

      // ── Historial últimos 4 trimestres ────────────────────────────────────
      const historialEPS = (summary.earningsHistory?.history || []).slice(0, 4).map(h => ({
        periodo:  h.period   ?? null,
        fecha:    h.quarter  ? new Date(h.quarter).toISOString().slice(0, 10) : null,
        estimado: h.epsEstimate   != null ? +h.epsEstimate.toFixed(3)        : null,
        real:     h.actual        != null ? +h.actual.toFixed(3)             : null,
        sorpresa: h.surprisePercent != null ? +(h.surprisePercent * 100).toFixed(2) : null,
      }));

      // ── Estimaciones analistas (earningsTrend) ────────────────────────────
      const trend0 = summary.earningsTrend?.trend?.[0] || {};
      const epsConsenso        = trend0.earningsEstimate?.avg     ?? cal.earnings?.earningsAverage ?? null;
      const revenueConsenso    = trend0.revenueEstimate?.avg      ?? cal.earnings?.revenueAverage  ?? null;
      const crecimientoRevenue = trend0.revenueEstimate?.growth  != null ? +(trend0.revenueEstimate.growth  * 100).toFixed(1) : null;
      const crecimientoEPS     = trend0.earningsEstimate?.growth != null ? +(trend0.earningsEstimate.growth * 100).toFixed(1) : null;
      const numAnalistas       = trend0.earningsEstimate?.numberOfAnalysts ?? null;

      // ── Analistas (financialData + recommendationTrend) ───────────────────
      const fin = summary.financialData || {};
      const rec = summary.recommendationTrend?.trend?.[0] || {};
      const recomendacion      = fin.recommendationKey  ?? null;
      const recomendacionScore = fin.recommendationMean != null ? +fin.recommendationMean.toFixed(2) : null;
      const precioObjetivo     = fin.targetMeanPrice    ?? null;

      // ── Info básica (price) ───────────────────────────────────────────────
      const p = summary.price || {};
      const precioActual = p.regularMarketPrice ?? null;
      const upside = precioObjetivo && precioActual
        ? +(((precioObjetivo - precioActual) / precioActual) * 100).toFixed(1) : null;

      const totalAnalistas = (rec.strongBuy ?? 0) + (rec.buy ?? 0) + (rec.hold ?? 0) + (rec.sell ?? 0) + (rec.strongSell ?? 0);
      const desgloseAnalistas = totalAnalistas > 0 ? {
        strongBuy:  rec.strongBuy  ?? 0,
        buy:        rec.buy        ?? 0,
        hold:       rec.hold       ?? 0,
        sell:       rec.sell       ?? 0,
        strongSell: rec.strongSell ?? 0,
      } : null;

      results.push({
        ticker,
        nombre:    p.shortName || p.longName || ticker,
        sector:    p.sector    ?? null,
        industria: p.industry  ?? null,
        marketCap: p.marketCap ?? null,
        // Fecha próximo earnings
        proximoEarnings: fechaStr,
        callTime,
        diasRestantes,
        fechaSource: "yahoo",
        ok: !!fechaStr,
        // Estimaciones
        epsEstimado:      epsConsenso,
        epsEstimadoAlto,
        epsEstimadoBajo,
        revenueEstimado:  revenueConsenso,
        crecimientoRevenue,
        crecimientoEPS,
        numAnalistas,
        // Historial
        historialEPS,
        // Analistas
        recomendacion,
        recomendacionScore,
        precioObjetivo,
        precioActual,
        upside,
        desgloseAnalistas,
      });
      process.stdout.write("✓\n");
    } catch (err) {
      process.stdout.write(`✗ (${err.message})\n`);
      results.push({ ticker, proximoEarnings: null, diasRestantes: null, ok: false });
    }

    await new Promise(r => setTimeout(r, 500)); // evitar rate limit
  }

  const outFile = path.join(__dirname, "..", "output", "earnings.json");
  fs.writeFileSync(outFile, JSON.stringify({
    actualizadoEn: new Date().toISOString(),
    total:    results.length,
    conFecha: results.filter(e => e.proximoEarnings).length,
    earnings: results,
  }, null, 2), "utf8");

  console.log(`\nDone. ${results.filter(e => e.proximoEarnings).length}/${results.length} tickers with confirmed dates.`);
}

main().catch(err => { console.error(err); process.exit(1); });
