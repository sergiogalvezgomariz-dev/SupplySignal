/**
 * company_intel_js.js — Company Intelligence Aggregator (Node.js version)
 *
 * Equivalent of company_intel_agent.py but runs natively in Node/Vercel.
 * Sources: Yahoo Finance (yahoo-finance2) + SEC EDGAR (https) + pares.json
 * Cache: output/company_intel/{TICKER}.json with 24h TTL
 */

"use strict";

const fs   = require("fs");
const path = require("path");
const https = require("https");

const PARES_FILE  = path.join(__dirname, "pares.json");
const INTEL_DIR   = path.join(__dirname, "output", "company_intel");
const CACHE_HOURS = 24;
const EDGAR_UA    = "SupplySignal research@supplysignal.com";

if (!fs.existsSync(INTEL_DIR)) fs.mkdirSync(INTEL_DIR, { recursive: true });

// ── Helpers ──────────────────────────────────────────────────────────────────

function fmtNum(v, prefix = "$", suffix = "") {
  if (v == null) return "—";
  v = parseFloat(v);
  if (isNaN(v)) return "—";
  if (Math.abs(v) >= 1e12) return `${prefix}${(v / 1e12).toFixed(2)}T${suffix}`;
  if (Math.abs(v) >= 1e9)  return `${prefix}${(v / 1e9).toFixed(2)}B${suffix}`;
  if (Math.abs(v) >= 1e6)  return `${prefix}${(v / 1e6).toFixed(1)}M${suffix}`;
  return `${prefix}${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}${suffix}`;
}

function fmtPct(v) {
  if (v == null) return "—";
  return `${(parseFloat(v) * 100).toFixed(1)}%`;
}

function httpsGet(url, headers = {}) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const opts = {
      hostname: u.hostname,
      path:     u.pathname + u.search,
      headers:  { "User-Agent": EDGAR_UA, "Accept": "application/json", ...headers },
    };
    https.get(opts, res => {
      let data = "";
      res.on("data", c => data += c);
      res.on("end", () => {
        try { resolve(JSON.parse(data)); }
        catch { resolve(data); }
      });
    }).on("error", reject);
  });
}

// ── Skill 1: Yahoo Finance ────────────────────────────────────────────────────

