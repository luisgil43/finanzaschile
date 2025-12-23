# server.py
import datetime as dt
import fcntl
import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Dict, Tuple

from flask import Flask, jsonify, request

# =========================
# Flask app
# =========================
app = Flask(__name__)

RUN_TOKEN = os.getenv("RUN_TOKEN", "").strip()

# Zona horaria (de Render) - ya la tienes en env TZ=America/Santiago
TZ_NAME = os.getenv("TZ", "America/Santiago").strip() or "America/Santiago"

# Ventana de ejecución a las 7am: permite que Better Stack pegue 1-5-10 min y no falle por segundos
# Por defecto: 10 minutos (07:00:00 hasta 07:09:59)
RUN_HOUR = int(os.getenv("RUN_HOUR", "7"))
RUN_WINDOW_MINUTES = int(os.getenv("RUN_WINDOW_MINUTES", "10"))

# Permite forzar ejecución manual aunque no sea hora (solo con token + force=1)
ALLOW_FORCE = os.getenv("ALLOW_FORCE", "1") == "1"

# Estado persistido (best-effort) en el filesystem del contenedor
# (en Render free el FS es efímero si reinician el contenedor, pero dentro del día normalmente basta)
RUNTIME_DIR = Path(os.getenv("RUNTIME_DIR", "/tmp/finanzaschile"))
STATE_FILE = RUNTIME_DIR / "state.json"
LOCK_FILE = RUNTIME_DIR / "run.lock"
LOG_FILE = RUNTIME_DIR / "last_run.log"

# Evita doble start dentro del mismo proceso
_thread_guard = threading.Lock()
_background_thread = None


def _tz_now() -> dt.datetime:
    try:
        from zoneinfo import ZoneInfo  # py3.9+
        return dt.datetime.now(ZoneInfo(TZ_NAME))
    except Exception:
        # fallback: naive localtime (no ideal, pero no rompe)
        return dt.datetime.now()


def _read_state() -> Dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _write_state(state: Dict) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def _append_log(line: str) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")


def _run(cmd) -> Tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, (p.stdout or ""), (p.stderr or "")


def _acquire_lock_nonblocking():
    """
    Lock de proceso/worker (fcntl). Si ya hay uno corriendo, no ejecuta.
    """
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    fp = LOCK_FILE.open("w")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fp
    except BlockingIOError:
        fp.close()
        return None


def _within_run_window(now: dt.datetime) -> bool:
    # Lunes=0 ... Domingo=6
    is_weekday = now.weekday() <= 4
    if not is_weekday:
        return False
    if now.hour != RUN_HOUR:
        return False
    # Ventana: 07:00..07:(window-1)
    return 0 <= now.minute < max(1, RUN_WINDOW_MINUTES)


def _should_run(now: dt.datetime, state: Dict) -> Tuple[bool, str]:
    """
    Decide si corresponde correr HOY.
    """
    if not _within_run_window(now):
        return False, "outside_schedule"

    today = now.date().isoformat()
    last_ok = (state.get("last_success_date") or "").strip()
    if last_ok == today:
        return False, "already_ran_today"

    return True, "ok_to_run"


def _pipeline_steps():
    return [
        ("fetch_to_json", ["python", "fetch_to_json.py"]),
        ("render_panel", ["python", "render_panel.py"]),
        ("voice", ["python", "voice_from_json.py"]),
        ("make_video", ["bash", "make_video.sh"]),
        ("upload", ["python", "upload_to_youtube.py"]),
    ]


def _run_pipeline_job(started_by: str, forced: bool):
    """
    Corre el pipeline en background. Actualiza state + log.
    """
    now = _tz_now()
    state = _read_state()

    state["last_started_at"] = now.isoformat(timespec="seconds")
    state["last_started_by"] = started_by
    state["last_forced"] = bool(forced)
    state["last_status"] = "running"
    state["last_error_step"] = None
    _write_state(state)

    _append_log(f"\n=== START {state['last_started_at']} by={started_by} forced={forced} ===")

    lock_fp = _acquire_lock_nonblocking()
    if not lock_fp:
        _append_log("LOCK: already running, exiting.")
        # Marca como "skipped" (no es error)
        st = _read_state()
        st["last_status"] = "skipped_already_running"
        st["last_finished_at"] = _tz_now().isoformat(timespec="seconds")
        _write_state(st)
        return

    try:
        for name, cmd in _pipeline_steps():
            _append_log(f"[STEP] {name}: {' '.join(cmd)}")
            code, out, err = _run(cmd)
            if out:
                _append_log(out)
            if err:
                _append_log(err)

            if code != 0:
                st = _read_state()
                st["last_status"] = "failed"
                st["last_error_step"] = name
                st["last_finished_at"] = _tz_now().isoformat(timespec="seconds")
                _write_state(st)
                _append_log(f"=== FAIL step={name} code={code} ===")
                return

        finished = _tz_now()
        st = _read_state()
        st["last_status"] = "success"
        st["last_finished_at"] = finished.isoformat(timespec="seconds")
        st["last_success_date"] = finished.date().isoformat()
        _write_state(st)
        _append_log(f"=== SUCCESS {st['last_finished_at']} ===")

    finally:
        try:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            lock_fp.close()
        except Exception:
            pass


def _start_background_job(started_by: str, forced: bool) -> bool:
    global _background_thread
    with _thread_guard:
        if _background_thread and _background_thread.is_alive():
            return False
        _background_thread = threading.Thread(
            target=_run_pipeline_job,
            args=(started_by, forced),
            daemon=True,
        )
        _background_thread.start()
        return True


# =========================
# Routes
# =========================
@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/status")
def status():
    state = _read_state()
    # Exponer resumen (sin secretos)
    return jsonify(
        {
            "ok": True,
            "tz": TZ_NAME,
            "run_hour": RUN_HOUR,
            "run_window_minutes": RUN_WINDOW_MINUTES,
            "state": state,
            "log_file": str(LOG_FILE),
        }
    )


@app.get("/run")
def run_daily():
    token = (request.args.get("token") or "").strip()
    if RUN_TOKEN and token != RUN_TOKEN:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    now = _tz_now()
    state = _read_state()

    forced = (request.args.get("force") == "1") and ALLOW_FORCE

    # Si es force, solo evitamos repetir si ya está corriendo
    if forced:
        started = _start_background_job(started_by="force", forced=True)
        return jsonify(
            {
                "ok": True,
                "forced": True,
                "started": started,
                "now": now.isoformat(timespec="seconds"),
            }
        )

    should, reason = _should_run(now, state)
    if not should:
        return jsonify(
            {
                "ok": True,
                "started": False,
                "reason": reason,
                "now": now.isoformat(timespec="seconds"),
                "weekday": now.weekday(),
                "hour": now.hour,
                "minute": now.minute,
                "last_success_date": state.get("last_success_date"),
            }
        )

    started = _start_background_job(started_by="schedule", forced=False)
    return jsonify(
        {
            "ok": True,
            "started": started,
            "reason": "started" if started else "already_running_in_process",
            "now": now.isoformat(timespec="seconds"),
        }
    )


if __name__ == "__main__":
    # Para pruebas locales (Render usa gunicorn)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))