import datetime as dt
import fcntl
import json
import os
import subprocess
import threading
from functools import wraps
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from flask import (Flask, jsonify, redirect, render_template_string, request,
                   session, url_for)
from werkzeug.security import check_password_hash

# =========================
# Flask app
# =========================
app = Flask(__name__)

# =========================
# Auth (sin DB)
# =========================
ADMIN_USER = os.getenv("ADMIN_USER", "admin").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "").strip()

# IMPORTANTÍSIMO: setea SECRET_KEY en Render (si no, cada restart mata la sesión)
app.secret_key = os.getenv("SECRET_KEY", "").strip() or os.urandom(24)

# =========================
# Runtime / Schedule
# =========================
RUN_TOKEN = os.getenv("RUN_TOKEN", "").strip()
TZ_NAME = os.getenv("TZ", "America/Santiago").strip() or "America/Santiago"

RUN_HOUR = int(os.getenv("RUN_HOUR", "7"))

# ✅ slots dentro de la hora (por defecto 0 y 30)
RUN_MINUTES = os.getenv("RUN_MINUTES", "0,30").strip()

RUN_WINDOW_MINUTES = int(os.getenv("RUN_WINDOW_MINUTES", "10"))
ALLOW_FORCE = os.getenv("ALLOW_FORCE", "1") == "1"

RUNTIME_DIR = Path(os.getenv("RUNTIME_DIR", "/tmp/finanzaschile"))
STATE_FILE = RUNTIME_DIR / "state.json"
LOCK_FILE = RUNTIME_DIR / "run.lock"
LOG_FILE = RUNTIME_DIR / "last_run.log"

# Limita crecimiento del log para NO reventar RAM (admin tail)
MAX_LOG_BYTES = int(os.getenv("MAX_LOG_BYTES", "1000000"))  # 1MB default
TAIL_BYTES = int(os.getenv("TAIL_BYTES", "250000"))  # 250KB default

BASE_DIR = Path(__file__).resolve().parent
ENAP_FILE = BASE_DIR / "sources" / "enap_semana.json"
LATEST_JSON = BASE_DIR / "data" / "latest.json"

IS_RENDER = bool(os.getenv("RENDER")) or bool(os.getenv("RENDER_SERVICE_ID"))
SHORT_ONLY = os.getenv("SHORT_ONLY", "1" if IS_RENDER else "0") == "1"

# ✅ Nuevo: duplicar el stream del pipeline a logs de Render (stdout)
LOG_TO_STDOUT = os.getenv("LOG_TO_STDOUT", "1" if IS_RENDER else "0") == "1"

_thread_guard = threading.Lock()
_background_thread = None


def _password_ok(pw: str) -> bool:
    pw = (pw or "").strip()
    if not pw:
        return False
    if ADMIN_PASSWORD_HASH:
        return check_password_hash(ADMIN_PASSWORD_HASH, pw)
    if ADMIN_PASSWORD:
        return pw == ADMIN_PASSWORD
    return False


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)

    return wrapper


def _tz_now() -> dt.datetime:
    try:
        from zoneinfo import ZoneInfo  # py3.9+
        return dt.datetime.now(ZoneInfo(TZ_NAME))
    except Exception:
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


def _truncate_log_if_needed() -> None:
    try:
        if not LOG_FILE.exists():
            return
        sz = LOG_FILE.stat().st_size
        if sz <= MAX_LOG_BYTES:
            return

        # conservamos solo los últimos TAIL_BYTES
        keep = min(TAIL_BYTES, sz)
        with LOG_FILE.open("rb") as f:
            f.seek(-keep, os.SEEK_END)
            chunk = f.read(keep)

        # corta a líneas completas
        text = chunk.decode("utf-8", errors="ignore")
        lines = text.splitlines()
        out = "\n".join(lines[-2000:]) + "\n"

        tmp = LOG_FILE.with_suffix(".tmp")
        tmp.write_text(out, encoding="utf-8")
        tmp.replace(LOG_FILE)
    except Exception:
        return


