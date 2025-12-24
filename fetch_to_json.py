# fetch_to_json.py

import datetime as dt
import json
import os
from pathlib import Path

from fetch_finanzas_cl import (get_brent, get_cobre_comex,
                               get_cobre_oficial_local, get_crypto,
                               get_enap_local, get_mindicador, load_last_ok)
from fetch_finanzas_cl import main as run_console
from fetch_finanzas_cl import save_last_ok


def _tz_now() -> dt.datetime:
    tzname = (os.getenv("TZ") or "America/Santiago").strip()
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo(tzname))
    except Exception:
        return dt.datetime.now()


def collect():
    last = load_last_ok()

    # --- Dólar/UF/UTM ---
    try:
        md = get_mindicador()
    except Exception:
        md = {
            "dolar_clp": last.get("dolar_clp"),
            "uf_clp": last.get("uf_clp"),
            "utm_clp": last.get("utm_clp"),
        }

    # --- Crypto ---
    try:
        cr = get_crypto()
    except Exception:
        cr = {
            "btc_usd": last.get("btc_usd"),
            "eth_usd": last.get("eth_usd"),
        }

    # --- Brent ---
    try:
        br = get_brent()
    except Exception:
        br = {"brent_usd": None}

    if br.get("brent_usd") is None and last.get("brent_usd") is not None:
        br["brent_usd"] = last.get("brent_usd")

    # --- Cobre ---
    cb = None
    try:
        cobre_of = get_cobre_oficial_local()
        if cobre_of and cobre_of.get("cobre_usd_lb") is not None:
            cb = cobre_of
    except Exception:
        cb = None

    if not cb:
        try:
            cb = get_cobre_comex()
        except Exception:
            cb = {"cobre_usd_lb": None}

    if cb.get("cobre_usd_lb") is None and last.get("cobre_usd_lb") is not None:
        cb["cobre_usd_lb"] = last.get("cobre_usd_lb")

    # --- Combustibles (local semanal) ---
    combustibles = get_enap_local() or {}
    for k in ("g93_clp_l", "g95_clp_l", "g97_clp_l", "diesel_clp_l"):
        if combustibles.get(k) is None and last.get(k) is not None:
            combustibles[k] = last.get(k)

    if isinstance(combustibles, dict):
        combustibles.pop("vigencia_semana", None)

    data = {
        **(md or {}),
        **(cr or {}),
        **(br or {}),
        **(cb or {}),
        **(combustibles or {}),
    }

    now = _tz_now()
    data["fecha"] = now.strftime("%d-%m-%Y")                 # ✅ DD-MM-YYYY
    data["generated_at"] = now.isoformat(timespec="seconds") # ✅ con TZ si está disponible
    return data


def _should_save_last_ok(d: dict) -> bool:
    critical = ("dolar_clp", "uf_clp", "cobre_usd_lb", "brent_usd")
    return all(d.get(k) is not None for k in critical)


if __name__ == "__main__":
    data = collect()

    outdir = Path("data")
    outdir.mkdir(exist_ok=True)
    (outdir / "latest.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if _should_save_last_ok(data):
        save_last_ok(data)

    run_console()
    print(json.dumps(data, ensure_ascii=False))