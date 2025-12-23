"""
Genera la locución en M4A desde data/latest.json.
- macOS: usa /usr/bin/say
- Linux/Render: usa espeak-ng (o espeak) + ffmpeg
Endurecido para tolerar valores None en el JSON (sin romper el pipeline).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

# ===== Config =====
DEFAULT_RATE = int(os.getenv("SPEAK_RATE", "175"))  # 160–190 natural
PREFERRED_ES_VOICES = [
    "Paulina", "Mónica", "Sandy", "Shelley", "Reed", "Jorge", "Diego", "Juan",
    "Rocko", "Grandma", "Grandpa", "Flo", "Eddy",
]
FALLBACK_VOICE = os.getenv("VOICE_NAME", "Paulina")


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
        # Formato típico: "Paulina es_ES ..."
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
    - macOS: usa /usr/bin/say (si existe) y convierte a .m4a
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
            aiff.unlink(missing_ok=True)
        return

    # --- Linux / Render ---
    wav = out_m4a.with_suffix(".wav")
    tts_voice = os.getenv("ESPEAK_VOICE", "es")
    tts_rate = str(rate)

    import shutil
    tts_bin = shutil.which("espeak-ng") or shutil.which("espeak")
    if not tts_bin:
        raise RuntimeError("No se encontró espeak-ng ni espeak en el sistema (Render).")

    subprocess.run([tts_bin, "-v", tts_voice, "-s", tts_rate, "-w", str(wav), text], check=True)

    try:
        subprocess.run(["ffmpeg", "-y", "-i", str(wav), "-c:a", "aac", "-b:a", "128k", str(out_m4a)], check=True)
    finally:
        wav.unlink(missing_ok=True)


def _sr(x) -> str:
    """string round: redondea números a entero en string; tolera None."""
    try:
        return str(round(float(x)))
    except Exception:
        return "N/D"


def build_text_from_json(data: dict) -> str:
    parts = []
    parts.append("Finanzas Hoy Chile.")
    parts.append(f"Dólar: {_sr(data.get('dolar_clp'))} pesos.")
    parts.append(f"UF: {_sr(data.get('uf_clp'))} pesos.")
    if data.get("cobre_usd_lb") is not None:
        try:
            parts.append(f"Cobre: {float(data['cobre_usd_lb']):.2f} dólares por libra.")
        except Exception:
            pass
    if data.get("brent_usd") is not None:
        parts.append(f"Petróleo Brent: {_sr(data.get('brent_usd'))} dólares por barril.")
    parts.append(f"Bitcoin: {_sr(data.get('btc_usd'))} dólares.")
    if data.get("eth_usd") is not None:
        parts.append(f"Ethereum: {_sr(data.get('eth_usd'))} dólares.")
    return " ".join(parts)


def main():
    data = json.loads(Path("data/latest.json").read_text(encoding="utf-8"))
    text = build_text_from_json(data)
    Path("out").mkdir(exist_ok=True)
    speak(text, Path("out/locucion.m4a"))


if __name__ == "__main__":
    main()