async function getYahooFinancials(ticker) {
  // yahoo-finance2 may not be installed — handle gracefully
  let yf;
  try {
    const yf2 = require("yahoo-finance2");
    yf = new yf2.default({ suppressNotices: ["yahooSurvey"] });
  } catch { yf = null; }

  if (!yf) return { source: "Yahoo Finance", error: "yahoo-finance2 not available", nombre: ticker };

  let quote = {}, summary = {}, news = [], history = [];

  try {
    quote = await yf.quote(ticker) || {};
  } catch {}

  try {
    summary = await yf.quoteSummary(ticker, {
      modules: ["summaryProfile", "financialData", "defaultKeyStatistics",
                "earnings", "calendarEvents", "upgradeDowngradeHistory"],
    }) || {};
  } catch {}

  try {
    const rawNews = await yf.search(ticker, { newsCount: 6, quotesCount: 0 }) || {};
    news = (rawNews.news || []).slice(0, 6).map(n => ({
      title:    n.title || "",
      url:      n.link  || "",
      provider: n.publisher || "",
      date:     n.providerPublishTime
                  ? new Date(n.providerPublishTime * 1000).toISOString().slice(0, 10)
                  : "",
    }));
  } catch {}

  const prof = summary.summaryProfile   || {};
  const fin  = summary.financialData    || {};
  const ks   = summary.defaultKeyStatistics || {};
  const cal  = summary.calendarEvents   || {};
  const earn = summary.earnings         || {};

  const price     = quote.regularMarketPrice || quote.previousClose;
  const prevClose = quote.regularMarketPreviousClose || quote.previousClose;
  const chgPct    = price && prevClose
    ? Math.round(((price - prevClose) / prevClose) * 10000) / 100
    : null;

  // EPS quarterly history
  const quarters = [];
  const qHistory = earn.earningsChart?.quarterly || [];
  for (const q of qHistory.slice(-4)) {
    quarters.push({
      quarter:     q.date || "",
      epsActual:   q.actual   != null ? parseFloat(q.actual.toFixed(3))   : null,
      epsEstimate: q.estimate != null ? parseFloat(q.estimate.toFixed(3)) : null,
      surprisePct: (q.actual != null && q.estimate != null && q.estimate !== 0)
                   ? parseFloat(((q.actual - q.estimate) / Math.abs(q.estimate) * 100).toFixed(1))
                   : null,
      beat:        (q.actual != null && q.estimate != null) ? q.actual > q.estimate : null,
    });
  }

  // Next earnings date
  const earningsDates = cal.earnings?.earningsDate || [];
  const earningsDate  = earningsDates.length
    ? (typeof earningsDates[0] === "object" ? earningsDates[0].toISOString?.().slice(0, 10) : String(earningsDates[0]).slice(0, 10))
    : null;

  return {
    source:       "Yahoo Finance",
    sourceUrl:    `https://finance.yahoo.com/quote/${ticker}`,
    nombre:       quote.longName || quote.shortName || ticker,
    sector:       prof.sector    || "—",
    industria:    prof.industry  || "—",
    pais:         prof.country   || "—",
    descripcion:  (prof.longBusinessSummary || "").slice(0, 800),
    precio:       price != null ? Math.round(price * 100) / 100 : null,
    changePct:    chgPct,
    currency:     quote.currency || "USD",
    marketState:  quote.marketState || "UNKNOWN",
    marketCap:    quote.marketCap,
    marketCapFmt: fmtNum(quote.marketCap),
    enterpriseValue: ks.enterpriseValue?.raw ?? null,
    evFmt:        fmtNum(ks.enterpriseValue?.raw),
    revenue:      fin.totalRevenue?.raw ?? null,
    revenueFmt:   fmtNum(fin.totalRevenue?.raw),
    ebitda:       fin.ebitda?.raw ?? null,
    ebitdaFmt:    fmtNum(fin.ebitda?.raw),
    netIncome:    fin.netIncomeToCommon?.raw ?? null,
    netIncomeFmt: fmtNum(fin.netIncomeToCommon?.raw),
    grossMargin:  fmtPct(fin.grossMargins?.raw),
    opMargin:     fmtPct(fin.operatingMargins?.raw),
    netMargin:    fmtPct(fin.profitMargins?.raw),
    peRatio:      quote.trailingPE   != null ? Math.round(quote.trailingPE * 10) / 10 : null,
    fwdPE:        quote.forwardPE    != null ? Math.round(quote.forwardPE * 10) / 10 : null,
    pbRatio:      ks.priceToBook?.raw != null ? Math.round(ks.priceToBook.raw * 100) / 100 : null,
    evEbitda:     ks.enterpriseToEbitda?.raw != null ? Math.round(ks.enterpriseToEbitda.raw * 10) / 10 : null,
    roe:          fmtPct(fin.returnOnEquity?.raw),
    roa:          fmtPct(fin.returnOnAssets?.raw),
    debtEquity:   ks.debtToEquity?.raw != null ? Math.round(ks.debtToEquity.raw / 100 * 100) / 100 : null,
    currentRatio: fin.currentRatio?.raw != null ? Math.round(fin.currentRatio.raw * 100) / 100 : null,
    beta:         quote.beta != null ? Math.round(quote.beta * 100) / 100 : null,
    w52High:      quote.fiftyTwoWeekHigh,
    w52Low:       quote.fiftyTwoWeekLow,
    avgVolume:    quote.averageVolume,
    employees:    prof.fullTimeEmployees,
    epsTrailing:  quote.epsTrailingTwelveMonths,
    epsForward:   quote.epsForward,
    earningsDate: earningsDate,
    dividendYield:fmtPct(quote.dividendYield),
    quarters,
    news,
  };
}

// ── Skill 2: SEC EDGAR ────────────────────────────────────────────────────────

