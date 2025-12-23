import datetime as dt
import os
import subprocess

from flask import Flask, jsonify, request

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # type: ignore

app = Flask(__name__)

RUN_TOKEN = os.getenv("RUN_TOKEN", "").strip()
LOCAL_TZ = os.getenv("TZ", "America/Santiago")


def _now_local():
    if ZoneInfo:
        try:
            return dt.datetime.now(ZoneInfo(LOCAL_TZ))
        except Exception:
            return dt.datetime.now()
    return dt.datetime.now()


def _run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, (p.stdout or ""), (p.stderr or "")


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/run")
def run_daily():
    token = (request.args.get("token") or "").strip()
    if RUN_TOKEN and token != RUN_TOKEN:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    now = _now_local()
    # Monday=0 ... Sunday=6  -> weekend >=5
    if now.weekday() >= 5:
        return jsonify({"ok": True, "skipped": True, "reason": "weekend", "local_time": now.isoformat(timespec="seconds")})

    steps = [
        ("fetch_to_json", ["python", "fetch_to_json.py"]),
        ("render_panel", ["python", "render_panel.py"]),
        ("voice", ["python", "voice_from_json.py"]),
        ("make_video", ["bash", "make_video.sh"]),
        ("upload", ["python", "upload_to_youtube.py"]),
    ]

    for name, cmd in steps:
        code, out, err = _run(cmd)
        if code != 0:
            return jsonify({"ok": False, "step": name, "stdout": out, "stderr": err}), 500

    return jsonify({"ok": True, "message": "daily run finished", "local_time": now.isoformat(timespec="seconds")})


if __name__ == "__main__":
    # Para pruebas locales (Render usa gunicorn)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))