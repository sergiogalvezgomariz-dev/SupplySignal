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

const express       = require("express");
const path          = require("path");
const fs            = require("fs");
const cookieSession = require("cookie-session");
const helmet        = require("helmet");
const rateLimit     = require("express-rate-limit");

const app = express();

// ── Seguridad: cabeceras HTTP ─────────────────────────────────────────────
app.use(helmet({
  contentSecurityPolicy: false, // CSP manual más abajo si se necesita
  crossOriginEmbedderPolicy: false,
}));

// ── Rate limiting en /api/* ───────────────────────────────────────────────
app.use("/api/", rateLimit({
  windowMs: 60_000,
  max: 60,
  standardHeaders: true,
  legacyHeaders: false,
  message: { error: "Too many requests, please try again in a minute." },
}));

// Vercel actúa como proxy — necesario para cookies secure
app.set("trust proxy", 1);

// ── Sesión en cookie (funciona en serverless sin almacenamiento externo) ──
app.use(cookieSession({
  name: "ss_session",
  keys: [process.env.SESSION_SECRET || "supplysignal_dev_secret"],
  maxAge: 8 * 60 * 60 * 1000,  // 8 horas
  httpOnly: true,
  secure: !!process.env.VERCEL,
  sameSite: "lax",
}));

app.use(express.urlencoded({ extended: false }));
app.use(express.json());

// ── Login page ────────────────────────────────────────────────────────────
const LOGIN_HTML = `<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SupplySignal — Access</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;600&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:'IBM Plex Sans',sans-serif;background:#001F5B;min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:24px}
.card{background:#fff;width:100%;max-width:380px;padding:40px 36px 36px;box-shadow:0 20px 60px rgba(0,0,0,.3)}
.top-accent{height:4px;background:#C8962A;margin:-40px -36px 32px;width:calc(100% + 72px)}
.logo{font-family:'Playfair Display',serif;font-size:22px;font-weight:600;color:#001F5B;letter-spacing:.3px;margin-bottom:4px}
.sub{font-size:10px;font-weight:500;letter-spacing:2px;text-transform:uppercase;color:#8A95A8;margin-bottom:32px}
label{display:block;font-size:10.5px;font-weight:600;letter-spacing:.8px;text-transform:uppercase;color:#4F5A6E;margin-bottom:6px}
input{width:100%;border:1px solid #CDD2DC;padding:11px 14px;font-family:inherit;font-size:13px;color:#1E2533;outline:none;margin-bottom:18px;transition:border-color .15s}
input:focus{border-color:#001F5B}
.btn{width:100%;background:#001F5B;color:#fff;border:none;padding:13px;font-family:inherit;font-size:11px;font-weight:600;letter-spacing:1.2px;text-transform:uppercase;cursor:pointer;transition:background .15s;margin-top:4px}
.btn:hover{background:#002A7A}
.err{background:#FAEAEA;border-left:3px solid #B81C1C;color:#B81C1C;font-size:12px;padding:10px 14px;margin-bottom:18px;display:none}
.err.show{display:block}
.footer{margin-top:28px;font-size:10px;color:rgba(255,255,255,.4);text-align:center;letter-spacing:.3px}
@media(max-width:420px){.card{padding:32px 24px 28px}.top-accent{margin:-32px -24px 28px;width:calc(100% + 48px)}}
</style>
</head>
<body>
<div class="card">
  <div class="top-accent"></div>
  <div class="logo">SupplySignal</div>
  <div class="sub">Research Platform</div>
  <div class="err" id="err">INVALID_MSG</div>
  <form method="POST" action="/login">
    <label>Username</label>
    <input type="text" name="username" autocomplete="username" required autofocus>
    <label>Password</label>
    <input type="password" name="password" autocomplete="current-password" required>
    <button class="btn" type="submit">Sign In</button>
  </form>
</div>
<div class="footer">For authorized use only &nbsp;·&nbsp; SupplySignal Research Platform</div>
<script>
const p = new URLSearchParams(location.search);
if(p.get('err')){const e=document.getElementById('err');e.textContent='Incorrect username or password.';e.classList.add('show');}
</script>
</body>
</html>`;

app.get("/login", (req, res) => {
  if (req.session.auth) return res.redirect("/");
  res.send(LOGIN_HTML);
});

app.post("/login", (req, res) => {
  const { username, password } = req.body;
  const validUser = process.env.AUTH_USER || "admin";
  const validPass = process.env.AUTH_PASS || "changeme";
  if (username === validUser && password === validPass) {
    req.session.auth = true;
    return res.redirect("/");
  }
  res.redirect("/login?err=1");
});

app.get("/logout", (req, res) => {
  req.session = null;
  res.redirect("/login");
});

// ── Middleware de autenticación ───────────────────────────────────────────
function requireAuth(req, res, next) {
  if (req.session.auth) return next();
  if (req.path.startsWith("/api/")) return res.status(401).json({ error: "Unauthorized" });
  res.redirect("/login");
}

app.use(requireAuth);

// ── Archivos estáticos (solo tras autenticación) ──────────────────────────
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

// ── Arranque: primera carga y loops (solo en entorno local) ───────────────
const esVercel = !!process.env.VERCEL;

if (!esVercel) {
  (async () => {
    console.log(`Cargando precios iniciales para ${todosLosTickers.length} tickers...`);
    await actualizarPrecios();
    calcularSenales();
    console.log("✅ Precios cargados. Loops activos (precios: 1min · señales: 1h).\n");

    setInterval(async () => {
      await actualizarPrecios();
      if (Date.now() - ultimaRefreshSenales >= INTERVALO_SENALES) calcularSenales();
    }, INTERVALO_PRECIOS);

    setInterval(calcularSenales, INTERVALO_SENALES);
  })();
}

