import datetime as dt
import json
import os
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from statistics import mean

import requests
import yfinance as yf

# =======================
# Config & Constantes
# =======================
HDR = {"User-Agent": "finanzas-hoy/1.0 (+github)"}
CNE_BASE = "https://api.cne.cl"  # Bencina en Línea (no usado ahora)
BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
LATEST_JSON = DATA_DIR / "latest.json"

# =======================
# Fecha en español (sin depender de locale del sistema)
# =======================
_DIAS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

def fecha_es_hoy():
    hoy = dt.datetime.now()
    dia = _DIAS_ES[hoy.weekday()].capitalize()
    return f"{dia} {hoy:%d/%m/%Y}"

# =======================
# Helpers de formato
# =======================
def fmt_clp(n):
    try:
        s = f"{round(float(n)):,}".replace(",", ".")
        return f"$ {s}"
    except Exception:
        return "N/D"

def fmt_usd(n, decimals=0):
    if n is None:
        return "N/D"
    try:
        if decimals == 0:
            return f"${round(float(n)):,}"
        return f"${float(n):,.{decimals}f}"
    except Exception:
        return "N/D"

def fmt_float(n, decimals=2):
    if n is None:
        return "N/D"
    try:
        return f"{float(n):.{decimals}f}"
    except Exception:
        return "N/D"

# =======================
# HTTP con reintentos (para red inestable)
# =======================
def http_get(url, *, params=None, headers=None, timeout=20, tries=5, backoff=2.0):
    """
    GET con reintentos exponenciales: 1, 2, 4, 8, ...
    Lanza la última excepción si se agotan.
    """
    headers = headers or HDR
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
            time.sleep(backoff ** i)
    raise last

# =======================
# Fetchers principales
# =======================
def get_mindicador():
    """Dólar, UF, UTM (Chile) desde mindicador.cl"""
    r = http_get("https://mindicador.cl/api", headers=HDR, timeout=20, tries=5, backoff=2.0)
    j = r.json()
    return {
        "fecha": j["fecha"][:10],
        "dolar_clp": float(j["dolar"]["valor"]),
        "uf_clp": float(j["uf"]["valor"]),
        "utm_clp": float(j["utm"]["valor"]),
    }

def get_crypto():
    """BTC y ETH en USD desde CoinGecko (simple/rápido)."""
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {"ids": "bitcoin,ethereum", "vs_currencies": "usd"}
    r = http_get(url, params=params, headers=HDR, timeout=20, tries=4, backoff=2.0)
    j = r.json()
    return {
        "btc_usd": float(j["bitcoin"]["usd"]),
        "eth_usd": float(j["ethereum"]["usd"]),
    }

def get_brent():
    """Brent USD/bbl usando Yahoo Finance."""
    try:
        t = yf.Ticker("BZ=F")  # Brent Crude Oil
        d = t.history(period="1d")
        if d.empty:
            return {"brent_usd": None}
        return {"brent_usd": float(d["Close"].iloc[-1])}
    except Exception:
        return {"brent_usd": None}

def get_cobre_comex():
    """Cobre en USD/lb (aprox) usando futuro de cobre COMEX (HG=F)."""
    try:
        t = yf.Ticker("HG=F")
        d = t.history(period="1d")
        if d.empty:
            return {"cobre_usd_lb": None}
        return {"cobre_usd_lb": float(d["Close"].iloc[-1])}
    except Exception:
        return {"cobre_usd_lb": None}

