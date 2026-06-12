# 📡 SupplySignal

App de research que detecta cuándo una gran empresa (Apple, Tesla, NVIDIA…) se mueve con fuerza
y avisa de qué proveedores suyos podrían reaccionar en los días siguientes — más un bot que
opera automáticamente según esas señales.

## Cómo se usa

1. **Arrancar el servidor de señales**: `node server.js`
2. **Abrir la web**: http://localhost:3100
3. **Arrancar el bot** (en otra ventana): `node bot.js`

## Los dos modos del bot

- **Simulación (por defecto)**: no necesita nada. Las operaciones se apuntan en `bot_diario.json` y se ven en la web.
- **Alpaca paper trading**: cuenta de práctica gratuita con dinero ficticio. Crea tus claves en
  https://alpaca.markets, copia `.env.example` como `.env` y rellénalas. El bot enviará órdenes reales a la cuenta de práctica.

## Reglas de seguridad del bot

- Máximo $220 (~200 EUR) por operación
- Máximo 1 operación por valor y día
- Solo opera señales con fuerza ≥ 1.0
- Nunca toca dinero real (apunta siempre a la cuenta de práctica de Alpaca)

⚠️ Herramienta de research, no asesoramiento financiero (MiFID II).
