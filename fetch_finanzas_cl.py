"""
fetch_finanzas_cl.py
- Fuentes robustas y SILENCIOSAS para Cobre/Brent:
  Stooq primero, y Yahoo Chart API (requests) como fallback.
- Mantiene load_last_ok() para fallback cuando hay reinicios.
- GUARDA generated_at + fecha (Chile) en latest.json para que el video/título SIEMPRE
  use la fecha correcta del día en Chile (no la "fecha" de mindicador).
"""

import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

import requests


# =========================
# TZ helpers (Chile)
# =========================
def tz_now() -> dt.datetime:
    tzname = (os.getenv("TZ") or "America/Santiago").strip()
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo(tzname))
    except Exception:
        return dt.datetime.now()


def fecha_ddmmyyyy(d: Optional[dt.datetime] = None) -> str:
    d = d or tz_now()
    return d.strftime("%d-%m-%Y")


def fecha_ddmmyyyy_slash(d: Optional[dt.datetime] = None) -> str:
    d = d or tz_now()
    return d.strftime("%d/%m/%Y")


# =========================
# Paths
# =========================
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SOURCES_DIR = BASE_DIR / "sources"

LATEST_JSON = DATA_DIR / "latest.json"
LAST_OK_JSON = DATA_DIR / "last_ok.json"

ENAP_FILE = SOURCES_DIR / "enap_semana.json"
COBRE_OFICIAL_FILE = SOURCES_DIR / "cobre_oficial.json"  # opcional

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
    return _safe_json_load(LAST_OK_JSON)


def save_last_ok(data: Dict) -> None:
    _safe_json_write(LAST_OK_JSON, data)


def save_latest(data: Dict) -> None:
    _safe_json_write(LATEST_JSON, data)


# =========================
# Stooq helpers
# =========================
def _stooq_last_close(symbol: str) -> Optional[float]:
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
# Yahoo Chart API fallback (silencioso)
# =========================
def _yahoo_chart_last_close(ticker: str) -> Optional[float]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {"range": "5d", "interval": "1d"}

    try:
        r = requests.get(url, params=params, timeout=20, headers={"User-Agent": _UA})
        if r.status_code != 200:
            return None

        j = r.json()
        chart = (j or {}).get("chart") or {}
        if chart.get("error"):
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
        for x in reversed(closes):
            v = _safe_float(x)
            if v is not None:
                return float(v)

        return None
    except Exception:
        return None


# =========================
# Fuentes principales
# =========================
def get_mindicador() -> Dict:
    url = "https://mindicador.cl/api"
    r = _http_get(url, timeout=20)
    r.raise_for_status()
    j = r.json()

    dolar = _safe_float((j.get("dolar") or {}).get("valor"))
    uf = _safe_float((j.get("uf") or {}).get("valor"))
    utm = _safe_float((j.get("utm") or {}).get("valor"))

    return {"dolar_clp": dolar, "uf_clp": uf, "utm_clp": utm}


def get_crypto() -> Dict:
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
    d = _safe_json_load(ENAP_FILE)
    return {
        "g93_clp_l": d.get("g93_clp_l"),
        "g95_clp_l": d.get("g95_clp_l"),
        "g97_clp_l": d.get("g97_clp_l"),
        "diesel_clp_l": d.get("diesel_clp_l"),
        "vigencia": (d.get("vigencia") or "").strip(),
    }


def get_cobre_oficial_local() -> Optional[Dict]:
    d = _safe_json_load(COBRE_OFICIAL_FILE)
    if not d:
        return None
    val = _safe_float(d.get("cobre_usd_lb"))
    if val is None:
        return None
    return {"cobre_usd_lb": val}


def get_cobre_comex() -> Dict:
    close = _stooq_last_close("hg.f")
    if close is not None:
        cobre = close / 100.0 if close > 50 else close
        return {"cobre_usd_lb": float(cobre)}

    yf_close = _yahoo_chart_last_close("HG=F")
    if yf_close is not None:
        return {"cobre_usd_lb": float(yf_close)}

    return {"cobre_usd_lb": None}


def get_brent() -> Dict:
    close = _stooq_last_close("cb.f")
    if close is not None:
        return {"brent_usd": float(close)}

    yf_close = _yahoo_chart_last_close("BZ=F")
    if yf_close is not None:
        return {"brent_usd": float(yf_close)}

    return {"brent_usd": None}


# =========================
# Main
# =========================
def main():
    # Fecha/TS OFICIAL del pipeline (Chile)
    stamp = tz_now()

    last_ok = load_last_ok()

    try:
        md = get_mindicador()
    except Exception:
        md = {}

    try:
        cr = get_crypto()
    except Exception:
        cr = {}

    br = get_brent()
    cb = get_cobre_oficial_local() or get_cobre_comex()
    en = get_enap_local()

    data = {**last_ok, **md, **cr, **br, **cb, **en}

    # ✅ Campos de fecha correctos (NO dependen de mindicador)
    data["generated_at"] = stamp.isoformat()
    data["fecha"] = fecha_ddmmyyyy(stamp)          # DD-MM-YYYY
    data["fecha_slash"] = fecha_ddmmyyyy_slash(stamp)  # DD/MM/YYYY

    # Guarda latest + last_ok
    save_latest(data)
    save_last_ok(data)

    print(f"OK latest.json generado para {data['fecha_slash']} ({data['generated_at']})")


if __name__ == "__main__":
    logging.getLogger().setLevel(logging.ERROR)
    main()