// ── Carga on-demand para Vercel (caché de 2 min) ─────────────────────────
const CACHE_TTL = 2 * 60 * 1000; // 2 minutos
let cargandoPrecios = false;

async function asegurarPrecios() {
  const yaRecientes = Date.now() - ultimaRefreshSenales < CACHE_TTL;
  if (yaRecientes || cargandoPrecios) return;
  cargandoPrecios = true;
  try {
    await actualizarPrecios();
    calcularSenales();
  } finally {
    cargandoPrecios = false;
  }
}

// ── Endpoints ─────────────────────────────────────────────────────────────

// Señales activas
app.get("/api/signals", async (req, res) => {
  await asegurarPrecios();
  if (!cacheSenales) return res.json({ generadoEn: null, umbralShock: UMBRAL_SHOCK, senales: [], errores: ["Cargando datos iniciales..."] });
  res.json(cacheSenales);
});

// Precios en vivo de todos los tickers seguidos
app.get("/api/prices", async (req, res) => {
  await asegurarPrecios();
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
  if (!/^[A-Z0-9.\-]{1,10}$/.test(cliente)) return res.status(400).json({ error: "Invalid ticker" });
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

// Servir solo el HTML del informe (no todo el directorio output/)
app.get("/report/informe_senales.html", (req, res) => {
  const ruta = path.join(__dirname, "output", "informe_senales.html");
  if (!fs.existsSync(ruta)) return res.status(404).send("Report not yet generated.");
  res.sendFile(ruta);
});

// Briefings del agente de earnings
app.get("/api/briefings", (req, res) => {
  const ruta = path.join(__dirname, "output", "briefings.json");
  if (!fs.existsSync(ruta)) return res.json({ briefings: [], mensaje: "No briefings yet" });
  try { res.json(JSON.parse(fs.readFileSync(ruta, "utf8"))); }
  catch(e) { res.status(500).json({ error: "Failed to parse briefings" }); }
});

// Earnings calendar
app.get("/api/earnings", (req, res) => {
  const ruta = path.join(__dirname, "output", "earnings.json");
  if (!fs.existsSync(ruta)) return res.json({ earnings: [], mensaje: "Ejecuta: python earnings_bot.py" });
  try { res.json(JSON.parse(fs.readFileSync(ruta, "utf8"))); }
  catch(e) { res.status(500).json({ error: "Failed to parse earnings" }); }
});

// Correlaciones históricas
app.get("/api/correlaciones", (req, res) => {
  const ruta = path.join(__dirname, "output", "analisis_correlacion.json");
  if (!fs.existsSync(ruta)) return res.json({ datos: [], mensaje: "Ejecuta primero: python ampliar_pares.py" });
  try { res.json({ datos: JSON.parse(fs.readFileSync(ruta, "utf8")) }); }
  catch(e) { res.status(500).json({ error: "Failed to parse correlaciones" }); }
});

// Company Intel: agrega Yahoo Finance + SEC EDGAR + supply chain con caché 24h
app.get("/api/company/:ticker", async (req, res) => {
  const ticker = req.params.ticker.toUpperCase();
  if (!/^[A-Z0-9.\-]{1,10}$/.test(ticker))
    return res.status(400).json({ error: "Invalid ticker" });

  const cacheFile = path.join(__dirname, "output", "company_intel", `${ticker}.json`);

  // Sirve caché si es válida (< 24h)
  if (fs.existsSync(cacheFile)) {
    try {
      const cached = JSON.parse(fs.readFileSync(cacheFile, "utf8"));
      const expires = cached.cacheExpires;
      if (expires && new Date(expires) > new Date()) {
        cached._fromCache = true;
        return res.json(cached);
      }
    } catch(e) { /* cache corrupta, recalcula */ }
  }

  // Caché expirada o inexistente → ejecuta el agente Python
  res.setHeader("Content-Type", "application/json");
  const { spawn } = require("child_process");
  const proc = spawn("python", [
    path.join(__dirname, "company_intel_agent.py"),
    "--ticker", ticker,
  ], { cwd: __dirname, stdio: ["ignore", "pipe", "pipe"] });

  let out = "", err = "";
  proc.stdout.on("data", d => { out += d; });
  proc.stderr.on("data", d => { err += d; });
  proc.on("close", code => {
    if (code !== 0) {
      return res.status(500).json({ error: "Agent failed", detail: err.slice(0, 300) });
    }
    try {
      const data = JSON.parse(fs.readFileSync(cacheFile, "utf8"));
      data._fromCache = false;
      res.json(data);
    } catch(e) {
      res.status(500).json({ error: "Could not read output file" });
    }
  });
});

// Postmortem del agente post-earnings
app.get("/api/postmortem", (req, res) => {
  const ruta = path.join(__dirname, "output", "postmortem.json");
  if (!fs.existsSync(ruta)) return res.json({ postmortems: [], mensaje: "No postmortems yet. Run: python earnings_postmortem_agent.py" });
  try { res.json(JSON.parse(fs.readFileSync(ruta, "utf8"))); }
  catch(e) { res.status(500).json({ error: "Failed to parse postmortem" }); }
});

// ── 404 ───────────────────────────────────────────────────────────────────
app.use((req, res) => res.status(404).json({ error: "Not found" }));

const PORT = process.env.PORT || 3100;
app.listen(PORT, () => {
  console.log(`\n✅ SupplySignal en http://localhost:${PORT}`);
  console.log(`   Precios: cada 1 minuto · Señales: cada 1 hora\n`);
});
