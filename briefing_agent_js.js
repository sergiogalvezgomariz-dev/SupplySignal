/**
 * briefing_agent_js.js — Earnings Briefing Agent (Node.js / Vercel-compatible)
 *
 * Port of earnings_briefing_agent.py. Runs natively in Node without Python.
 * Sources: yahoo-finance2 + SEC EDGAR (https) + Anthropic SDK + pares.json
 */

"use strict";

const fs    = require("fs");
const path  = require("path");
const https = require("https");

const PARES_FILE    = path.join(__dirname, "pares.json");
const EARNINGS_FILE = path.join(__dirname, "output", "earnings.json");
const OUT_FILE      = path.join(__dirname, "output", "briefings.json");
const MEMORY_FILE   = path.join(__dirname, "output", "eps_memory.json");
const EDGAR_UA      = "SupplySignal research@supplysignal.com";
const CLAUDE_MODEL  = "claude-sonnet-4-6";

const SYSTEM_PROMPT = `You are a senior equity research analyst specializing in supply chain intelligence.
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
- Use hyperbolic language

Format your response as JSON with this exact structure:
{
  "headline": "One-sentence summary of the most important thing to watch",
  "business_context": "2-3 sentences on what this company does and why it matters in the supply chain",
  "market_expectations": "What consensus expects: EPS, revenue, key metrics",
  "eps_trend_analysis": "Analysis of last 4 quarters beat/miss history and what it implies",
  "supply_chain_exposure": "Which suppliers or customers are exposed, quantified where possible",
  "key_risks": "1-2 specific risks that could surprise to the downside",
  "key_catalysts": "1-2 specific factors that could drive an upside surprise",
  "options_read": "1-2 sentences interpreting the implied move vs historical move.",
  "revision_read": "1 sentence on analyst estimate revision trend.",
  "watch_list": ["list", "of", "specific", "metrics", "to", "watch"]
}`;

// ── Helpers ──────────────────────────────────────────────────────────────────

function httpsGet(url) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    https.get({
      hostname: u.hostname,
      path:     u.pathname + u.search,
      headers:  { "User-Agent": EDGAR_UA, "Accept": "application/json" },
    }, res => {
      let data = "";
      res.on("data", c => data += c);
      res.on("end", () => {
        try { resolve(JSON.parse(data)); }
        catch { resolve(data); }
      });
    }).on("error", reject);
  });
}

