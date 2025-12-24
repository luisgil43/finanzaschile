"""
voice_from_json.py

Genera la locución en M4A desde data/latest.json.

- macOS: usa /usr/bin/say (mejor calidad, voces del sistema como "Paulina")
- Linux/Render:
    1) Edge TTS (neural) si USE_EDGE_TTS=1 y edge-tts está disponible
    2) Piper (neural) si está configurado
    3) espeak-ng/espeak fallback

ENDURECIDO:
- Tolera valores None
- Si falta cobre/brent/etc. intenta fallback desde "last_ok"
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

# ===== Paths =====
LATEST_JSON = Path(os.getenv("LATEST_JSON_PATH", "data/latest.json"))
LAST_OK_JSON = Path(os.getenv("LAST_OK_JSON_PATH", "data/last_ok.json"))
OUT_M4A = Path(os.getenv("VOICE_OUT_PATH", "out/locucion.m4a"))

# ===== macOS say =====
DEFAULT_RATE = int(os.getenv("SPEAK_RATE", "175"))  # 160–190 natural
PREFERRED_ES_VOICES = ["Paulina", "Mónica", "Jorge", "Diego", "Juan"]
FALLBACK_VOICE = os.getenv("VOICE_NAME", "Paulina").strip()

# ===== Edge TTS (Linux/Render) =====
USE_EDGE_TTS = os.getenv("USE_EDGE_TTS", "1").strip() == "1"
EDGE_VOICE = os.getenv("EDGE_VOICE", "es-CL-CatalinaNeural").strip()
EDGE_RATE = os.getenv("EDGE_RATE", "0").strip()  # acepta "5" o "+5%" o "-5%"
EDGE_PITCH = os.getenv("EDGE_PITCH", "0").strip()  # acepta "0" o "+0Hz"
EDGE_VOLUME = os.getenv("EDGE_VOLUME", "").strip()  # opcional, ej "+0%"
EDGE_OUTPUT = os.getenv("EDGE_OUTPUT", "out/locucion_edge.mp3").strip()

# ===== Piper (Linux/Render) =====
USE_PIPER = os.getenv("USE_PIPER", "1").strip() == "1"
PIPER_BIN = os.getenv("PIPER_BIN", "piper").strip() or "piper"
PIPER_MODEL = os.getenv("PIPER_MODEL", "").strip()
PIPER_CONFIG = os.getenv("PIPER_CONFIG", "").strip()

# Ajustes naturalidad (Piper)
PIPER_LENGTH_SCALE = os.getenv("PIPER_LENGTH_SCALE", "1.08").strip()   # >1 = más lento/natural
PIPER_SENTENCE_SILENCE = os.getenv("PIPER_SENTENCE_SILENCE", "0.20").strip()
PIPER_NOISE_SCALE = os.getenv("PIPER_NOISE_SCALE", "").strip()
PIPER_NOISE_W = os.getenv("PIPER_NOISE_W", "").strip()

# ===== espeak fallback =====
# Compat: algunos envs tuyos están como SPEAK_* (antes)
ESPEAK_VOICE = os.getenv("ESPEAK_VOICE", os.getenv("SPEAK_VOICE", "es+f3")).strip()
ESPEAK_PITCH = os.getenv("ESPEAK_PITCH", os.getenv("SPEAK_PITCH", "55")).strip()
ESPEAK_AMP = os.getenv("ESPEAK_AMP", os.getenv("SPEAK_AMP", "115")).strip()
ESPEAK_GAP = os.getenv("ESPEAK_GAP", os.getenv("SPEAK_GAP", "6")).strip()

# ffmpeg
AUDIO_BITRATE = os.getenv("VOICE_AAC_BITRATE", "128k").strip()


# -------------------------
# Helpers fallback data
# -------------------------
def _load_last_ok_anyhow() -> Dict:
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
# Texto: limpieza + números
# -------------------------
def _clean_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _to_int_like(x) -> Optional[int]:
    try:
        if x is None:
            return None
        return int(round(float(x)))
    except Exception:
        return None


def _to_float(x) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def build_text_from_json(data: dict) -> str:
    dolar = _to_int_like(data.get("dolar_clp"))
    uf = _to_int_like(data.get("uf_clp"))
    btc = _to_int_like(data.get("btc_usd"))
    eth = _to_int_like(data.get("eth_usd"))
    cobre = _to_float(data.get("cobre_usd_lb"))
    brent = _to_float(data.get("brent_usd"))

    def si(x, nd="no disponible"):
        return str(x) if x is not None else nd

    parts = [
        "Finanzas Hoy Chile.",
        f"Dólar: {si(dolar)} pesos.",
        f"UF: {si(uf)} pesos.",
        f"Cobre: {si(None if cobre is None else round(cobre, 2))} dólares por libra.",
        f"Petróleo Brent: {si(None if brent is None else round(brent, 2))} dólares por barril.",
        f"Bitcoin: {si(btc)} dólares.",
        f"Ethereum: {si(eth)} dólares.",
    ]
    return _clean_spaces("\n".join(parts))


# -------------------------
# macOS say
# -------------------------
def _list_system_voices() -> str:
    try:
        return subprocess.run(
            ["/usr/bin/say", "-v", "?"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except Exception:
        return ""


def _pick_spanish_voice() -> str:
    out = _list_system_voices()
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


# -------------------------
# Edge TTS
# -------------------------
def _normalize_edge_percent(v: str) -> str:
    v = (v or "").strip()
    if not v:
        return "+0%"
    if v.endswith("%"):
        if v.startswith(("+", "-")):
            return v
        return f"+{v}"
    # si viene "5" o "-5"
    try:
        n = int(v)
        return f"{'+' if n >= 0 else ''}{n}%"
    except Exception:
        # si viene raro, usa default
        return "+0%"


def _normalize_edge_pitch(v: str) -> str:
    v = (v or "").strip()
    if not v:
        return "+0Hz"
    if v.lower().endswith("hz"):
        if v.startswith(("+", "-")):
            return v
        return f"+{v}"
    try:
        n = int(v)
        return f"{'+' if n >= 0 else ''}{n}Hz"
    except Exception:
        return "+0Hz"


def _have_edge_tts() -> bool:
    if not USE_EDGE_TTS:
        return False
    return shutil.which("edge-tts") is not None


def _edge_tts_to_audio(text: str, out_audio: Path) -> None:
    out_audio.parent.mkdir(parents=True, exist_ok=True)

    rate = _normalize_edge_percent(EDGE_RATE)
    pitch = _normalize_edge_pitch(EDGE_PITCH)

    cmd = [
        "edge-tts",
        "--voice", EDGE_VOICE,
        "--text", text,
        "--write-media", str(out_audio),
        "--rate", rate,
        "--pitch", pitch,
    ]
    if EDGE_VOLUME:
        cmd += ["--volume", _normalize_edge_percent(EDGE_VOLUME)]

    subprocess.run(cmd, check=True)


# -------------------------
# Piper
# -------------------------
def _have_piper() -> bool:
    if not USE_PIPER:
        return False
    if shutil.which(PIPER_BIN) is None:
        return False
    if not PIPER_MODEL:
        return False
    if not Path(PIPER_MODEL).exists():
        return False
    return True


def _piper_to_wav(text: str, wav_path: Path) -> None:
    wav_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        PIPER_BIN,
        "--model", PIPER_MODEL,
        "--output_file", str(wav_path),
        "--length_scale", str(PIPER_LENGTH_SCALE),
        "--sentence_silence", str(PIPER_SENTENCE_SILENCE),
    ]

    if PIPER_CONFIG and Path(PIPER_CONFIG).exists():
        cmd += ["--config", PIPER_CONFIG]
    if PIPER_NOISE_SCALE:
        cmd += ["--noise_scale", str(PIPER_NOISE_SCALE)]
    if PIPER_NOISE_W:
        cmd += ["--noise_w", str(PIPER_NOISE_W)]

    subprocess.run(cmd, input=text, text=True, check=True)


# -------------------------
# espeak fallback
# -------------------------
def _espeak_to_wav(text: str, wav_path: Path, rate: int) -> None:
    wav_path.parent.mkdir(parents=True, exist_ok=True)

    tts_bin = shutil.which("espeak-ng") or shutil.which("espeak")
    if not tts_bin:
        raise RuntimeError("No se encontró espeak-ng ni espeak en el sistema.")

    cmd = [
        tts_bin,
        "-v", ESPEAK_VOICE or "es",
        "-s", str(rate),
        "-p", ESPEAK_PITCH,
        "-a", ESPEAK_AMP,
        "-g", ESPEAK_GAP,
        "-w", str(wav_path),
        text,
    ]
    subprocess.run(cmd, check=True)


# -------------------------
# Audio -> M4A
# -------------------------
def _to_m4a(input_audio: Path, out_m4a: Path) -> None:
    out_m4a.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(input_audio), "-c:a", "aac", "-b:a", AUDIO_BITRATE, str(out_m4a)],
        check=True,
    )


def speak(text: str, out_m4a: Path, rate: int = DEFAULT_RATE) -> None:
    out_m4a.parent.mkdir(parents=True, exist_ok=True)

    # macOS
    if sys.platform == "darwin" and Path("/usr/bin/say").exists():
        voice = _pick_spanish_voice()
        aiff = out_m4a.with_suffix(".aiff")

        say_cmd = ["/usr/bin/say"]
        if voice:
            say_cmd += ["-v", voice]
        say_cmd += ["-r", str(rate), text, "-o", str(aiff)]
        subprocess.run(say_cmd, check=True)

        subprocess.run(["ffmpeg", "-y", "-i", str(aiff), str(out_m4a)], check=True)
        aiff.unlink(missing_ok=True)
        print(f"TTS_ENGINE=macos_say voice={voice}", flush=True)
        return

    # Linux/Render
    # 1) Edge TTS (neural) => evita voz robótica
    if _have_edge_tts():
        try:
            tmp_audio = Path(EDGE_OUTPUT)
            _edge_tts_to_audio(text, tmp_audio)
            _to_m4a(tmp_audio, out_m4a)
            tmp_audio.unlink(missing_ok=True)
            print(f"TTS_ENGINE=edge_tts voice={EDGE_VOICE} rate={_normalize_edge_percent(EDGE_RATE)} pitch={_normalize_edge_pitch(EDGE_PITCH)}", flush=True)
            return
        except Exception as e:
            print(f"TTS_ENGINE=edge_tts_failed error={e}", flush=True)

    # 2) Piper
    wav = out_m4a.with_suffix(".wav")
    if _have_piper():
        try:
            _piper_to_wav(text, wav)
            _to_m4a(wav, out_m4a)
            wav.unlink(missing_ok=True)
            print(f"TTS_ENGINE=piper model={PIPER_MODEL}", flush=True)
            return
        except Exception as e:
            print(f"TTS_ENGINE=piper_failed error={e}", flush=True)

    # 3) espeak fallback
    _espeak_to_wav(text, wav, rate=rate)
    _to_m4a(wav, out_m4a)
    wav.unlink(missing_ok=True)
    print(f"TTS_ENGINE=espeak voice={ESPEAK_VOICE}", flush=True)


def main():
    latest = json.loads(LATEST_JSON.read_text(encoding="utf-8"))
    last_ok = _load_last_ok_anyhow()
    data = _merge_with_fallback(latest, last_ok)

    text = build_text_from_json(data)
    Path("out").mkdir(exist_ok=True)
    speak(text, OUT_M4A)


if __name__ == "__main__":
    main()