"""
Genera la locución en M4A desde data/latest.json.

- macOS: usa /usr/bin/say (mejor calidad)
- Linux/Render: usa PIPER (neural, offline) + ffmpeg
  (fallback final: espeak-ng/espeak + ffmpeg)

ENDURECIDO:
- Tolera valores None
- Si falta cobre/brent/etc. intenta fallback desde "last_ok"
  (via fetch_finanzas_cl.load_last_ok() o data/last_ok.json si existe)
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict

# ===== Config =====
DEFAULT_RATE = int(os.getenv("SPEAK_RATE", "175"))  # 160–190 natural

# Piper (Render/Linux) - voz neural offline
PIPER_BIN = os.getenv("PIPER_BIN", "").strip() or str(Path(__file__).resolve().parent / "piper")
PIPER_VOICE_ONNX = os.getenv("PIPER_VOICE_ONNX", "").strip() or "voices/es_ES-carlfm-x_low.onnx"

# Para Linux/Render: fallback robótico (si Piper no está)
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
    try:
        from fetch_finanzas_cl import load_last_ok  # type: ignore
        d = load_last_ok()
        if isinstance(d, dict):
            return d
    except Exception:
        pass

    try:
        if LAST_OK_JSON.exists():
            return json.loads(LAST_OK_JSON.read_text(encoding="utf-8"))
    except Exception:
        pass

    return {}


def _merge_with_fallback(latest: Dict, last_ok: Dict) -> Dict:
    out = dict(latest or {})
    for k, v in (last_ok or {}).items():
        if out.get(k) is None and v is not None:
            out[k] = v
    return out


# -------------------------
# TTS macOS helpers
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
    out = list_system_voices()
    candidates = []
    for line in (out or "").splitlines():
        if "es_" in line or "Spanish" in line:
            name = line.split(None, 1)[0].strip()
            candidates.append(name)

    for pref in PREFERRED_ES_VOICES:
        for cand in candidates:
            if cand.lower().startswith(pref.lower()):
                return cand

    return candidates[0] if candidates else (FALLBACK_VOICE or "")


def _have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg"))


def _resolve_piper_bin() -> str:
    # Si es ruta directa existente, úsala
    if PIPER_BIN and Path(PIPER_BIN).exists():
        return PIPER_BIN
    # Si es nombre de binario en PATH
    found = shutil.which(PIPER_BIN) if PIPER_BIN else None
    if found:
        return found
    # fallback final
    found2 = shutil.which("piper")
    return found2 or ""


def _try_piper(text: str, out_m4a: Path) -> bool:
    """
    Intenta generar la voz con Piper (neural).
    Devuelve True si logró generar out_m4a.
    """
    piper = _resolve_piper_bin()
    model = Path(PIPER_VOICE_ONNX)

    if not piper:
        return False
    if not model.exists():
        return False
    if not _have_ffmpeg():
        return False

    out_m4a.parent.mkdir(exist_ok=True, parents=True)
    wav = out_m4a.with_suffix(".wav")

    # Piper lee texto por stdin y escribe wav.
    # Nota: Piper no usa rate como say/espeak; la naturalidad depende del modelo.
    cmd = [piper, "--model", str(model), "--output_file", str(wav)]
    try:
        subprocess.run(cmd, input=text, text=True, check=True)
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(wav), "-c:a", "aac", "-b:a", "128k", str(out_m4a)],
            check=True,
        )
        return True
    finally:
        try:
            wav.unlink(missing_ok=True)
        except Exception:
            pass


def speak(text: str, out_m4a: Path, rate: int = DEFAULT_RATE):
    """
    - macOS: usa /usr/bin/say y convierte a .m4a
    - Linux/Render:
        1) Piper (neural) -> wav -> ffmpeg -> m4a
        2) fallback: espeak-ng -> wav -> ffmpeg -> m4a
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

    # --- Linux / Render: Piper primero (neural) ---
    if _try_piper(text, out_m4a):
        return

    # --- Fallback robótico: espeak-ng/espeak ---
    wav = out_m4a.with_suffix(".wav")
    tts_rate = str(rate)

    tts_bin = shutil.which("espeak-ng") or shutil.which("espeak")
    if not tts_bin:
        raise RuntimeError("No se encontró Piper (o falló) y tampoco espeak-ng/espeak en el sistema.")

    voice_try = ESPEAK_VOICE.strip() or "es"
    pitch = ESPEAK_PITCH.strip() or "55"
    amp = ESPEAK_AMP.strip() or "110"
    gap = ESPEAK_GAP.strip() or "6"

    cmd = [tts_bin, "-v", voice_try, "-s", tts_rate, "-p", pitch, "-a", amp, "-g", gap, "-w", str(wav), text]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError:
        cmd2 = [tts_bin, "-v", "es", "-s", tts_rate, "-p", pitch, "-a", amp, "-g", gap, "-w", str(wav), text]
        subprocess.run(cmd2, check=True)

    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(wav), "-c:a", "aac", "-b:a", "128k", str(out_m4a)],
            check=True,
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
    try:
        return str(round(float(x)))
    except Exception:
        return "N/D"


def _sf(x, nd="N/D") -> str:
    try:
        return f"{float(x):.2f}"
    except Exception:
        return nd


def build_text_from_json(data: dict) -> str:
    parts = []
    parts.append("Finanzas Hoy Chile.")

    parts.append(f"Dólar: {_sr(data.get('dolar_clp'))} pesos.")
    parts.append(f"UF: {_sr(data.get('uf_clp'))} pesos.")

    cobre = data.get("cobre_usd_lb")
    if cobre is None:
        parts.append("Cobre: no disponible.")
    else:
        parts.append(f"Cobre: {_sf(cobre)} dólares por libra.")

    brent = data.get("brent_usd")
    if brent is None:
        parts.append("Petróleo Brent: no disponible.")
    else:
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