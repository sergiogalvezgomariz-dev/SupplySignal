require("dotenv").config();
// Fallback: lee .env manualmente por si dotenv falla con BOM (Windows)
try {
  const envContent = require("fs").readFileSync(require("path").join(__dirname, ".env"), "utf8").replace(/^﻿/, "");
  for (const line of envContent.split(/\r?\n/)) {
    const eq = line.indexOf("=");
    if (eq > 0 && !line.startsWith("#")) {
      const k = line.slice(0, eq).trim();
      const v = line.slice(eq + 1).trim();
      if (!process.env[k]) process.env[k] = v;
    }
  }
} catch(e) {}
const express = require("express");
const path    = require("path");
const fs      = require("fs");

const app = express();
app.use(express.static(path.join(__dirname, "public")));

// ── Configuración ─────────────────────────────────────────────────────────
const UMBRAL_SHOCK       = 3;        // % variación diaria para activar señal
const INTERVALO_PRECIOS  = 60_000;   // refresca precios cada 1 minuto
const INTERVALO_SENALES  = 3_600_000;// recalcula señales cada 1 hora
const UA = "Mozilla/5.0 (SupplySignal research@supplysignal.com)";

const pares = JSON.parse(fs.readFileSync(path.join(__dirname, "pares.json"), "utf8")).pares;

// Extrae todos los tickers únicos (clientes + proveedores con ticker válido)
const todosLosTickers = [...new Set([
  ...pares.map(p => p.cliente),
  ...pares.filter(p => p.proveedor).map(p => p.proveedor),
])].filter(t => t && t.length > 0);

// ── Estado en memoria ─────────────────────────────────────────────────────
let preciosVivos   = {};   // { AAPL: { precio, variacionPct, ultimaActualizacion } }
let cacheSenales   = null;
let ultimaRefreshSenales = 0;

// ── Yahoo Finance: precio actual (quote en tiempo real o retardado 15min) ─
async function fetchPrecio(ticker) {
  const url = `https://query1.finance.yahoo.com/v8/finance/chart/${ticker}?range=1d&interval=1m`;
  const r = await fetch(url, { headers: { "User-Agent": UA } });
  if (!r.ok) throw new Error(`Yahoo ${r.status}`);
  const data   = await r.json();
  const result = data.chart?.result?.[0];
  if (!result) throw new Error("Sin resultado");

  const meta   = result.meta;
  const precio = meta.regularMarketPrice ?? meta.chartPreviousClose;
  const cierre = meta.chartPreviousClose  ?? meta.previousClose;
  const variacionPct = cierre ? ((precio - cierre) / cierre) * 100 : 0;

  return {
    precio:         Math.round(precio * 100) / 100,
    cierre:         Math.round(cierre * 100) / 100,
    variacionPct:   Math.round(variacionPct * 1000) / 1000,
    nombre:         meta.shortName || meta.longName || ticker,
    moneda:         meta.currency || "USD",
    marketState:    meta.marketState || "UNKNOWN",
    ultimaActualizacion: new Date().toISOString(),
  };
}

// ── Actualiza todos los precios (se llama cada minuto) ────────────────────
async function actualizarPrecios() {
  const ahora = new Date().toLocaleTimeString("es-ES");
  process.stdout.write(`\r[${ahora}] Actualizando ${todosLosTickers.length} tickers...`);

  for (const ticker of todosLosTickers) {
    try {
      preciosVivos[ticker] = await fetchPrecio(ticker);
    } catch (e) {
      // mantiene el precio anterior si falla
      if (!preciosVivos[ticker]) {
        preciosVivos[ticker] = { precio: null, variacionPct: null, error: e.message, ultimaActualizacion: new Date().toISOString() };
      }
    }
    await new Promise(r => setTimeout(r, 120)); // 120ms entre peticiones
  }
  process.stdout.write(`  OK\n`);
}

