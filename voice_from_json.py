#voice_from_json.py


"""
Genera la locución en M4A desde data/latest.json.

- macOS: usa /usr/bin/say (mejor calidad)
- Linux/Render: usa espeak-ng (o espeak) + ffmpeg

ENDURECIDO:
- Tolera valores None
- Si falta cobre/brent/etc. intenta fallback desde "last_ok"
  (via fetch_finanzas_cl.load_last_ok() o data/last_ok.json si existe)
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

# ===== Config =====
DEFAULT_RATE = int(os.getenv("SPEAK_RATE", "175"))  # 160–190 natural

# Para Linux/Render: parámetros que suavizan un poco la voz
ESPEAK_VOICE = os.getenv("ESPEAK_VOICE", "es-la")  # prueba "es-la", si no existe cae a "es"
ESPEAK_PITCH = os.getenv("ESPEAK_PITCH", "55")     # 0-99 (50 ~ neutral)
ESPEAK_AMP = os.getenv("ESPEAK_AMP", "110")        # 0-200
ESPEAK_GAP = os.getenv("ESPEAK_GAP", "6")          # word gap ms aprox (0-20)

PREFERRED_ES_VOICES = [
    "Paulina", "Mónica", "Sandy", "Shelley", "Reed", "Jorge", "Diego", "Juan",
    "Rocko", "Grandma", "Grandpa", "Flo", "Eddy",
]
FALLBACK_VOICE = os.getenv("VOICE_NAME", "Paulina")

LATEST_JSON = Path(os.getenv("LATEST_JSON_PATH", "data/latest.json"))
LAST_OK_JSON = Path(os.getenv("LAST_OK_JSON_PATH", "data/last_ok.json"))  # opcional
OUT_M4A = Path(os.getenv("VOICE_OUT_PATH", "out/locucion.m4a"))


# -------------------------
# Helpers de fallback
# -------------------------
def _load_last_ok_anyhow() -> Dict:
    """
    Intenta cargar last_ok desde:
      1) fetch_finanzas_cl.load_last_ok() (si existe)
      2) data/last_ok.json (si existe)
    """
    # 1) Intento por módulo
    try:
        from fetch_finanzas_cl import load_last_ok  # type: ignore
        d = load_last_ok()
        if isinstance(d, dict):
            return d
    except Exception:
        pass

    # 2) Intento por archivo
    try:
        if LAST_OK_JSON.exists():
            return json.loads(LAST_OK_JSON.read_text(encoding="utf-8"))
    except Exception:
        pass

    return {}


def _merge_with_fallback(latest: Dict, last_ok: Dict) -> Dict:
    """
    Si en latest falta un key o viene None, lo rellena con last_ok[key] si existe.
    """
    out = dict(latest or {})
    for k, v in (last_ok or {}).items():
        if out.get(k) is None and v is not None:
            out[k] = v
    return out


# -------------------------
# TTS macOS
# -------------------------
def list_system_voices() -> str:
    try:
        out = subprocess.run(
            ["/usr/bin/say", "-v", "?"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return out
    except Exception:
        return ""


def pick_spanish_voice() -> str:
    """
    Devuelve una voz preferida (macOS) si existe.
    En Linux/Render no aplica.
    """
    out = list_system_voices()
    candidates = []
    for line in (out or "").splitlines():
        # Ej: "Paulina es_ES ..."
        if "es_" in line or "Spanish" in line:
            name = line.split(None, 1)[0].strip()
            candidates.append(name)

    for pref in PREFERRED_ES_VOICES:
        for cand in candidates:
            if cand.lower().startswith(pref.lower()):
                return cand

    return candidates[0] if candidates else (FALLBACK_VOICE or "")


def speak(text: str, out_m4a: Path, rate: int = DEFAULT_RATE):
    """
    - macOS: usa /usr/bin/say y convierte a .m4a
    - Linux/Render: usa espeak-ng (o espeak) -> wav -> ffmpeg -> m4a
    """
    out_m4a.parent.mkdir(exist_ok=True, parents=True)

    # --- macOS ---
    if sys.platform == "darwin" and Path("/usr/bin/say").exists():
        voice = pick_spanish_voice()
        aiff = out_m4a.with_suffix(".aiff")

        say_cmd = ["/usr/bin/say"]
        if voice:
            say_cmd += ["-v", voice]
        say_cmd += ["-r", str(rate), text, "-o", str(aiff)]
        subprocess.run(say_cmd, check=True)

        # Convertir a M4A
        try:
            subprocess.run(["ffmpeg", "-y", "-i", str(aiff), str(out_m4a)], check=True)
        except FileNotFoundError:
            subprocess.run(
                [
                    "afconvert",
                    str(aiff),
                    str(out_m4a),
                    "-f",
                    "m4af",
                    "-d",
                    "aac",
                    "-b",
                    "128000",
                    "-q",
                    "1",
                ],
                check=True,
            )
        finally:
            try:
                aiff.unlink(missing_ok=True)
            except Exception:
                pass
        return

    # --- Linux / Render ---
    wav = out_m4a.with_suffix(".wav")
    tts_rate = str(rate)

    import shutil
    tts_bin = shutil.which("espeak-ng") or shutil.which("espeak")
    if not tts_bin:
        raise RuntimeError("No se encontró espeak-ng ni espeak en el sistema (Render).")

    # probamos voz preferida, si falla caemos a "es"
    voice_try = ESPEAK_VOICE.strip() or "es"
    pitch = ESPEAK_PITCH.strip() or "55"
    amp = ESPEAK_AMP.strip() or "110"
    gap = ESPEAK_GAP.strip() or "6"

    cmd = [tts_bin, "-v", voice_try, "-s", tts_rate, "-p", pitch, "-a", amp, "-g", gap, "-w", str(wav), text]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError:
        # fallback a "es"
        cmd2 = [tts_bin, "-v", "es", "-s", tts_rate, "-p", pitch, "-a", amp, "-g", gap, "-w", str(wav), text]
        subprocess.run(cmd2, check=True)

    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(wav), "-c:a", "aac", "-b:a", "128k", str(out_m4a)],
            check=True
        )
    finally:
        try:
            wav.unlink(missing_ok=True)
        except Exception:
            pass


# -------------------------
# Formateo
# -------------------------
def _sr(x) -> str:
    """string round: redondea números; tolera None."""
    try:
        return str(round(float(x)))
    except Exception:
        return "N/D"


def _sf(x, nd="N/D") -> str:
    """string float: 2 decimales; tolera None."""
    try:
        return f"{float(x):.2f}"
    except Exception:
        return nd


def build_text_from_json(data: dict) -> str:
    # Siempre arma locución estable (sin omitir cobre/brent por None)
    parts = []
    parts.append("Finanzas Hoy Chile.")

    parts.append(f"Dólar: {_sr(data.get('dolar_clp'))} pesos.")
    parts.append(f"UF: {_sr(data.get('uf_clp'))} pesos.")

    # cobre: si no hay dato, lo decimos igual como "no disponible" (pero idealmente ya viene del fallback)
    cobre = data.get("cobre_usd_lb")
    if cobre is None:
        parts.append("Cobre: no disponible.")
    else:
        parts.append(f"Cobre: {_sf(cobre)} dólares por libra.")

    brent = data.get("brent_usd")
    if brent is None:
        parts.append("Petróleo Brent: no disponible.")
    else:
        # brent suele verse mejor como float corto (ej 62.58)
        parts.append(f"Petróleo Brent: {_sf(brent)} dólares por barril.")

    parts.append(f"Bitcoin: {_sr(data.get('btc_usd'))} dólares.")

    eth = data.get("eth_usd")
    if eth is None:
        parts.append("Ethereum: no disponible.")
    else:
        parts.append(f"Ethereum: {_sr(eth)} dólares.")

    return " ".join(parts)


def main():
    latest = json.loads(LATEST_JSON.read_text(encoding="utf-8"))
    last_ok = _load_last_ok_anyhow()
    data = _merge_with_fallback(latest, last_ok)

    text = build_text_from_json(data)

    Path("out").mkdir(exist_ok=True)
    speak(text, OUT_M4A)


if __name__ == "__main__":
    main()