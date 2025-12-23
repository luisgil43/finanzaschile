#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
fetch_finanzas_cl.py
- Fuentes robustas y SILENCIOSAS para Cobre/Brent:
  Stooq primero, y Yahoo Chart API (requests) como fallback.
- Mantiene load_last_ok() para fallback cuando hay reinicios.
- main() imprime salida estilo consola (para debug local).

NOTA:
- Evitamos yfinance como fallback porque mete prints/logs raros y a veces falla JSON.
"""

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

import requests

# =========================
# Paths
# =========================
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SOURCES_DIR = BASE_DIR / "sources"

LATEST_JSON = DATA_DIR / "latest.json"
LAST_OK_JSON = DATA_DIR / "last_ok.json"

ENAP_FILE = SOURCES_DIR / "enap_semana.json"
COBRE_OFICIAL_FILE = SOURCES_DIR / "cobre_oficial.json"  # opcional si quieres tener uno local

# =========================
# HTTP helpers
# =========================
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def _http_get(url: str, timeout: int = 15) -> requests.Response:
    return requests.get(url, timeout=timeout, headers={"User-Agent": _UA})


def _safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def _safe_json_load(path: Path) -> Dict:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _safe_json_write(path: Path, data: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# =========================
# Persistencia "best effort"
# =========================
def load_last_ok() -> Dict:
    """
    Último set de valores "buenos" (para fallback cuando un fetch falla).
    OJO: en Render sin disco persistente se puede perder tras restart.
    """
    return _safe_json_load(LAST_OK_JSON)


def save_last_ok(data: Dict) -> None:
    _safe_json_write(LAST_OK_JSON, data)


# =========================
# Stooq helpers (liviano)
# =========================
def _stooq_last_close(symbol: str) -> Optional[float]:
    """
    Intenta obtener el último CLOSE desde Stooq.
    Devuelve float o None.
    """
    urls = [
        f"https://stooq.pl/q/l/?s={symbol}&i=d",
        f"https://stooq.com/q/l/?s={symbol}&i=d",
        f"https://stooq.pl/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv",
        f"https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv",
    ]

    for url in urls:
        try:
            r = _http_get(url, timeout=15)
            if r.status_code != 200:
                continue
            text = (r.text or "").strip()
            if not text or "No data" in text or "Brak danych" in text:
                continue

            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            if len(lines) < 2:
                continue

            last = lines[-1]
            cols = [c.strip() for c in last.split(",")]
            if len(cols) < 5:
                cols = [c.strip() for c in last.split(";")]
            if len(cols) < 5:
                continue

            close_val = _safe_float(cols[4])
            if close_val is None:
                continue
            return close_val
        except Exception:
            continue

    return None


# =========================
# Yahoo Chart API fallback (SILENCIOSO + liviano)
# =========================
def _yahoo_chart_last_close(ticker: str) -> Optional[float]:
    """
    Último cierre usando endpoint público:
      https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=5d&interval=1d

    Ventajas:
    - No usa yfinance (no prints raros)
    - Menos dependencia / menos RAM
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {"range": "5d", "interval": "1d"}

    try:
        r = requests.get(url, params=params, timeout=20, headers={"User-Agent": _UA})
        if r.status_code != 200:
            return None

        j = r.json()
        chart = (j or {}).get("chart") or {}
        error = chart.get("error")
        if error:
            return None

        results = chart.get("result") or []
        if not results:
            return None

        result0 = results[0] or {}
        indicators = (result0.get("indicators") or {})
        quote = (indicators.get("quote") or [])
        if not quote:
            return None

        closes = (quote[0] or {}).get("close") or []
        # toma último close no-nulo desde el final
        for x in reversed(closes):
            v = _safe_float(x)
            if v is not None:
                return float(v)

        return None
    except Exception:
        return None


# =========================
# Fuentes principales (Chile + mercados)
# =========================
def get_mindicador() -> Dict:
    """
    mindicador.cl (UF, dólar, UTM)
    """
    url = "https://mindicador.cl/api"
    r = _http_get(url, timeout=20)
    r.raise_for_status()
    j = r.json()

    dolar = _safe_float((j.get("dolar") or {}).get("valor"))
    uf = _safe_float((j.get("uf") or {}).get("valor"))
    utm = _safe_float((j.get("utm") or {}).get("valor"))

    return {"dolar_clp": dolar, "uf_clp": uf, "utm_clp": utm}


def get_crypto() -> Dict:
    """
    Coingecko simple price
    """
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        "?ids=bitcoin,ethereum&vs_currencies=usd"
    )
    r = _http_get(url, timeout=20)
    r.raise_for_status()
    j = r.json()
    btc = _safe_float((j.get("bitcoin") or {}).get("usd"))
    eth = _safe_float((j.get("ethereum") or {}).get("usd"))
    return {"btc_usd": btc, "eth_usd": eth}


