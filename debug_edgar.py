"""Debug: por qué SWKS no extrae a Apple."""
import requests, re, html as html_mod

HEADERS = {"User-Agent": "SupplySignal research@supplysignal.com"}
URL = "https://www.sec.gov/Archives/edgar/data/4127/000000412725000085/swks-20251003.htm"

print("Descargando SWKS 10-K...")
r = requests.get(URL, headers=HEADERS, timeout=60)
raw = r.text
print(f"  HTML: {len(raw):,} chars")

clean = re.sub(r"<[^>]{1,500}>", " ", raw)
clean = html_mod.unescape(clean)
clean = re.sub(r"\s+", " ", clean)
print(f"  Texto plano: {len(clean):,} chars")

# 1. ¿Está Apple en el texto?
hits_apple = [m.start() for m in re.finditer(r"Apple", clean)]
print(f"\n  'Apple' aparece {len(hits_apple)} veces")

# 2. ¿Está "accounted for" cerca de Apple?
for h in hits_apple[:5]:
    fragmento = clean[max(0, h-30):h+200]
    print(f"  pos {h}: {repr(fragmento)}")

# 3. Buscar la frase exacta conocida
print("\n--- Buscando 'accounted for' cerca de Apple ---")
patron_simple = re.compile(r"Apple.{0,700}?accounted\s+for\s+([\d\.]+)\s*%", re.DOTALL | re.IGNORECASE)
for m in patron_simple.finditer(clean):
    print(f"MATCH: {repr(m.group()[:200])} -> {m.group(1)}%")

# 4. Mostrar contexto alrededor del primer 'Apple'
if hits_apple:
    h = hits_apple[0]
    print(f"\n--- Contexto (pos {h}) ---")
    print(clean[h:h+800])