function stripHtml(html) {
  return html
    .replace(/<script[^>]*>[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[^>]*>[\s\S]*?<\/style>/gi, " ")
    .replace(/<!--[\s\S]*?-->/g, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/g, " ").replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<").replace(/&gt;/g, ">")
    .replace(/\s+/g, " ").trim();
}

function getYF() {
  const yf2 = require("yahoo-finance2");
  return new yf2.default({ suppressNotices: ["yahooSurvey"] });
}

// ── Skills ────────────────────────────────────────────────────────────────────

function getEarningsCalendar(days) {
  if (!fs.existsSync(EARNINGS_FILE)) return [];
  const data  = JSON.parse(fs.readFileSync(EARNINGS_FILE, "utf8"));
  const today = new Date(); today.setHours(0,0,0,0);
  const limit = new Date(today); limit.setDate(limit.getDate() + days);
  return (data.earnings || [])
    .filter(e => {
      if (!e.proximoEarnings) return false;
      const d = new Date(e.proximoEarnings);
      return d >= today && d <= limit;
    })
    .sort((a, b) => a.proximoEarnings.localeCompare(b.proximoEarnings));
}

async function skillGetEpsHistory(ticker) {
  try {
    const yf      = getYF();
    const summary = await yf.quoteSummary(ticker, {
      modules: ["earnings", "calendarEvents", "earningsTrend"],
    }).catch(() => ({}));

    const earn  = summary.earnings     || {};
    const cal   = summary.calendarEvents || {};
    const trend = summary.earningsTrend || {};

    // Quarterly history
    const quarters = [];
    for (const q of (earn.earningsChart?.quarterly || []).slice(-4)) {
      const actual   = q.actual   != null ? +q.actual.toFixed(4)   : null;
      const estimate = q.estimate != null ? +q.estimate.toFixed(4) : null;
      const beat     = actual != null && estimate != null ? actual > estimate : null;
      const surp     = actual != null && estimate != null && estimate !== 0
        ? +((actual - estimate) / Math.abs(estimate) * 100).toFixed(2) : null;
      quarters.push({ quarter: q.date || "", epsActual: actual, epsEstimate: estimate, surprisePct: surp, beat });
    }

    // Beat streak
    const beats = quarters.map(q => q.beat).filter(b => b !== null);
    let streak = 0;
    if (beats.length) {
      const last = beats[beats.length - 1];
      for (let i = beats.length - 1; i >= 0; i--) {
        if (beats[i] === last) streak++; else break;
      }
      if (!last) streak = -streak;
    }

    // Next quarter estimates
    const earningsDates = cal.earnings?.earningsDate || [];
    const calEst        = (trend.trend || []).find(t => t.period === "0q") || {};
    const epsNextQ      = calEst.earningsEstimate?.avg?.raw ?? null;
    const epsLow        = calEst.earningsEstimate?.low?.raw ?? null;
    const epsHigh       = calEst.earningsEstimate?.high?.raw ?? null;
    const revNextQ      = calEst.revenueEstimate?.avg?.raw ?? null;

    return { quarters, beatStreak: streak, epsNextQ, epsLow, epsHigh, revNextQ };
  } catch(e) {
    return { error: e.message, quarters: [], beatStreak: 0 };
  }
}

async function skillGetCompanyInfo(ticker) {
  try {
    const yf      = getYF();
    const [quote, summary] = await Promise.all([
      yf.quote(ticker).catch(() => ({})),
      yf.quoteSummary(ticker, { modules: ["summaryProfile", "financialData", "defaultKeyStatistics"] }).catch(() => ({})),
    ]);
    const prof = summary.summaryProfile   || {};
    const fin  = summary.financialData    || {};
    const ks   = summary.defaultKeyStatistics || {};
    return {
      nombre:      quote.longName || quote.shortName || ticker,
      sector:      prof.sector    || "—",
      industria:   prof.industry  || "—",
      pais:        prof.country   || "—",
      resumen:     (prof.longBusinessSummary || "").slice(0, 600),
      marketCap:   quote.marketCap,
      revenue_ttm: fin.totalRevenue?.raw,
      margen_bruto: fin.grossMargins?.raw,
      margen_neto:  fin.profitMargins?.raw,
      pe_ratio:     quote.trailingPE,
      precio:       quote.regularMarketPrice || quote.previousClose,
      "52w_high":   quote.fiftyTwoWeekHigh,
      "52w_low":    quote.fiftyTwoWeekLow,
    };
  } catch(e) {
    return { nombre: ticker, error: e.message };
  }
}

async function skillGetNews(ticker, maxItems = 6) {
  try {
    const yf  = getYF();
    const res = await yf.search(ticker, { newsCount: maxItems, quotesCount: 0 }).catch(() => ({}));
    return (res.news || []).slice(0, maxItems).map(n => ({
      title:    n.title || "",
      provider: n.publisher || "",
      url:      n.link  || "",
      fecha:    n.providerPublishTime
                  ? new Date(n.providerPublishTime * 1000).toISOString().slice(0, 10)
                  : "",
    })).filter(n => n.title);
  } catch { return []; }
}

async function skillGetImpliedMove(ticker, earningsDate) {
  try {
    const yf    = getYF();
    const quote = await yf.quote(ticker).catch(() => ({}));
    const price = quote.regularMarketPrice || quote.previousClose;
    if (!price) return { available: false, error: "No price" };

    const opts = await yf.options(ticker).catch(() => null);
    if (!opts || !opts.expirationDates?.length) return { available: false, error: "No options data" };

    // Find expiry just after earnings date
    let targetExpiry = opts.expirationDates[0];
    if (earningsDate && earningsDate !== "—") {
      const earnDt = new Date(earningsDate);
      for (const exp of opts.expirationDates) {
        if (new Date(exp) >= earnDt) { targetExpiry = exp; break; }
      }
    }

    const chain = await yf.options(ticker, { date: targetExpiry }).catch(() => null);
    if (!chain?.options?.[0]) return { available: false, error: "Empty chain" };

    const calls = chain.options[0].calls || [];
    const puts  = chain.options[0].puts  || [];
    if (!calls.length || !puts.length) return { available: false, error: "Empty chain" };

    // ATM strike
    const atmCall = calls.reduce((best, c) =>
      Math.abs(c.strike - price) < Math.abs(best.strike - price) ? c : best, calls[0]);
    const atmStrike = atmCall.strike;

    const callRow = calls.find(c => c.strike === atmStrike);
    const putRow  = puts.find(p => p.strike  === atmStrike);
    if (!callRow || !putRow) return { available: false, error: "ATM strike not in both legs" };

    const callMid = ((callRow.bid || 0) + (callRow.ask || 0)) / 2 || callRow.lastPrice || 0;
    const putMid  = ((putRow.bid  || 0) + (putRow.ask  || 0)) / 2 || putRow.lastPrice  || 0;
    const straddle = callMid + putMid;
    const impliedMovePct = +(straddle / price * 100).toFixed(2);

    return {
      available: true,
      stockPrice: +price.toFixed(2),
      expiry: targetExpiry,
      atmStrike,
      callMid: +callMid.toFixed(2),
      putMid:  +putMid.toFixed(2),
      straddle: +straddle.toFixed(2),
      impliedMovePct,
      label: `±${impliedMovePct}%`,
    };
  } catch(e) {
    return { available: false, error: e.message };
  }
}

async function skillGetHistoricalMoves(ticker, quarters) {
  try {
    if (!quarters?.length) return { available: false, moves: [] };
    const yf   = getYF();
    const hist = await yf.historical(ticker, { period1: "2022-01-01", interval: "1d" }).catch(() => []);
    if (!hist.length) return { available: false, moves: [] };

    const moves = [];
    for (const q of quarters) {
      if (!q.quarter) continue;
      const qDate = new Date(q.quarter);
      // Find index of that date or nearest
      let idx = hist.findIndex(d => new Date(d.date) >= qDate);
      if (idx < 0 || idx + 1 >= hist.length) continue;
      const before = hist[idx].close;
      const after  = hist[idx + 1].close;
      const movePct = +((after - before) / before * 100).toFixed(2);
      moves.push({ quarter: q.quarter.slice(0, 10), movePct, direction: movePct > 0 ? "up" : "down" });
    }

    if (!moves.length) return { available: false, moves: [] };
    const absM = moves.map(m => Math.abs(m.movePct));
    return {
      available:  true,
      moves,
      avgAbsMove: +(absM.reduce((a,b) => a+b, 0) / absM.length).toFixed(2),
      maxMove:    +Math.max(...absM).toFixed(2),
      nSamples:   moves.length,
    };
  } catch(e) {
    return { available: false, moves: [], error: e.message };
  }
}

async function skillGetEstimateRevisions(ticker) {
  try {
    const yf      = getYF();
    const summary = await yf.quoteSummary(ticker, {
      modules: ["earningsTrend", "recommendationTrend"],
    }).catch(() => ({}));

    const trend = summary.earningsTrend || {};
    const curr  = (trend.trend || []).find(t => t.period === "0q") || {};
    const rev   = curr.epsTrend || {};

    const up30d = rev.upLast30days  != null ? +rev.upLast30days  : 0;
    const dn30d = rev.downLast30days != null ? +rev.downLast30days : 0;
    const up7d  = rev.upLast7days   != null ? +rev.upLast7days   : 0;

    let trend_val = "unknown";
    if (up30d > dn30d * 1.5)      trend_val = "rising";
    else if (dn30d > up30d * 1.5) trend_val = "falling";
    else if (up30d > 0 || dn30d > 0) trend_val = "stable";

    const est      = curr.earningsEstimate || {};
    const analysts = est.numberOfAnalysts?.raw ?? null;
    const epsGrowth= est.growth?.raw ?? null;
    const ptMean   = curr.revenueEstimate?.avg?.raw ?? null;

    return {
      available: true, trend: trend_val,
      up30d, dn30d, up7d, analysts, epsGrowth,
    };
  } catch(e) {
    return { available: false, trend: "unknown", error: e.message };
  }
}

async function skillGetLatest8K(ticker, daysBack = 3) {
  try {
    const tickers = await httpsGet("https://www.sec.gov/files/company_tickers.json");
    let cik = null;
    for (const e of Object.values(tickers)) {
      if ((e.ticker || "").toUpperCase() === ticker.toUpperCase()) { cik = e.cik_str; break; }
    }
    if (!cik) return { available: false, error: `CIK not found for ${ticker}` };

    const cikPadded = String(+cik).padStart(10, "0");
    const subs      = await httpsGet(`https://data.sec.gov/submissions/CIK${cikPadded}.json`);
    const recent    = (subs.filings || {}).recent || {};
    const forms     = recent.form              || [];
    const dates     = recent.filingDate        || [];
    const accNums   = recent.accessionNumber   || [];
    const docs      = recent.primaryDocument   || [];

    const cutoff = new Date(); cutoff.setDate(cutoff.getDate() - daysBack);
    const cutoffStr = cutoff.toISOString().slice(0, 10);

    let found = null;
    for (let i = 0; i < forms.length; i++) {
      if ((forms[i] === "8-K" || forms[i] === "8-K/A") && dates[i] >= cutoffStr) {
        const acc = (accNums[i] || "").replace(/-/g, "");
        found = {
          filingDate: dates[i],
          docUrl: docs[i] ? `https://www.sec.gov/Archives/edgar/data/${+cik}/${acc}/${docs[i]}` : "",
          edgarUrl: `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=${cik}&type=8-K&count=5`,
        };
        break;
      }
    }
    if (!found) return { available: false, message: `No 8-K in last ${daysBack} days for ${ticker}` };

    // Fetch document text
    let textExtract = "";
    if (found.docUrl) {
      try {
        const raw = await new Promise((res, rej) => {
          https.get(found.docUrl, { headers: { "User-Agent": EDGAR_UA } }, r => {
            let d = ""; r.on("data", c => d += c); r.on("end", () => res(d));
          }).on("error", rej);
        });
        textExtract = stripHtml(raw).slice(0, 8000);
      } catch {}
    }

    return { available: true, ...found, textExtract };
  } catch(e) {
    return { available: false, error: e.message };
  }
}

function getSupplyChainExposure(ticker, pares) {
  const asCustomer = pares.filter(p => p.cliente === ticker && p.proveedor);
  const asSupplier = pares.filter(p => p.proveedor === ticker && p.cliente);
  return {
    esCliente:   asCustomer.length > 0,
    esProveedor: asSupplier.length > 0,
    proveedores: asCustomer.map(p => ({ ticker: p.proveedor, nombre: p.proveedorNombre, dependencia: p.dependencia, lag: p.lag })),
    clientes:    asSupplier.map(p => ({ ticker: p.cliente,   nombre: p.clienteNombre,   dependencia: p.dependencia, lag: p.lag })),
  };
}

// ── EPS Memory ────────────────────────────────────────────────────────────────

function loadEpsMemory() {
  if (fs.existsSync(MEMORY_FILE))
    try { return JSON.parse(fs.readFileSync(MEMORY_FILE, "utf8")); } catch {}
  return {};
}

function updateEpsMemory(memory, ticker, epsData) {
  if (!memory[ticker]) memory[ticker] = { quarters: [], updatedAt: null };
  const existing = new Set(memory[ticker].quarters.map(q => q.quarter));
  for (const q of (epsData.quarters || []))
    if (!existing.has(q.quarter)) memory[ticker].quarters.push(q);
  memory[ticker].quarters = memory[ticker].quarters
    .sort((a, b) => a.quarter.localeCompare(b.quarter)).slice(-8);
  memory[ticker].updatedAt = new Date().toISOString();
  fs.writeFileSync(MEMORY_FILE, JSON.stringify(memory, null, 2), "utf8");
  return memory;
}

// ── Claude call ───────────────────────────────────────────────────────────────

async function generateBriefingWithClaude(ticker, earningsDate, dias, companyInfo, epsData, supplyChain, memory, impliedMove, histMoves, revisions, edgar8k) {
  const Anthropic = require("@anthropic-ai/sdk");
  const client    = new Anthropic.default();

  const mc = (companyInfo.marketCap || 0) / 1e9;
  const memoriaHist = (memory[ticker]?.quarters || []).slice(0, -4);
  const memTxt = memoriaHist.length > 0
    ? `\nHistorical memory (quarters before last 4): ${memoriaHist.length} quarters, ${memoriaHist.filter(q => q.beat).length} beats.`
    : "";

  let ctx = `TICKER: ${ticker}
EARNINGS DATE: ${earningsDate} (in ${dias} days)
COMPANY: ${companyInfo.nombre || ticker}
SECTOR: ${companyInfo.sector} / ${companyInfo.industria}
MARKET CAP: $${mc.toFixed(1)}B
CURRENT PRICE: $${companyInfo.precio || "—"}
52W RANGE: $${companyInfo["52w_low"] || "—"} – $${companyInfo["52w_high"] || "—"}
GROSS MARGIN: ${((companyInfo.margen_bruto || 0) * 100).toFixed(1)}%
NET MARGIN: ${((companyInfo.margen_neto || 0) * 100).toFixed(1)}%
P/E RATIO: ${companyInfo.pe_ratio || "—"}

BUSINESS SUMMARY:
${(companyInfo.resumen || "Not available").slice(0, 500)}

CONSENSUS ESTIMATES FOR NEXT QUARTER:
- EPS estimate: $${epsData.epsNextQ || "—"}
- EPS range: $${epsData.epsLow || "—"} – $${epsData.epsHigh || "—"}
- Revenue estimate: $${((epsData.revNextQ || 0) / 1e9).toFixed(2)}B

LAST 4 QUARTERS EPS HISTORY:
`;
  for (const q of (epsData.quarters || [])) {
    const b = q.beat === true ? "BEAT" : q.beat === false ? "MISS" : "—";
    ctx += `  ${q.quarter}: actual $${q.epsActual} vs est $${q.epsEstimate} → ${b} (${q.surprisePct || "—"}%)\n`;
  }
  const streak = epsData.beatStreak || 0;
  if (streak > 1)       ctx += `\nBEAT STREAK: ${streak} consecutive beats\n`;
  else if (streak < -1) ctx += `\nMISS STREAK: ${Math.abs(streak)} consecutive misses\n`;
  ctx += memTxt;

  ctx += `\nSUPPLY CHAIN EXPOSURE:\n- Role: ${supplyChain.esCliente ? "Customer (has suppliers)" : ""} ${supplyChain.esProveedor ? "Supplier (has customers)" : ""}\n`;
  if (supplyChain.proveedores?.length) {
    ctx += "Key suppliers:\n";
    for (const p of supplyChain.proveedores)
      ctx += `  - ${p.ticker} (${p.nombre}): ${p.dependencia}% revenue dependency, ~${p.lag} day lag\n`;
  }
  if (supplyChain.clientes?.length) {
    ctx += "Key customers:\n";
    for (const c of supplyChain.clientes)
      ctx += `  - ${c.ticker} (${c.nombre}): ${c.dependencia}% of supplier revenue, ~${c.lag} day lag\n`;
  }

  if (revisions?.available) {
    const trendStr = { rising: "RISING (bullish setup)", falling: "FALLING (bearish setup)", stable: "STABLE" }[revisions.trend] || "unknown";
    ctx += `\nANALYST ESTIMATE REVISIONS (last 30 days):
- Trend: ${trendStr}
- Estimates raised (up 30d): ${revisions.up30d ?? "—"} analysts
- Estimates cut (dn 30d): ${revisions.dn30d ?? "—"} analysts
- Estimates raised (up 7d): ${revisions.up7d ?? "—"} analysts
- Total analysts covering: ${revisions.analysts ?? "—"}
- EPS growth expected vs YoY: ${((revisions.epsGrowth || 0) * 100).toFixed(1)}%\n`;
  } else {
    ctx += "\nANALYST REVISIONS: Not available.\n";
  }

  if (edgar8k?.available && edgar8k.textExtract) {
    ctx += `\nSEC EDGAR 8-K FILING (filed ${edgar8k.filingDate}):\n— Results already published. Extract:\n${edgar8k.textExtract.slice(0, 3000)}\n[...truncated]\n`;
  }

  if (impliedMove?.available) {
    ctx += `\nOPTIONS MARKET — IMPLIED MOVE:
- ATM straddle: $${impliedMove.straddle} (call $${impliedMove.callMid} + put $${impliedMove.putMid})
- Stock price: $${impliedMove.stockPrice}  |  ATM strike: $${impliedMove.atmStrike}
- Implied move: ${impliedMove.label}  (expiry: ${impliedMove.expiry})\n`;
  } else {
    ctx += "\nOPTIONS MARKET: No options data available.\n";
  }

  if (histMoves?.available) {
    ctx += `\nHISTORICAL EARNINGS MOVES (last ${histMoves.nSamples} quarters):
- Average absolute move: ±${histMoves.avgAbsMove}%
- Max observed move: ±${histMoves.maxMove}%
- Individual moves: ${histMoves.moves.slice(-4).map(m => `${m.movePct > 0 ? "+" : ""}${m.movePct}%`).join(", ")}\n`;
    if (impliedMove?.available) {
      const delta = impliedMove.impliedMovePct - histMoves.avgAbsMove;
      if      (delta >  1.5) ctx += `→ Options EXPENSIVE: pricing ${delta.toFixed(1)}pp MORE than historical avg.\n`;
      else if (delta < -1.5) ctx += `→ Options CHEAP: pricing ${Math.abs(delta).toFixed(1)}pp LESS than historical avg.\n`;
      else                   ctx += `→ Options FAIRLY PRICED vs historical earnings volatility.\n`;
    }
  } else {
    ctx += "\nHISTORICAL MOVES: Insufficient data.\n";
  }

  ctx += "\nGenerate the pre-earnings briefing as JSON per the format specified.";

  const msg = await client.messages.create({
    model: CLAUDE_MODEL, max_tokens: 1024,
    system: SYSTEM_PROMPT,
    messages: [{ role: "user", content: ctx }],
  });

  let text = msg.content[0].text.trim();
  if (text.includes("```json")) text = text.split("```json")[1].split("```")[0].trim();
  else if (text.includes("```")) text = text.split("```")[1].split("```")[0].trim();

  try { return JSON.parse(text); } catch {}

  // Retry: ask Claude to fix JSON
  const fix = await client.messages.create({
    model: CLAUDE_MODEL, max_tokens: 1200,
    messages: [{ role: "user", content: `Fix this JSON so it is valid. Return only the corrected JSON:\n\n${text}` }],
  });
  let t2 = fix.content[0].text.trim();
  if (t2.includes("```json")) t2 = t2.split("```json")[1].split("```")[0].trim();
  else if (t2.includes("```")) t2 = t2.split("```")[1].split("```")[0].trim();
  return JSON.parse(t2);
}

// ── Main pipeline ─────────────────────────────────────────────────────────────

async function processTicker(ticker, earningsDate, dias, pares, memory) {
  const [companyInfo, epsData, noticias, revisions, edgar8k] = await Promise.all([
    skillGetCompanyInfo(ticker).catch(e => ({ nombre: ticker, error: e.message })),
    skillGetEpsHistory(ticker).catch(e => ({ quarters: [], beatStreak: 0, error: e.message })),
    skillGetNews(ticker).catch(() => []),
    skillGetEstimateRevisions(ticker).catch(() => ({ available: false, trend: "unknown" })),
    skillGetLatest8K(ticker, 3).catch(() => ({ available: false })),
  ]);

  const supplyChain = getSupplyChainExposure(ticker, pares);

  const [impliedMove, histMoves] = await Promise.all([
    skillGetImpliedMove(ticker, earningsDate).catch(e => ({ available: false, error: e.message })),
    skillGetHistoricalMoves(ticker, epsData.quarters || []).catch(() => ({ available: false, moves: [] })),
  ]);

  const updatedMemory = updateEpsMemory(memory, ticker, epsData);

  const briefing = await generateBriefingWithClaude(
    ticker, earningsDate, dias, companyInfo, epsData, supplyChain,
    updatedMemory, impliedMove, histMoves, revisions, edgar8k,
  );

  return {
    ticker, nombre: companyInfo.nombre || ticker, earningsDate,
    diasRestantes: dias,
    epsNextQ: epsData.epsNextQ, epsLow: epsData.epsLow, epsHigh: epsData.epsHigh,
    revNextQ: epsData.revNextQ, beatStreak: epsData.beatStreak || 0,
    quarters: epsData.quarters || [],
    supplyChain, noticias, impliedMove, histMoves, revisions,
    edgar8k: edgar8k?.available
      ? { available: true, filingDate: edgar8k.filingDate, edgarUrl: edgar8k.edgarUrl }
      : { available: false },
    briefing,
    generadoEn: new Date().toISOString(),
  };
}

async function runBriefingAgent({ days = 7, ticker = null } = {}) {
  const pares  = JSON.parse(fs.readFileSync(PARES_FILE, "utf8")).pares;
  const memory = loadEpsMemory();

  let pending;
  if (ticker) {
    const tu = ticker.toUpperCase();
    if (fs.existsSync(EARNINGS_FILE)) {
      const ef  = JSON.parse(fs.readFileSync(EARNINGS_FILE, "utf8"));
      const match = (ef.earnings || []).find(e => e.ticker === tu);
      pending = match ? [match] : [{ ticker: tu, proximoEarnings: "—", diasRestantes: 0 }];
    } else {
      pending = [{ ticker: tu, proximoEarnings: "—", diasRestantes: 0 }];
    }
  } else {
    pending = getEarningsCalendar(days);
  }

  if (!pending.length) return { briefings: [], message: `No earnings in next ${days} days` };

  // Load existing briefings
  let existing = {};
  if (fs.existsSync(OUT_FILE)) {
    try {
      const prev = JSON.parse(fs.readFileSync(OUT_FILE, "utf8"));
      for (const b of (prev.briefings || [])) existing[b.ticker] = b;
    } catch {}
  }

  const today = new Date().toISOString().slice(0, 10);
  const results = [];

  for (const e of pending) {
    const t    = e.ticker;
    const date = e.proximoEarnings || "—";
    const dias = e.diasRestantes   || 0;

    // Skip if already generated today
    const ex = existing[t];
    if (ex && (ex.generadoEn || "").startsWith(today)) {
      results.push(ex);
      continue;
    }

    try {
      const result = await processTicker(t, date, dias, pares, memory);
      results.push(result);
      existing[t] = result;
    } catch(err) {
      console.error(`[briefing ${t}] ${err.message}`);
    }
  }

  // Keep still-relevant briefings from previous runs
  for (const [t, b] of Object.entries(existing)) {
    if (!results.find(r => r.ticker === t)) {
      const d = b.earningsDate || "";
      if (d && d >= today) results.push(b);
    }
  }

  results.sort((a, b) => (a.earningsDate || "").localeCompare(b.earningsDate || ""));

  const output = {
    actualizadoEn: new Date().toISOString(),
    ventanaDias: days, total: results.length, briefings: results,
  };
  fs.writeFileSync(OUT_FILE, JSON.stringify(output, null, 2), "utf8");
  return output;
}

module.exports = { runBriefingAgent };