def _append_log(line: str) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")
    _truncate_log_if_needed()


def _tail_log(n_lines: int = 250) -> str:
    """
    Lee solo el final del archivo (bytes), evita cargar el log completo a RAM.
    """
    try:
        if not LOG_FILE.exists():
            return ""
        sz = LOG_FILE.stat().st_size
        if sz <= 0:
            return ""

        read_bytes = min(TAIL_BYTES, sz)
        with LOG_FILE.open("rb") as f:
            f.seek(-read_bytes, os.SEEK_END)
            chunk = f.read(read_bytes)

        text = chunk.decode("utf-8", errors="ignore")
        lines = text.splitlines()
        return "\n".join(lines[-n_lines:])
    except Exception:
        return ""


def _read_latest_json() -> Dict:
    try:
        if not LATEST_JSON.exists():
            return {}
        return json.loads(LATEST_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _run(cmd: List[str], extra_env: Optional[Dict[str, str]] = None) -> Tuple[int, str, str]:
    """
    Ejecuta cmd y streamea salida línea-a-línea al LOG_FILE.
    Si LOG_TO_STDOUT=1, duplica también a stdout (Render Logs).
    """
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
        env=env,
    )

    upload_lines: List[str] = []
    if p.stdout:
        for line in p.stdout:
            s = (line or "").rstrip()
            if not s:
                continue

            _append_log(s)

            if LOG_TO_STDOUT:
                # ✅ esto hace que Render muestre el proceso en tiempo real
                print(s, flush=True)

            if s.startswith("UPLOAD_RESULT "):
                upload_lines.append(s)
            if s.startswith("UPLOAD_SKIPPED "):
                upload_lines.append(s)

    code = p.wait()
    return code, "\n".join(upload_lines), ""


def _acquire_lock_nonblocking():
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    fp = LOCK_FILE.open("w")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fp
    except BlockingIOError:
        fp.close()
        return None


