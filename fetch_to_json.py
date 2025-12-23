import datetime as dt
import json
from pathlib import Path

from fetch_finanzas_cl import (get_brent, get_cobre_comex,
                               get_cobre_oficial_local, get_crypto,
                               get_enap_local, get_mindicador, load_last_ok)
from fetch_finanzas_cl import main as run_console


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
    br = get_brent()
    if br.get("brent_usd") is None and last.get("brent_usd") is not None:
        br["brent_usd"] = last.get("brent_usd")

    # --- Cobre ---
    cobre_of = get_cobre_oficial_local()
    if cobre_of and cobre_of.get("cobre_usd_lb"):
        cb = cobre_of
    else:
        cb = get_cobre_comex()
        if cb.get("cobre_usd_lb") is None and last.get("cobre_usd_lb") is not None:
            cb["cobre_usd_lb"] = last.get("cobre_usd_lb")

    # --- Combustibles (forzado a JSON local semanal) ---
    combustibles = get_enap_local()

    # ✅ Cambio: quitamos "vigencia_semana" para eliminar fechas/rangos de combustibles
    for k in ("g93_clp_l", "g95_clp_l", "g97_clp_l", "diesel_clp_l"):
        if combustibles.get(k) is None and last.get(k) is not None:
            combustibles[k] = last.get(k)

    # Por seguridad: si por cualquier razón viene en el dict, lo removemos igual
    if isinstance(combustibles, dict):
        combustibles.pop("vigencia_semana", None)

    data = {
        **(md or {}),
        **(cr or {}),
        **(br or {}),
        **(cb or {}),
        **(combustibles or {}),
    }
    data["generated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    return data


if __name__ == "__main__":
    data = collect()
    outdir = Path("data")
    outdir.mkdir(exist_ok=True)
    (outdir / "latest.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # Imprime como consola (no intenta CNE)
    run_console()