// ── Recalcula señales a partir de preciosVivos (se llama cada hora) ───────
function calcularSenales() {
  const senales = [];

  for (const par of pares) {
    if (!par.proveedor) continue;
    const datosCli  = preciosVivos[par.cliente];
    const datosProv = preciosVivos[par.proveedor];
    if (!datosCli || datosCli.variacionPct === null) continue;

    const shock = Math.abs(datosCli.variacionPct) >= UMBRAL_SHOCK;
    if (!shock) continue;

    const direccion  = datosCli.variacionPct > 0 ? "COMPRA" : "VENTA";
    const puntuacion = Math.round(Math.abs(datosCli.variacionPct) * par.dependencia) / 10;

    senales.push({
      accion:          direccion,
      proveedor:       par.proveedor,
      proveedorNombre: par.proveedorNombre,
      cliente:         par.cliente,
      clienteNombre:   par.clienteNombre,
      variacionCliente:Math.round(datosCli.variacionPct * 100) / 100,
      precioProveedor: datosProv?.precio ?? null,
      variacionProveedor: datosProv?.variacionPct ?? null,
      dependencia:     par.dependencia,
      lag:             par.lag,
      puntuacion,
      fuente:          par.fuente || "Manual",
      explicacion:
        direccion === "COMPRA"
          ? `${par.clienteNombre} ha subido un ${datosCli.variacionPct.toFixed(1)}% hoy. ${par.proveedorNombre} depende de ${par.clienteNombre} en ~${par.dependencia}% de sus ingresos y históricamente reacciona con ${par.lag} días de retardo.`
          : `${par.clienteNombre} ha caído un ${Math.abs(datosCli.variacionPct).toFixed(1)}% hoy. ${par.proveedorNombre} (~${par.dependencia}% de ingresos ligados a ${par.clienteNombre}) podría caer en los próximos ${par.lag} días.`,
    });
  }

  senales.sort((a, b) => b.puntuacion - a.puntuacion);
  cacheSenales = {
    generadoEn:  new Date().toISOString(),
    umbralShock: UMBRAL_SHOCK,
    senales,
    errores: [],
  };
  ultimaRefreshSenales = Date.now();
  if (senales.length > 0) {
    console.log(`[Señales] ${senales.length} señal(es) activa(s) a las ${new Date().toLocaleTimeString("es-ES")}`);
  }
  // Genera informe automáticamente tras cada recálculo de señales
  generarInformeBackground();
}

// Estado del informe: { generadoEn, disponible, url }
let estadoInforme = { generadoEn: null, disponible: false, url: null };