def get_enap_local() -> Dict:
    """
    Lee sources/enap_semana.json (editable por tu panel).
    """
    d = _safe_json_load(ENAP_FILE)
    out = {
        "vigencia": (d.get("vigencia") or "").strip(),
        "g93_clp_l": d.get("g93_clp_l"),
        "g95_clp_l": d.get("g95_clp_l"),
        "g97_clp_l": d.get("g97_clp_l"),
        "diesel_clp_l": d.get("diesel_clp_l"),
    }
    return out


def get_cobre_oficial_local() -> Optional[Dict]:
    """
    (Opcional) si tienes una fuente oficial/propia cacheada en sources/cobre_oficial.json:
      {"cobre_usd_lb": 5.57, "source": "...", "ts": "..."}
    """
    d = _safe_json_load(COBRE_OFICIAL_FILE)
    if not d:
        return None
    val = _safe_float(d.get("cobre_usd_lb"))
    if val is None:
        return None
    return {"cobre_usd_lb": val}


def get_cobre_comex() -> Dict:
    """
    Cobre COMEX aprox (USD/lb).

    Prioridad:
      1) Stooq HG.F (a veces viene en centavos/lb -> convertimos)
      2) Yahoo Chart HG=F (fallback)
    """
    # 1) Stooq
    close = _stooq_last_close("hg.f")
    if close is not None:
        cobre = close / 100.0 if close > 50 else close
        return {"cobre_usd_lb": float(cobre)}

    # 2) Yahoo chart fallback (silencioso)
    yf_close = _yahoo_chart_last_close("HG=F")
    if yf_close is not None:
        return {"cobre_usd_lb": float(yf_close)}

    return {"cobre_usd_lb": None}


def get_brent() -> Dict:
    """
    Brent (USD/bbl).

    Prioridad:
      1) Stooq CB.F
      2) Yahoo Chart BZ=F (fallback)
    """
    # 1) Stooq
    close = _stooq_last_close("cb.f")
    if close is not None:
        return {"brent_usd": float(close)}

    # 2) Yahoo chart fallback (silencioso)
    yf_close = _yahoo_chart_last_close("BZ=F")
    if yf_close is not None:
        return {"brent_usd": float(yf_close)}

    return {"brent_usd": None}


# =========================
# Consola (debug)
# =========================
def _fmt_clp(x: Optional[float]) -> str:
    if x is None:
        return "N/D"
    return f"$ {x:,.0f}".replace(",", ".")


def _fmt_usd(x: Optional[float]) -> str:
    if x is None:
        return "N/D"
    return f"${x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_num(x: Optional[float], nd: str = "N/D") -> str:
    if x is None:
        return nd
    return f"{x:.2f}"


def main():
    """
    Imprime un resumen estilo consola.
    Nota: el pipeline real usa fetch_to_json.py, render_panel.py, etc.
    """
    d = _safe_json_load(LATEST_JSON) if LATEST_JSON.exists() else {}
    if not d:
        last = load_last_ok()
        try:
            md = get_mindicador()
        except Exception:
            md = {}
        try:
            cr = get_crypto()
        except Exception:
            cr = {}
        br = get_brent()
        cb = get_cobre_comex()
        en = get_enap_local()
        d = {**last, **md, **cr, **br, **cb, **en}

    today = dt.datetime.now()
    dias = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    dia = dias[today.weekday()]

    print(f"\nFINANZAS HOY CHILE — {dia} {today.strftime('%d/%m/%Y')}")
    print("-" * 64)
    print(f"Dólar (CLP):       {_fmt_clp(_safe_float(d.get('dolar_clp')))}")
    print(f"UF (CLP):          {_fmt_clp(_safe_float(d.get('uf_clp')))}")
    print(f"UTM (CLP):         {_fmt_clp(_safe_float(d.get('utm_clp')))}")
    print(f"Cobre (USD/lb):    {_fmt_num(_safe_float(d.get('cobre_usd_lb')))}")
    print(f"Brent (USD/bbl):   {_fmt_usd(_safe_float(d.get('brent_usd')))}")
    print("-" * 64)
    print("Combustibles (CLP/L) — Precios a público:")
    print(f"  93:    {_fmt_clp(_safe_float(d.get('g93_clp_l')))}")
    print(f"  95:    {_fmt_clp(_safe_float(d.get('g95_clp_l')))}")
    print(f"  97:    {_fmt_clp(_safe_float(d.get('g97_clp_l')))}")
    print(f"  Diésel:{_fmt_clp(_safe_float(d.get('diesel_clp_l')))}")
    print("-" * 64)
    # (mantengo tu formato; si quieres lo dejamos más limpio después)
    print(f"BTC (USD):         {_fmt_clp(_safe_float(d.get('btc_usd'))).replace('$','$',1)}")
    print(f"ETH (USD):         {_fmt_clp(_safe_float(d.get('eth_usd'))).replace('$','$',1)}")
    print("-" * 64)


if __name__ == "__main__":
    # baja el ruido de logs globales si alguna lib insiste
    logging.getLogger().setLevel(logging.ERROR)
    main()