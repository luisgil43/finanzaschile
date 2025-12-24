"""
Genera la locución en M4A desde data/latest.json.

- macOS: usa /usr/bin/say (muy buena calidad)
- Linux/Render: usa Piper (neural TTS) si está disponible
  y cae a espeak-ng/espeak como fallback.

ENDURECIDO:
- Tolera valores None
- Si falta cobre/brent/etc. intenta fallback desde "last_ok"
  (via fetch_finanzas_cl.load_last_ok() o data/last_ok.json si existe)
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

# ===== Config paths =====
LATEST_JSON = Path(os.getenv("LATEST_JSON_PATH", "data/latest.json"))
LAST_OK_JSON = Path(os.getenv("LAST_OK_JSON_PATH", "data/last_ok.json"))  # opcional
OUT_M4A = Path(os.getenv("VOICE_OUT_PATH", "out/locucion.m4a"))

# ===== macOS say =====
DEFAULT_RATE = int(os.getenv("SPEAK_RATE", "175"))  # 160–190 natural

PREFERRED_ES_VOICES = [
    "Paulina", "Mónica", "Sandy", "Shelley", "Reed", "Jorge", "Diego", "Juan",
    "Rocko", "Grandma", "Grandpa", "Flo", "Eddy",
]
FALLBACK_VOICE = os.getenv("VOICE_NAME", "Paulina")

# ===== Piper (Render/Linux) =====
USE_PIPER = os.getenv("USE_PIPER", "1").strip() == "1"
PIPER_BIN = os.getenv("PIPER_BIN", "piper").strip() or "piper"
PIPER_MODEL = os.getenv("PIPER_MODEL", "").strip()
PIPER_CONFIG = os.getenv("PIPER_CONFIG", "").strip()

# Ajustes opcionales de naturalidad (Piper)
PIPER_LENGTH_SCALE = os.getenv("PIPER_LENGTH_SCALE", "1.02").strip()  # >1 = más lento
PIPER_NOISE_SCALE = os.getenv("PIPER_NOISE_SCALE", "").strip()
PIPER_NOISE_W_SCALE = os.getenv("PIPER_NOISE_W_SCALE", "").strip()
PIPER_SENTENCE_SILENCE = os.getenv("PIPER_SENTENCE_SILENCE", "0.18").strip()

# ===== Espeak fallback (si no hay Piper) =====
ESPEAK_VOICE = os.getenv("ESPEAK_VOICE", "es+f3")  # suele sonar más agradable que "es"
ESPEAK_PITCH = os.getenv("ESPEAK_PITCH", "55")     # 0-99 (50 ~ neutral)
ESPEAK_AMP = os.getenv("ESPEAK_AMP", "115")        # 0-200
ESPEAK_GAP = os.getenv("ESPEAK_GAP", "6")          # ms aprox

# ffmpeg audio
AUDIO_BITRATE = os.getenv("VOICE_AAC_BITRATE", "128k").strip()
AUDIO_GAIN_DB = os.getenv("VOICE_GAIN_DB", "0").strip()  # "0" sin gain, ej "2.5"


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
# Texto: limpieza + números
# -------------------------
def _clean_spaces(s: str) -> str:
    s = re.sub(r"\s+", " ", (s or "").strip())
    return s


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


# Conversión simple a palabras (ES) para sonar más natural.
_UNITS = [
    "cero","uno","dos","tres","cuatro","cinco","seis","siete","ocho","nueve"
]
_TEENS = {
    10:"diez",11:"once",12:"doce",13:"trece",14:"catorce",15:"quince",
    16:"dieciséis",17:"diecisiete",18:"dieciocho",19:"diecinueve"
}
_TENS = {
    20:"veinte",30:"treinta",40:"cuarenta",50:"cincuenta",
    60:"sesenta",70:"setenta",80:"ochenta",90:"noventa"
}
_HUNDREDS = {
    100:"cien",200:"doscientos",300:"trescientos",400:"cuatrocientos",500:"quinientos",
    600:"seiscientos",700:"setecientos",800:"ochocientos",900:"novecientos"
}

def _words_1_99(n: int) -> str:
    if n < 10:
        return _UNITS[n]
    if 10 <= n <= 19:
        return _TEENS[n]
    if 20 <= n <= 29:
        if n == 20:
            return "veinte"
        # veintiuno, veintidós, ...
        return "veinti" + _UNITS[n - 20]
    tens = (n // 10) * 10
    unit = n % 10
    if unit == 0:
        return _TENS[tens]
    return f"{_TENS[tens]} y {_UNITS[unit]}"

def _words_1_999(n: int) -> str:
    if n < 100:
        return _words_1_99(n)
    if n in _HUNDREDS:
        return _HUNDREDS[n]
    hundreds = (n // 100) * 100
    rest = n % 100
    if hundreds == 100:
        return "ciento " + _words_1_99(rest)
    return _HUNDREDS[hundreds] + " " + _words_1_99(rest)

def _num_to_words_es(n: int) -> str:
    if n < 0:
        return "menos " + _num_to_words_es(abs(n))
    if n < 1000:
        return _words_1_999(n)
    if n < 1_000_000:
        thousands = n // 1000
        rest = n % 1000
        if thousands == 1:
            head = "mil"
        else:
            head = _words_1_999(thousands) + " mil"
        return head if rest == 0 else head + " " + _words_1_999(rest)
    if n < 1_000_000_000:
        millions = n // 1_000_000
        rest = n % 1_000_000
        if millions == 1:
            head = "un millón"
        else:
            head = _num_to_words_es(millions) + " millones"
        return head if rest == 0 else head + " " + _num_to_words_es(rest)
    # por si algún día explota
    return str(n)

def _say_int(n: Optional[int], nd: str = "no disponible") -> str:
    if n is None:
        return nd
    return _num_to_words_es(n)

def _say_float_2(x: Optional[float], nd: str = "no disponible") -> str:
    if x is None:
        return nd
    # 2 decimales: "cinco coma sesenta y uno"
    ip = int(x)
    dp = int(round(abs(x - ip) * 100))
    if dp == 0:
        return _num_to_words_es(ip)
    return f"{_num_to_words_es(ip)} coma {_num_to_words_es(dp)}"


def build_text_from_json(data: dict) -> str:
    dolar = _to_int_like(data.get("dolar_clp"))
    uf = _to_int_like(data.get("uf_clp"))
    btc = _to_int_like(data.get("btc_usd"))
    eth = _to_int_like(data.get("eth_usd"))
    cobre = _to_float(data.get("cobre_usd_lb"))
    brent = _to_float(data.get("brent_usd"))

    # Frases cortas + pausas naturales
    parts = [
        "Finanzas Hoy Chile.",
        f"Dólar: {_say_int(dolar)} pesos.",
        f"UF: {_say_int(uf)} pesos.",
        f"Cobre: {_say_float_2(cobre)} dólares por libra.",
        f"Petróleo Brent: {_say_float_2(brent)} dólares por barril.",
        f"Bitcoin: {_say_int(btc)} dólares.",
        f"Ethereum: {_say_int(eth)} dólares.",
    ]
    # newlines ayudan a algunas voces
    return _clean_spaces("\n".join(parts))


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


# -------------------------
# Piper (Linux/Render)
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
    # config puede faltar: piper igual puede funcionar sin -c, pero preferimos tenerlo
    return True


def _piper_to_wav(text: str, wav_path: Path) -> None:
    wav_path.parent.mkdir(parents=True, exist_ok=True)

    tmp_txt = wav_path.with_suffix(".txt")
    tmp_txt.write_text(text, encoding="utf-8")

    cmd = [
        PIPER_BIN,
        "--model", PIPER_MODEL,
        "--input-file", str(tmp_txt),
        "--output-file", str(wav_path),
        "--length-scale", str(PIPER_LENGTH_SCALE),
        "--sentence-silence", str(PIPER_SENTENCE_SILENCE),
    ]

    if PIPER_CONFIG and Path(PIPER_CONFIG).exists():
        cmd += ["--config", PIPER_CONFIG]

    if PIPER_NOISE_SCALE:
        cmd += ["--noise-scale", str(PIPER_NOISE_SCALE)]
    if PIPER_NOISE_W_SCALE:
        cmd += ["--noise-w-scale", str(PIPER_NOISE_W_SCALE)]

    subprocess.run(cmd, check=True)

    try:
        tmp_txt.unlink(missing_ok=True)
    except Exception:
        pass


# -------------------------
# Espeak fallback (Linux/Render)
# -------------------------
def _espeak_to_wav(text: str, wav_path: Path, rate: int) -> None:
    wav_path.parent.mkdir(parents=True, exist_ok=True)

    tts_bin = shutil.which("espeak-ng") or shutil.which("espeak")
    if not tts_bin:
        raise RuntimeError("No se encontró espeak-ng ni espeak en el sistema.")

    voice_try = (ESPEAK_VOICE or "es").strip()
    pitch = (ESPEAK_PITCH or "55").strip()
    amp = (ESPEAK_AMP or "115").strip()
    gap = (ESPEAK_GAP or "6").strip()

    cmd = [tts_bin, "-v", voice_try, "-s", str(rate), "-p", pitch, "-a", amp, "-g", gap, "-w", str(wav_path), text]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError:
        cmd2 = [tts_bin, "-v", "es", "-s", str(rate), "-p", pitch, "-a", amp, "-g", gap, "-w", str(wav_path), text]
        subprocess.run(cmd2, check=True)


# -------------------------
# WAV -> M4A
# -------------------------
def _wav_to_m4a(wav: Path, out_m4a: Path) -> None:
    out_m4a.parent.mkdir(exist_ok=True, parents=True)

    # Ganancia opcional
    af = []
    try:
        g = float(AUDIO_GAIN_DB)
        if abs(g) > 0.001:
            af.append(f"volume={g}dB")
    except Exception:
        pass

    cmd = ["ffmpeg", "-y", "-i", str(wav), "-c:a", "aac", "-b:a", AUDIO_BITRATE]
    if af:
        cmd += ["-af", ",".join(af)]
    cmd += [str(out_m4a)]

    subprocess.run(cmd, check=True)


# -------------------------
# speak()
# -------------------------
def speak(text: str, out_m4a: Path, rate: int = DEFAULT_RATE) -> None:
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
                ["afconvert", str(aiff), str(out_m4a), "-f", "m4af", "-d", "aac", "-b", "128000", "-q", "1"],
                check=True,
            )
        finally:
            try:
                aiff.unlink(missing_ok=True)
            except Exception:
                pass
        return

    # --- Linux/Render ---
    wav = out_m4a.with_suffix(".wav")

    # 1) Piper (neural) si está disponible
    if _have_piper():
        _piper_to_wav(text, wav)
        _wav_to_m4a(wav, out_m4a)
        try:
            wav.unlink(missing_ok=True)
        except Exception:
            pass
        return

    # 2) Fallback espeak
    _espeak_to_wav(text, wav, rate=rate)
    _wav_to_m4a(wav, out_m4a)
    try:
        wav.unlink(missing_ok=True)
    except Exception:
        pass


def main():
    latest = json.loads(LATEST_JSON.read_text(encoding="utf-8"))
    last_ok = _load_last_ok_anyhow()
    data = _merge_with_fallback(latest, last_ok)

    text = build_text_from_json(data)

    Path("out").mkdir(exist_ok=True)
    speak(text, OUT_M4A)


if __name__ == "__main__":
    main()