function generarInformeBackground() {
  const { spawn } = require("child_process");
  const proc = spawn("python", [path.join(__dirname, "generar_informe.py")], {
    cwd: __dirname,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stderr = "";
  proc.stderr.on("data", d => { stderr += d; });
  proc.stdout.on("data", d => process.stdout.write(d));
  proc.on("close", code => {
    if (code === 0) {
      estadoInforme = {
        generadoEn: new Date().toISOString(),
        disponible: true,
        url: "/report/informe_senales.html",
      };
      console.log(`[Informe] Generado correctamente → output/informe_senales.html`);
    } else {
      console.error(`[Informe] Error al generar: ${stderr.slice(0, 200)}`);
    }
  });
}

// ── Arranque: primera carga y loops ───────────────────────────────────────
(async () => {
  console.log(`Cargando precios iniciales para ${todosLosTickers.length} tickers...`);
  await actualizarPrecios();
  calcularSenales();
  console.log("✅ Precios cargados. Loops activos (precios: 1min · señales: 1h).\n");

  setInterval(async () => {
    await actualizarPrecios();
    // Las señales se recalculan cada hora O si llevan más de 1h sin calcular
    if (Date.now() - ultimaRefreshSenales >= INTERVALO_SENALES) calcularSenales();
  }, INTERVALO_PRECIOS);

  // También recalcula señales en el intervalo horario independientemente
  setInterval(calcularSenales, INTERVALO_SENALES);
})();

// ── Endpoints ─────────────────────────────────────────────────────────────

// Señales activas
app.get("/api/signals", (req, res) => {
  if (!cacheSenales) return res.json({ generadoEn: null, umbralShock: UMBRAL_SHOCK, senales: [], errores: ["Cargando datos iniciales..."] });
  res.json(cacheSenales);
});

// Precios en vivo de todos los tickers seguidos
app.get("/api/prices", (req, res) => {
  // Enriquece con info de los pares: qué rol tiene cada ticker
  const clientes   = new Set(pares.map(p => p.cliente));
  const resultado  = {};
  for (const [ticker, datos] of Object.entries(preciosVivos)) {
    resultado[ticker] = {
      ...datos,
      esCliente:   clientes.has(ticker),
      // suppliers de este ticker si es cliente
      suppliers: clientes.has(ticker)
        ? pares.filter(p => p.cliente === ticker && p.proveedor).map(p => ({
            ticker:      p.proveedor,
            nombre:      p.proveedorNombre,
            dependencia: p.dependencia,
            lag:         p.lag,
          }))
        : [],
    };
  }
  res.json({
    actualizadoEn: new Date().toISOString(),
    umbralShock: UMBRAL_SHOCK,
    precios: resultado,
  });
});

// Estado del bot
app.get("/api/bot", (req, res) => {
  const diario = path.join(__dirname, "bot_diario.json");
  if (!fs.existsSync(diario)) return res.json({ operaciones: [], modo: "apagado" });
  res.json(JSON.parse(fs.readFileSync(diario, "utf8")));
});

// Serie de precios intradía para gráficas (cliente + sus proveedores)
app.get("/api/chart/:ticker", async (req, res) => {
  const cliente = req.params.ticker.toUpperCase();
  const suppliers = pares
    .filter(p => p.cliente === cliente && p.proveedor)
    .map(p => ({ ticker: p.proveedor, nombre: p.proveedorNombre, dependencia: p.dependencia }));

  const todos = [{ ticker: cliente, nombre: preciosVivos[cliente]?.nombre || cliente, esCliente: true }, ...suppliers.map(s => ({ ...s, esCliente: false }))];

  async function fetchSerie(ticker) {
    const url = `https://query1.finance.yahoo.com/v8/finance/chart/${ticker}?range=1d&interval=5m`;
    const r = await fetch(url, { headers: { "User-Agent": UA } });
    if (!r.ok) return null;
    const data = await r.json();
    const result = data.chart?.result?.[0];
    if (!result) return null;

    const timestamps = result.timestamp || [];
    const closes     = result.indicators?.quote?.[0]?.close || [];
    const meta       = result.meta;
    const apertura   = meta.chartPreviousClose || closes.find(c => c != null);
    if (!apertura) return null;

    const puntos = timestamps
      .map((ts, i) => ({ t: ts * 1000, v: closes[i] }))
      .filter(p => p.v != null)
      .map(p => ({ t: p.t, v: Math.round(p.v * 100) / 100 }));

    return { ticker, nombre: meta.shortName || meta.longName || ticker, puntos, moneda: meta.currency || "USD", marketState: meta.marketState };
  }

  const series = [];
  for (const item of todos) {
    try {
      const serie = await fetchSerie(item.ticker);
      if (serie) series.push({ ...serie, esCliente: item.esCliente, dependencia: item.dependencia || null });
    } catch (e) { /* skip */ }
    await new Promise(r => setTimeout(r, 120));
  }

  res.json({ cliente, actualizadoEn: new Date().toISOString(), series });
});

// Estado del informe (generado automáticamente con las señales)
app.get("/api/report/status", (req, res) => res.json(estadoInforme));

// Servir informes generados
app.use("/report", express.static(path.join(__dirname, "output")));

// Briefings del agente de earnings
app.get("/api/briefings", (req, res) => {
  const ruta = path.join(__dirname, "output", "briefings.json");
  if (!fs.existsSync(ruta)) return res.json({ briefings: [], mensaje: "No briefings yet" });
  res.json(JSON.parse(fs.readFileSync(ruta, "utf8")));
});

// Earnings calendar
app.get("/api/earnings", (req, res) => {
  const ruta = path.join(__dirname, "output", "earnings.json");
  if (!fs.existsSync(ruta)) return res.json({ earnings: [], mensaje: "Ejecuta: python earnings_bot.py" });
  res.json(JSON.parse(fs.readFileSync(ruta, "utf8")));
});

// Correlaciones históricas
app.get("/api/correlaciones", (req, res) => {
  const ruta = path.join(__dirname, "output", "analisis_correlacion.json");
  if (!fs.existsSync(ruta)) return res.json({ datos: [], mensaje: "Ejecuta primero: python ampliar_pares.py" });
  res.json({ datos: JSON.parse(fs.readFileSync(ruta, "utf8")) });
});

const PORT = process.env.PORT || 3100;
app.listen(PORT, () => {
  console.log(`\n✅ SupplySignal en http://localhost:${PORT}`);
  console.log(`   Precios: cada 1 minuto · Señales: cada 1 hora\n`);
});
