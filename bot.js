// SupplySignal Bot — lee las señales del servidor y opera automáticamente.
//
// DOS MODOS:
//  1. Simulación local (por defecto): no necesita nada, apunta las operaciones en bot_diario.json
//  2. Alpaca paper trading: cuenta de práctica gratuita con dinero ficticio en alpaca.markets.
//     Pon en el .env:  ALPACA_KEY=...  ALPACA_SECRET=...
//
// Reglas de seguridad del bot:
//  - Máximo 200 EUR (~220 USD) por operación
//  - Máximo 1 operación por proveedor y día
//  - Solo opera señales con puntuación >= PUNTUACION_MINIMA
require("dotenv").config();
const fs = require("fs");
const path = require("path");

const SERVIDOR = process.env.SIGNALS_URL || "http://localhost:3100";
const MAX_USD_POR_OPERACION = 220;
const PUNTUACION_MINIMA = 1.0;
const CADA_MINUTOS = 15;

const ALPACA_KEY = process.env.ALPACA_KEY;
const ALPACA_SECRET = process.env.ALPACA_SECRET;
const ALPACA_URL = "https://paper-api.alpaca.markets"; // cuenta de práctica, nunca dinero real
const modoAlpaca = Boolean(ALPACA_KEY && ALPACA_SECRET);

const DIARIO = path.join(__dirname, "bot_diario.json");

function leerDiario() {
  if (!fs.existsSync(DIARIO)) return { modo: modoAlpaca ? "alpaca-paper" : "simulación", operaciones: [] };
  return JSON.parse(fs.readFileSync(DIARIO, "utf8"));
}

function guardarDiario(diario) {
  diario.modo = modoAlpaca ? "alpaca-paper" : "simulación";
  diario.ultimaRevision = new Date().toISOString();
  fs.writeFileSync(DIARIO, JSON.stringify(diario, null, 2));
}

function yaOperadoHoy(diario, ticker) {
  const hoy = new Date().toISOString().slice(0, 10);
  return diario.operaciones.some((op) => op.ticker === ticker && op.fecha.startsWith(hoy));
}

// Envía una orden real a la cuenta de práctica de Alpaca
async function ordenAlpaca(ticker, lado, importeUsd) {
  const r = await fetch(`${ALPACA_URL}/v2/orders`, {
    method: "POST",
    headers: {
      "APCA-API-KEY-ID": ALPACA_KEY,
      "APCA-API-SECRET-KEY": ALPACA_SECRET,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      symbol: ticker,
      notional: String(importeUsd),
      side: lado === "COMPRA" ? "buy" : "sell",
      type: "market",
      time_in_force: "day",
    }),
  });
  const respuesta = await r.json();
  if (!r.ok) throw new Error(respuesta.message || `Alpaca respondió ${r.status}`);
  return respuesta.id;
}

async function revisar() {
  console.log(`\n🔎 ${new Date().toLocaleTimeString()} — pidiendo señales al servidor...`);
  let datos;
  try {
    const r = await fetch(`${SERVIDOR}/api/signals`);
    datos = await r.json();
  } catch (e) {
    console.log(`⚠️  No pude hablar con el servidor (${SERVIDOR}). ¿Está arrancado? (node server.js)`);
    return;
  }

  const senales = (datos.senales || []).filter((s) => s.puntuacion >= PUNTUACION_MINIMA);
  if (senales.length === 0) {
    console.log("   Hoy no hay señales fuertes. El bot espera.");
    return;
  }

  const diario = leerDiario();
  for (const s of senales) {
    if (yaOperadoHoy(diario, s.proveedor)) {
      console.log(`   ${s.proveedor}: ya operado hoy, lo salto.`);
      continue;
    }
    // En venta solo vendemos si tenemos el valor (en simulación: si lo compramos antes)
    if (s.accion === "VENTA" && !modoAlpaca) {
      const tieneCompra = diario.operaciones.some((op) => op.ticker === s.proveedor && op.lado === "COMPRA");
      if (!tieneCompra) {
        console.log(`   ${s.proveedor}: señal de venta pero no lo tenemos en cartera. La salto.`);
        continue;
      }
    }

    const operacion = {
      fecha: new Date().toISOString(),
      ticker: s.proveedor,
      nombre: s.proveedorNombre,
      lado: s.accion,
      importeUsd: MAX_USD_POR_OPERACION,
      precioAprox: s.precioProveedor,
      motivo: s.explicacion,
      puntuacion: s.puntuacion,
    };

    try {
      if (modoAlpaca) {
        operacion.ordenId = await ordenAlpaca(s.proveedor, s.accion, MAX_USD_POR_OPERACION);
        console.log(`✅ Orden enviada a Alpaca (práctica): ${s.accion} ${s.proveedor} por $${MAX_USD_POR_OPERACION}`);
      } else {
        console.log(`📝 [SIMULACIÓN] ${s.accion} ${s.proveedor} por $${MAX_USD_POR_OPERACION} — ${s.explicacion}`);
      }
      diario.operaciones.push(operacion);
    } catch (e) {
      console.log(`❌ No se pudo operar ${s.proveedor}: ${e.message}`);
    }
  }
  guardarDiario(diario);
}

console.log("🤖 SupplySignal Bot arrancado");
console.log(`   Modo: ${modoAlpaca ? "Alpaca PAPER TRADING (dinero ficticio)" : "SIMULACIÓN local (sin broker)"}`);
console.log(`   Revisa señales cada ${CADA_MINUTOS} minutos. Límite por operación: $${MAX_USD_POR_OPERACION}.`);
revisar();
setInterval(revisar, CADA_MINUTOS * 60 * 1000);