# =======================
# Lecturas locales/overrides
# =======================
def load_json_if_exists(path: Path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None

def get_cobre_oficial_local():
    """
    sources/cochilco_override.json (opcional):
    { "cobre_usd_lb": 4.12, "fecha": "YYYY-MM-DD", "fuente": "Cochilco (oficial)" }
    """
    src = BASE / "sources" / "cochilco_override.json"
    js = load_json_if_exists(src)
    if not js:
        return None
    return {"cobre_usd_lb": js.get("cobre_usd_lb")}

def get_enap_local():
    """
    sources/enap_semana.json (fuente semanal manual)
    {
      "vigencia": "2025-10-23 a 2025-10-29",
      "g93_clp_l": 1250,
      "g95_clp_l": 1285,
      "g97_clp_l": 1320,
      "diesel_clp_l": 1090
    }

    ✅ Cambio pedido: NO devolvemos ni guardamos vigencia/fecha.
    La razón: evitar que se quede una fecha mala si olvidas actualizarla.
    """
    src = BASE / "sources" / "enap_semana.json"
    js = load_json_if_exists(src)
    if not js:
        return {
            "g93_clp_l": None,
            "g95_clp_l": None,
            "g97_clp_l": None,
            "diesel_clp_l": None,
        }
    return {
        "g93_clp_l": js.get("g93_clp_l"),
        "g95_clp_l": js.get("g95_clp_l"),
        "g97_clp_l": js.get("g97_clp_l"),
        "diesel_clp_l": js.get("diesel_clp_l"),
    }

# =======================
# CNE API (Bencina en Línea) — no se usa (dejado por si vuelve)
# =======================
def _cne_get(path, params=None):
    if params is None:
        params = {}
    api_key = os.getenv("CNE_API_KEY")
    if not api_key:
        raise RuntimeError("Falta CNE_API_KEY en el entorno.")
    params["apikey"] = api_key  # ajusta si tu key exige otro nombre
    r = http_get(f"{CNE_BASE}{path}", params=params, headers=HDR, timeout=30, tries=4, backoff=2.0)
    return r.json()

def get_cne_fuel_averages():
    """
    Promedios nacionales CLP/L para 93/95/97/Diésel desde CNE.
    (No usado actualmente, pero corregido por si se reactiva).

    ✅ Cambio pedido: sin vigencia/fecha.
    """
    precios = {"93": [], "95": [], "97": [], "diesel": []}

    page = 1
    while True:
        data = _cne_get("/combustibles/estaciones", params={"page": page})
        estaciones = data.get("data") or data.get("results") or []
        if not estaciones:
            break

        for est in estaciones:
            for p in est.get("precios", []):
                prod = str(p.get("producto", "")).lower()
                val = p.get("precio")
                if not isinstance(val, (int, float)):
                    continue
                if prod in ("93", "gasolina 93"):
                    precios["93"].append(val)
                elif prod in ("95", "gasolina 95"):
                    precios["95"].append(val)
                elif prod in ("97", "gasolina 97"):
                    precios["97"].append(val)
                elif "díesel" in prod or "diesel" in prod or prod == "d":
                    precios["diesel"].append(val)

        next_page = data.get("next") or data.get("pagination", {}).get("next_page")
        if next_page:
            page += 1
        else:
            break

    promedios = {
        "g93_clp_l": round(mean(precios["93"])) if precios["93"] else None,
        "g95_clp_l": round(mean(precios["95"])) if precios["95"] else None,
        "g97_clp_l": round(mean(precios["97"])) if precios["97"] else None,
        "diesel_clp_l": round(mean(precios["diesel"])) if precios["diesel"] else None,
    }
    return promedios

# =======================
# Noticias automáticas (RSS) + fallback local
# =======================
RSS_FEEDS = [
    "https://feeds.reuters.com/reuters/ESbusinessNews",
    "https://feeds.reuters.com/reuters/ESworldNews",
    "https://news.google.com/rss/search?q=Econom%C3%ADa+Chile&hl=es-419&gl=CL&ceid=CL:es-419",
    "https://news.google.com/rss/search?q=Mercados+Chile&hl=es-419&gl=CL&ceid=CL:es-419",
]

def _clean_title(t):
    if not t:
        return None
    t = t.strip()
    for pref in ("VIDEO:", "Video:", "EN VIVO:", "En vivo:", "FOTO:", "Fotos:"):
        if t.startswith(pref):
            t = t[len(pref):].strip()
    if len(t) > 150:
        t = t[:147].rstrip() + "…"
    return t

def _parse_rss(xml_bytes):
    root = ET.fromstring(xml_bytes)

    # RSS: channel/item/title
    chan = root.find("channel")
    if chan is not None:
        for item in chan.findall("item"):
            title_el = item.find("title")
            if title_el is not None and title_el.text:
                title = _clean_title(title_el.text)
                if title:
                    return {"titular": title}
        return None

    # Atom: feed/entry/title
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for entry in root.findall("atom:entry", ns):
        title_el = entry.find("atom:title", ns)
        if title_el is not None and title_el.text:
            title = _clean_title(title_el.text)
            if title:
                return {"titular": title}
    return None

def get_news_auto():
    """RSS → si falla, news_override.json → si no, N/D"""
    for url in RSS_FEEDS:
        try:
            r = http_get(url, headers=HDR, timeout=15, tries=3, backoff=2.0)
            if r.status_code == 200 and r.content:
                parsed = _parse_rss(r.content)
                if parsed and parsed.get("titular"):
                    return parsed
        except Exception:
            continue

    # Fallback local (opcional)
    src = BASE / "sources" / "news_override.json"
    js = load_json_if_exists(src)
    if js and js.get("titular"):
        return {"titular": js.get("titular")}

    return {"titular": None}

# =======================
# Cache (último JSON bueno)
# =======================
def load_last_ok():
    js = load_json_if_exists(LATEST_JSON)
    return js or {}

def save_latest(data: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_JSON.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

# =======================
# Main
# =======================
def main():
    fecha_hoy = fecha_es_hoy()
    last = load_last_ok()

    # --- Dólar/UF/UTM ---
    try:
        md = get_mindicador()
    except Exception as e:
        print(f"[WARN] mindicador falló: {e}")
        md = {
            "dolar_clp": last.get("dolar_clp"),
            "uf_clp": last.get("uf_clp"),
            "utm_clp": last.get("utm_clp"),
        }

    # --- Crypto ---
    try:
        cr = get_crypto()
    except Exception as e:
        print(f"[WARN] cripto falló: {e}")
        cr = {
            "btc_usd": last.get("btc_usd"),
            "eth_usd": last.get("eth_usd"),
        }

    # --- Brent ---
    br = get_brent()
    if br.get("brent_usd") is None and last.get("brent_usd") is not None:
        br["brent_usd"] = last.get("brent_usd")

    # --- Cobre: override local si existe; si no, COMEX ---
    cobre_of = get_cobre_oficial_local()
    if cobre_of and cobre_of.get("cobre_usd_lb"):
        cb = cobre_of
    else:
        cb = get_cobre_comex()
        if cb.get("cobre_usd_lb") is None and last.get("cobre_usd_lb") is not None:
            cb["cobre_usd_lb"] = last.get("cobre_usd_lb")

    # --- Combustibles (forzado a JSON local semanal) ---
    combustibles = get_enap_local()
    for k in ("g93_clp_l", "g95_clp_l", "g97_clp_l", "diesel_clp_l"):
        if combustibles.get(k) is None and last.get(k) is not None:
            combustibles[k] = last.get(k)

    # --- Noticia automática ---
    news = get_news_auto()

    # --- Ensamble datos finales ---
    data = {
        **({} if md is None else md),
        **({} if cr is None else cr),
        **({} if br is None else br),
        **({} if cb is None else cb),
        **({} if combustibles is None else combustibles),
    }

    # Guarda cache para el render
    save_latest(data)

    # --- Salida por consola (prolija) ---
    print(f"\nFINANZAS HOY CHILE — {fecha_hoy}")
    print("-" * 64)
    print(f"Dólar (CLP):       {fmt_clp(data.get('dolar_clp'))}")
    print(f"UF (CLP):          {fmt_clp(data.get('uf_clp'))}")
    print(f"UTM (CLP):         {fmt_clp(data.get('utm_clp'))}")
    print(f"Cobre (USD/lb):    {fmt_float(data.get('cobre_usd_lb'), 2)}")
    print(f"Brent (USD/bbl):   {fmt_usd(data.get('brent_usd'), 0)}")
    print("-" * 64)
    print("Combustibles (CLP/L) — Precios a público:")
    print(f"  93:    {fmt_clp(data.get('g93_clp_l'))}")
    print(f"  95:    {fmt_clp(data.get('g95_clp_l'))}")
    print(f"  97:    {fmt_clp(data.get('g97_clp_l'))}")
    print(f"  Diésel:{fmt_clp(data.get('diesel_clp_l'))}")
    print("-" * 64)
    print(f"BTC (USD):         {fmt_usd(data.get('btc_usd'), 0)}")
    print(f"ETH (USD):         {fmt_usd(data.get('eth_usd'), 0)}")
    print("-" * 64)
    print("Noticia del día:", (news.get("titular") or "N/D"))

if __name__ == "__main__":
    main()