# =========================
# Schedule slots (07:00 / 07:30)
# =========================
def _parse_run_minutes() -> List[int]:
    out: List[int] = []
    for part in (RUN_MINUTES or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            m = int(part)
            if 0 <= m <= 59:
                out.append(m)
        except Exception:
            pass
    out = sorted(set(out))
    return out or [0]


def _match_slot(now: dt.datetime) -> Optional[int]:
    """
    Si now cae dentro de la ventana de algún slot (RUN_MINUTES),
    devuelve el minuto del slot. Si no, None.
    """
    if now.hour != RUN_HOUR:
        return None

    win = max(1, RUN_WINDOW_MINUTES)
    for m in _parse_run_minutes():
        if m <= now.minute < (m + win):
            return m
    return None


def _within_run_window(now: dt.datetime) -> bool:
    is_weekday = now.weekday() <= 4  # lun-vie
    if not is_weekday:
        return False
    return _match_slot(now) is not None


def _slot_profile(slot_minute: int) -> str:
    # 0 => short, 30 => normal
    if slot_minute == 0:
        return "short"
    if slot_minute == 30:
        return "normal"
    return "short"


def _should_run(now: dt.datetime, state: Dict) -> Tuple[bool, str]:
    """
    Evita doble corrida del mismo slot.
    Guarda y usa un run_key: YYYY-MM-DD@07:00 o YYYY-MM-DD@07:30
    """
    slot = _match_slot(now)
    if slot is None:
        return False, "outside_schedule"

    today_iso = now.date().isoformat()
    run_key = f"{today_iso}@{RUN_HOUR:02d}:{slot:02d}"

    last_key = (state.get("last_success_run_key") or "").strip()
    if last_key == run_key:
        return False, "already_ran_this_slot"

    state["_pending_slot"] = int(slot)
    state["_pending_run_key"] = run_key
    _write_state(state)

    return True, "ok_to_run"


def _pipeline_steps():
    return [
        ("fetch_to_json", ["python", "fetch_to_json.py"]),
        ("render_panel", ["python", "render_panel.py"]),
        ("voice", ["python", "voice_from_json.py"]),
        ("make_video", ["bash", "make_video.sh"]),
        ("upload", ["python", "upload_to_youtube.py"]),
    ]


def _parse_upload_results(stdout: str, stderr: str) -> List[Dict]:
    results = []
    text = (stdout or "") + "\n" + (stderr or "")
    for line in text.splitlines():
        line = line.strip()
        if not (line.startswith("UPLOAD_RESULT ") or line.startswith("UPLOAD_SKIPPED ")):
            continue

        kind_tag = "UPLOAD_RESULT " if line.startswith("UPLOAD_RESULT ") else "UPLOAD_SKIPPED "
        payload = line[len(kind_tag):].strip()

        parts = payload.split()
        d = {"event": "result" if kind_tag == "UPLOAD_RESULT " else "skipped"}
        for p in parts:
            if "=" not in p:
                continue
            k, v = p.split("=", 1)
            d[k.strip()] = v.strip()

        if d.get("id"):
            vid = d["id"]
            d["url_watch"] = f"https://www.youtube.com/watch?v={vid}"
            d["url_shorts"] = f"https://www.youtube.com/shorts/{vid}"
        results.append(d)
    return results


def _run_pipeline_job(started_by: str, forced: bool, forced_profile: Optional[str] = None):
    now = _tz_now()
    state = _read_state()

    # Decide perfil:
    slot = state.get("_pending_slot")
    if forced_profile in ("short", "normal"):
        profile = forced_profile
    else:
        try:
            slot_int = int(slot) if slot is not None else 0
        except Exception:
            slot_int = 0
        profile = _slot_profile(slot_int)

    run_key = (state.get("_pending_run_key") or "").strip() or f"{now.date().isoformat()}@{RUN_HOUR:02d}:{int(slot or 0):02d}"

    state["last_started_at"] = now.isoformat(timespec="seconds")
    state["last_started_by"] = started_by
    state["last_forced"] = bool(forced)
    state["last_status"] = "running"
    state["last_error_step"] = None
    state["last_profile"] = profile
    _write_state(state)

    _append_log(
        f"\n=== START {state['last_started_at']} by={started_by} forced={forced} profile={profile} run_key={run_key} ==="
    )
    if LOG_TO_STDOUT:
        print(f"=== START {state['last_started_at']} by={started_by} forced={forced} profile={profile} run_key={run_key} ===", flush=True)

    lock_fp = _acquire_lock_nonblocking()
    if not lock_fp:
        _append_log("LOCK: already running, exiting.")
        if LOG_TO_STDOUT:
            print("LOCK: already running, exiting.", flush=True)
        st = _read_state()
        st["last_status"] = "skipped_already_running"
        st["last_finished_at"] = _tz_now().isoformat(timespec="seconds")
        _write_state(st)
        return

    try:
        for name, cmd in _pipeline_steps():
            _append_log(f"[STEP] {name}: {' '.join(cmd)}")
            if LOG_TO_STDOUT:
                print(f"[STEP] {name}: {' '.join(cmd)}", flush=True)

            extra_env: Dict[str, str] = {}

            if profile == "short":
                if name == "make_video":
                    extra_env.setdefault("GENERATE_FULL_VIDEO", "0")
                    extra_env.setdefault("GENERATE_SHORT_VIDEO", "1")
                if name == "upload":
                    extra_env.setdefault("UPLOAD_NORMAL", "0")
                    extra_env.setdefault("UPLOAD_SHORT", "1")

            elif profile == "normal":
                if name == "make_video":
                    extra_env.setdefault("GENERATE_FULL_VIDEO", "1")
                    extra_env.setdefault("GENERATE_SHORT_VIDEO", "0")
                if name == "upload":
                    extra_env.setdefault("UPLOAD_NORMAL", "1")
                    extra_env.setdefault("UPLOAD_SHORT", "0")

            code, out, err = _run(cmd, extra_env=extra_env if extra_env else None)

            if name == "upload":
                uploads = _parse_upload_results(out, err)
                if uploads:
                    st = _read_state()
                    st.setdefault("uploads", [])
                    ts = _tz_now().isoformat(timespec="seconds")
                    for u in uploads:
                        u["ts"] = ts
                    st["uploads"] = (uploads + st["uploads"])[:50]
                    _write_state(st)

            if code != 0:
                st = _read_state()
                st["last_status"] = "failed"
                st["last_error_step"] = name
                st["last_finished_at"] = _tz_now().isoformat(timespec="seconds")
                _write_state(st)
                _append_log(f"=== FAIL step={name} code={code} ===")
                if LOG_TO_STDOUT:
                    print(f"=== FAIL step={name} code={code} ===", flush=True)
                return

        finished = _tz_now()
        st = _read_state()
        st["last_status"] = "success"
        st["last_finished_at"] = finished.isoformat(timespec="seconds")
        st["last_success_date"] = finished.strftime("%d-%m-%Y")
        st["last_success_run_key"] = run_key
        st.pop("_pending_slot", None)
        st.pop("_pending_run_key", None)
        _write_state(st)
        _append_log(f"=== SUCCESS {st['last_finished_at']} ===")
        if LOG_TO_STDOUT:
            print(f"=== SUCCESS {st['last_finished_at']} ===", flush=True)

    finally:
        try:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            lock_fp.close()
        except Exception:
            pass


def _start_background_job(started_by: str, forced: bool, forced_profile: Optional[str] = None) -> bool:
    global _background_thread
    with _thread_guard:
        if _background_thread and _background_thread.is_alive():
            return False
        _background_thread = threading.Thread(
            target=_run_pipeline_job,
            args=(started_by, forced, forced_profile),
            daemon=True,
        )
        _background_thread.start()
        return True


# =========================
# Fuel file helpers
# =========================
def _read_enap() -> Dict:
    if ENAP_FILE.exists():
        try:
            return json.loads(ENAP_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _write_enap(data: Dict) -> None:
    ENAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ENAP_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(ENAP_FILE)


def _safe_int(x: str) -> Optional[int]:
    x = (x or "").strip().replace(".", "").replace(",", "")
    if not x:
        return None
    try:
        return int(float(x))
    except Exception:
        return None


# =========================
# Routes: base
# =========================
@app.get("/")
def home():
    if session.get("logged_in"):
        return redirect(url_for("admin"))
    return redirect(url_for("login"))


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/status")
def status():
    state = _read_state()
    return jsonify(
        {
            "ok": True,
            "tz": TZ_NAME,
            "run_hour": RUN_HOUR,
            "run_minutes": _parse_run_minutes(),
            "run_window_minutes": RUN_WINDOW_MINUTES,
            "short_only": SHORT_ONLY,
            "log_to_stdout": LOG_TO_STDOUT,
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

    forced_profile = (request.args.get("slot") or "").strip().lower()
    if forced_profile not in ("short", "normal"):
        forced_profile = None

    if forced:
        started = _start_background_job(started_by="force", forced=True, forced_profile=forced_profile)
        return jsonify(
            {
                "ok": True,
                "forced": True,
                "started": started,
                "now": now.isoformat(timespec="seconds"),
                "profile": forced_profile or "auto",
            }
        )

    should, reason = _should_run(now, state)
    if not should:
        slot = _match_slot(now)
        return jsonify(
            {
                "ok": True,
                "started": False,
                "reason": reason,
                "now": now.isoformat(timespec="seconds"),
                "weekday": now.weekday(),
                "hour": now.hour,
                "minute": now.minute,
                "slot": slot,
                "profile": _slot_profile(int(slot)) if slot is not None else None,
                "last_success_date": state.get("last_success_date"),
                "last_success_run_key": state.get("last_success_run_key"),
            }
        )

    started = _start_background_job(started_by="schedule", forced=False, forced_profile=None)
    return jsonify(
        {
            "ok": True,
            "started": started,
            "reason": "started" if started else "already_running_in_process",
            "now": now.isoformat(timespec="seconds"),
        }
    )


# =========================
# Routes: login/UI
# =========================
LOGIN_HTML = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Login - Finanzas Chile</title>
  <style>
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial;background:#0A1D36;color:#fff;margin:0}
    .wrap{max-width:420px;margin:60px auto;padding:20px}
    .card{background:#0E2C5A;border:1px solid #5CA9FF;border-radius:16px;padding:18px}
    label{display:block;margin:10px 0 6px;color:#CFE6FF}
    input{width:100%;padding:12px;border-radius:12px;border:1px solid #78B0FF;background:#0B2B57;color:#fff}
    button{margin-top:14px;width:100%;padding:12px;border-radius:12px;border:0;background:#5CA9FF;color:#001a33;font-weight:700}
    .err{color:#ffb4b4;margin-top:10px}
    .muted{color:#BFD8FF;font-size:13px;margin-top:10px}
  </style>
</head>
<body>
  <div class="wrap">
    <h2>🔐 Panel Finanzas Chile</h2>
    <div class="card">
      <form method="post">
        <label>Usuario</label>
        <input name="user" autocomplete="username" required />
        <label>Password</label>
        <input name="password" type="password" autocomplete="current-password" required />
        <button type="submit">Entrar</button>
        {% if error %}<div class="err">{{ error }}</div>{% endif %}
        <div class="muted">Define ADMIN_USER + ADMIN_PASSWORD_HASH (recomendado) o ADMIN_PASSWORD.</div>
      </form>
    </div>
  </div>
</body>
</html>
"""


@app.route("/login", methods=["GET", "POST"])
def login():
    err = None
    if request.method == "POST":
        u = (request.form.get("user") or "").strip()
        pw = (request.form.get("password") or "").strip()
        if u == ADMIN_USER and _password_ok(pw):
            session["logged_in"] = True
            nxt = request.args.get("next") or url_for("admin")
            return redirect(nxt)
        err = "Credenciales inválidas."
    return render_template_string(LOGIN_HTML, error=err)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


ADMIN_HTML = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Admin - Finanzas Chile</title>
  <style>
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial;background:#0A1D36;color:#fff;margin:0}
    a{color:#9FC5FF;text-decoration:none}
    .wrap{max-width:1100px;margin:30px auto;padding:0 14px}
    .top{display:flex;justify-content:space-between;align-items:center;gap:12px}
    .card{background:#0E2C5A;border:1px solid #5CA9FF;border-radius:16px;padding:16px;margin-top:14px}
    .grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
    @media (max-width: 900px){.grid{grid-template-columns:1fr}}
    .k{color:#CFE6FF}
    .row{display:flex;gap:10px;flex-wrap:wrap}
    .pill{background:#0B2B57;border:1px solid #78B0FF;border-radius:999px;padding:6px 10px}
    pre{white-space:pre-wrap;background:#06162b;border:1px solid #2a5ea6;border-radius:12px;padding:12px;overflow:auto;max-height:420px}
    table{width:100%;border-collapse:collapse}
    th,td{border-bottom:1px solid rgba(92,169,255,.25);padding:10px;text-align:left}
    th{color:#CFE6FF}
    .btn{display:inline-block;background:#5CA9FF;color:#001a33;font-weight:700;padding:8px 12px;border-radius:12px}
    .muted{color:#BFD8FF;font-size:13px}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <h2>📊 Admin - Finanzas Chile</h2>
      <div class="row">
        <a class="btn" href="{{ url_for('fuel') }}">⛽ Editar combustibles</a>
        <a class="btn" href="{{ url_for('admin') }}">🔄 Refresh</a>
        <a class="btn" href="{{ url_for('logout') }}">Salir</a>
      </div>
    </div>

    <div class="grid">
      <div class="card">
        <h3>Estado último run</h3>
        <div class="row">
          <div class="pill"><span class="k">status:</span> {{ state.get('last_status') }}</div>
          <div class="pill"><span class="k">started:</span> {{ state.get('last_started_at') }}</div>
          <div class="pill"><span class="k">finished:</span> {{ state.get('last_finished_at') }}</div>
          <div class="pill"><span class="k">last_success_date:</span> {{ state.get('last_success_date') }}</div>
          <div class="pill"><span class="k">run_key:</span> {{ state.get('last_success_run_key') }}</div>
          <div class="pill"><span class="k">profile:</span> {{ state.get('last_profile') }}</div>
          <div class="pill"><span class="k">error_step:</span> {{ state.get('last_error_step') }}</div>
          <div class="pill"><span class="k">short_only:</span> {{ short_only }}</div>
          <div class="pill"><span class="k">log_to_stdout:</span> {{ log_to_stdout }}</div>
        </div>
        <div style="margin-top:10px">
          <a class="btn" href="/run?token={{ run_token }}&force=1&slot=short" target="_blank">▶️ Probar SHORT</a>
          <a class="btn" href="/run?token={{ run_token }}&force=1&slot=normal" target="_blank" style="margin-left:8px">▶️ Probar VIDEO</a>
          <span class="muted" style="margin-left:10px">*usa tu RUN_TOKEN</span>
        </div>
      </div>

      <div class="card">
        <h3>latest.json (debug rápido)</h3>
        {% if latest %}
          <div class="row">
            <div class="pill"><span class="k">cobre_usd_lb:</span> {{ latest.get('cobre_usd_lb') }}</div>
            <div class="pill"><span class="k">brent_usd:</span> {{ latest.get('brent_usd') }}</div>
            <div class="pill"><span class="k">generated_at:</span> {{ latest.get('generated_at') }}</div>
          </div>
          <div class="muted" style="margin-top:8px">
            Si cobre sale None aquí, el panel va a mostrar N/D (falló el fetch en ese run).
          </div>
        {% else %}
          <div class="muted">No existe data/latest.json todavía (aún no corre fetch_to_json en este contenedor).</div>
        {% endif %}
      </div>
    </div>

    <div class="card">
      <h3>Últimos uploads detectados</h3>
      {% if uploads %}
        <table>
          <thead>
            <tr><th>Fecha</th><th>Evento</th><th>Tipo</th><th>ID</th><th>Detalle</th><th>Links</th></tr>
          </thead>
          <tbody>
            {% for u in uploads %}
            <tr>
              <td>{{ u.get('ts') }}</td>
              <td>{{ u.get('event') }}</td>
              <td>{{ u.get('kind') }}</td>
              <td>{{ u.get('id','') }}</td>
              <td>{{ u.get('reason','') }}</td>
              <td>
                {% if u.get('url_watch') %}<a href="{{ u.get('url_watch') }}" target="_blank">watch</a>{% endif %}
                {% if u.get('url_watch') and u.get('url_shorts') %}&nbsp;|&nbsp;{% endif %}
                {% if u.get('url_shorts') %}<a href="{{ u.get('url_shorts') }}" target="_blank">shorts</a>{% endif %}
              </td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      {% else %}
        <div class="muted">Aún no hay uploads guardados. (Se llena cuando corre el step upload)</div>
      {% endif %}
    </div>

    <div class="card">
      <h3>Últimas líneas del log</h3>
      <pre>{{ log_tail }}</pre>
    </div>
  </div>
</body>
</html>
"""


@app.get("/admin")
@login_required
def admin():
    state = _read_state()
    uploads = state.get("uploads") or []
    run_token = os.getenv("RUN_TOKEN", "").strip()
    latest = _read_latest_json()
    return render_template_string(
        ADMIN_HTML,
        state=state,
        uploads=uploads[:20],
        log_tail=_tail_log(250),
        run_token=run_token,
        latest=latest,
        short_only=SHORT_ONLY,
        log_to_stdout=LOG_TO_STDOUT,
    )


FUEL_HTML = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Combustibles - Finanzas Chile</title>
  <style>
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial;background:#0A1D36;color:#fff;margin:0}
    a{color:#9FC5FF;text-decoration:none}
    .wrap{max-width:700px;margin:30px auto;padding:0 14px}
    .card{background:#0E2C5A;border:1px solid #5CA9FF;border-radius:16px;padding:16px;margin-top:14px}
    label{display:block;margin:10px 0 6px;color:#CFE6FF}
    input{width:100%;padding:12px;border-radius:12px;border:1px solid #78B0FF;background:#0B2B57;color:#fff}
    button{margin-top:14px;padding:12px 14px;border-radius:12px;border:0;background:#5CA9FF;color:#001a33;font-weight:700}
    .row{display:flex;gap:10px;flex-wrap:wrap}
    .ok{color:#b7ffcf}
    .err{color:#ffb4b4}
    .btn{display:inline-block;background:#5CA9FF;color:#001a33;font-weight:700;padding:8px 12px;border-radius:12px}
    .muted{color:#BFD8FF;font-size:13px}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="row" style="justify-content:space-between;align-items:center">
      <h2>⛽ Editar combustibles</h2>
      <div class="row">
        <a class="btn" href="{{ url_for('admin') }}">← Volver</a>
        <a class="btn" href="{{ url_for('logout') }}">Salir</a>
      </div>
    </div>

    <div class="card">
      <div class="muted">Edita <code>sources/enap_semana.json</code>.</div>

      {% if msg %}<div class="ok" style="margin-top:10px">✅ {{ msg }}</div>{% endif %}
      {% if error %}<div class="err" style="margin-top:10px">❌ {{ error }}</div>{% endif %}

      <form method="post">
        <label>Gasolina 93 (CLP/L)</label>
        <input name="g93" value="{{ enap.get('g93_clp_l','') }}" placeholder="Ej: 1250" />

        <label>Gasolina 95 (CLP/L)</label>
        <input name="g95" value="{{ enap.get('g95_clp_l','') }}" placeholder="Ej: 1285" />

        <label>Gasolina 97 (CLP/L)</label>
        <input name="g97" value="{{ enap.get('g97_clp_l','') }}" placeholder="Ej: 1320" />

        <label>Diésel (CLP/L)</label>
        <input name="diesel" value="{{ enap.get('diesel_clp_l','') }}" placeholder="Ej: 1090" />

        <label>Vigencia (opcional)</label>
        <input name="vigencia" value="{{ enap.get('vigencia','') }}" placeholder="(vacío si no quieres fechas)" />

        <button type="submit">Guardar</button>
      </form>
    </div>
  </div>
</body>
</html>
"""


@app.route("/admin/fuel", methods=["GET", "POST"])
@login_required
def fuel():
    msg = None
    err = None
    enap = _read_enap() or {}

    if request.method == "POST":
        g93 = _safe_int(request.form.get("g93", ""))
        g95 = _safe_int(request.form.get("g95", ""))
        g97 = _safe_int(request.form.get("g97", ""))
        diesel = _safe_int(request.form.get("diesel", ""))
        vig = (request.form.get("vigencia") or "").strip()

        payload = {
            "vigencia": vig if vig else "",
            "g93_clp_l": g93,
            "g95_clp_l": g95,
            "g97_clp_l": g97,
            "diesel_clp_l": diesel,
        }

        try:
            _write_enap(payload)
            enap = payload
            msg = "Combustibles actualizados."
        except Exception as e:
            err = f"No pude guardar enap_semana.json: {e}"

    return render_template_string(FUEL_HTML, enap=enap, msg=msg, error=err)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))