async function getEdgarFilings(ticker) {
  let tickersData;
  try {
    tickersData = await httpsGet("https://www.sec.gov/files/company_tickers.json");
  } catch {
    return { available: false, error: "Could not reach SEC EDGAR" };
  }

  if (typeof tickersData !== "object" || Array.isArray(tickersData))
    return { available: false, error: "Unexpected EDGAR response" };

  let cik = null, companyName = ticker;
  for (const entry of Object.values(tickersData)) {
    if ((entry.ticker || "").toUpperCase() === ticker.toUpperCase()) {
      cik = parseInt(entry.cik_str);
      companyName = entry.title || ticker;
      break;
    }
  }
  if (!cik) return { available: false, error: `Ticker ${ticker} not found in EDGAR` };

  const cikPadded = String(cik).padStart(10, "0");
  let subs;
  try {
    subs = await httpsGet(`https://data.sec.gov/submissions/CIK${cikPadded}.json`);
  } catch {
    return { available: false, error: "Could not fetch submissions" };
  }

  const recent   = (subs.filings || {}).recent || {};
  const forms    = recent.form              || [];
  const dates    = recent.filingDate        || [];
  const accNums  = recent.accessionNumber   || [];
  const primary  = recent.primaryDocument   || [];
  const descs    = recent.primaryDocDescription || [];

  const targetForms = new Set(["10-K", "10-Q", "8-K", "DEF 14A", "S-1"]);
  const filings = [];

  for (let i = 0; i < forms.length && filings.length < 15; i++) {
    if (!targetForms.has(forms[i])) continue;
    const acc    = (accNums[i] || "").replace(/-/g, "");
    const doc    = primary[i] || "";
    const docUrl = doc ? `https://www.sec.gov/Archives/edgar/data/${cik}/${acc}/${doc}` : "";
    filings.push({
      form:        forms[i],
      date:        dates[i]  || "",
      description: descs[i]  || "",
      docUrl,
      indexUrl: `https://www.sec.gov/Archives/edgar/data/${cik}/${acc}/`,
      accession: accNums[i],
    });
  }

  return {
    available:   true,
    cik,
    cikPadded,
    companyName,
    companyPage: `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=${cik}&type=&dateb=&owner=include&count=40`,
    filings,
    latestByType: filings.reduce((acc, f) => { if (!acc[f.form]) acc[f.form] = f; return acc; }, {}),
  };
}

// ── Skill 3: Supply Chain ─────────────────────────────────────────────────────

function getSupplyChain(ticker, pares) {
  const asCustomer = pares.filter(p => p.cliente === ticker && p.proveedor);
  const asSupplier = pares.filter(p => p.proveedor === ticker);

  return {
    suppliers: asCustomer.map(p => ({
      ticker:      p.proveedor,
      nombre:      p.proveedorNombre,
      dependencia: p.dependencia,
      lag:         p.lag,
      fuente:      p.fuente || "Manual",
      yahooUrl:    `https://finance.yahoo.com/quote/${p.proveedor}`,
    })),
    customers: asSupplier.map(p => ({
      ticker:      p.cliente,
      nombre:      p.clienteNombre,
      dependencia: p.dependencia,
      lag:         p.lag,
      fuente:      p.fuente || "Manual",
      yahooUrl:    `https://finance.yahoo.com/quote/${p.cliente}`,
    })),
    totalRelaciones: asCustomer.length + asSupplier.length,
    inPares: asCustomer.length + asSupplier.length > 0,
  };
}

// ── Main pipeline ─────────────────────────────────────────────────────────────

async function buildCompanyIntel(ticker) {
  ticker = ticker.toUpperCase().trim();

  const pares = JSON.parse(fs.readFileSync(PARES_FILE, "utf8")).pares;

  const [yfData, edgarData] = await Promise.all([
    getYahooFinancials(ticker).catch(e => ({ source: "Yahoo Finance", error: e.message, nombre: ticker })),
    getEdgarFilings(ticker).catch(e => ({ available: false, error: e.message })),
  ]);

  const scData = getSupplyChain(ticker, pares);

  const now     = new Date();
  const expires = new Date(now.getTime() + CACHE_HOURS * 3600 * 1000);

  const result = {
    ticker,
    generadoEn:   now.toISOString(),
    cacheExpires: expires.toISOString(),
    financials:   yfData,
    edgar:        edgarData,
    supplyChain:  scData,
  };

  const outFile = path.join(INTEL_DIR, `${ticker}.json`);
  fs.writeFileSync(outFile, JSON.stringify(result, null, 2), "utf8");
  return result;
}

function cacheIsFresh(ticker) {
  const f = path.join(INTEL_DIR, `${ticker}.json`);
  if (!fs.existsSync(f)) return false;
  try {
    const data = JSON.parse(fs.readFileSync(f, "utf8"));
    return data.cacheExpires && new Date(data.cacheExpires) > new Date();
  } catch { return false; }
}

module.exports = { buildCompanyIntel, cacheIsFresh, INTEL_DIR };
