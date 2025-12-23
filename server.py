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
from werkzeug.security import check_password_hash, generate_password_hash

# =========================
# Flask app
# =========================
app = Flask(__name__)

# =========================
# Auth (sin DB)
# =========================
# Opción A (simple): ADMIN_USER + ADMIN_PASSWORD
# Opción B (mejor): ADMIN_USER + ADMIN_PASSWORD_HASH (pbkdf2:sha256...)
ADMIN_USER = os.getenv("ADMIN_USER", "admin").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "").strip()

# SECRET_KEY para sesiones (OBLIGATORIO en Render)
app.secret_key = os.getenv("SECRET_KEY", "").strip() or os.urandom(24)

def _password_ok(pw: str) -> bool:
    pw = (pw or "").strip()
    if not pw:
        return False
    if ADMIN_PASSWORD_HASH:
        return check_password_hash(ADMIN_PASSWORD_HASH, pw)
    # fallback: password plano en env
    if ADMIN_PASSWORD:
        # comparación simple (en env igual sirve para proyecto personal)
        return pw == ADMIN_PASSWORD
    return False

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper


# =========================
# Config runtime / schedule
# =========================
RUN_TOKEN = os.getenv("RUN_TOKEN", "").strip()

# Zona horaria (Render)
TZ_NAME = os.getenv("TZ", "America/Santiago").strip() or "America/Santiago"

RUN_HOUR = int(os.getenv("RUN_HOUR", "7"))
RUN_WINDOW_MINUTES = int(os.getenv("RUN_WINDOW_MINUTES", "10"))
ALLOW_FORCE = os.getenv("ALLOW_FORCE", "1") == "1"

RUNTIME_DIR = Path(os.getenv("RUNTIME_DIR", "/tmp/finanzaschile"))
STATE_FILE = RUNTIME_DIR / "state.json"
LOCK_FILE = RUNTIME_DIR / "run.lock"
LOG_FILE = RUNTIME_DIR / "last_run.log"

# Archivo de combustibles editable
BASE_DIR = Path(__file__).resolve().parent
ENAP_FILE = BASE_DIR / "sources" / "enap_semana.json"

_thread_guard = threading.Lock()
_background_thread = None


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
    return 0 <= now.minute < max(1, RUN_WINDOW_MINUTES)


def _should_run(now: dt.datetime, state: Dict) -> Tuple[bool, str]:
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


def _parse_upload_results(stdout: str, stderr: str) -> List[Dict]:
    """
    Lee líneas tipo:
      UPLOAD_RESULT kind=normal id=XXXX title=... privacy=public
    Devuelve lista de dicts.
    """
    results = []
    text = (stdout or "") + "\n" + (stderr or "")
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("UPLOAD_RESULT "):
            continue
        payload = line[len("UPLOAD_RESULT "):].strip()
        parts = payload.split()
        d = {}
        for p in parts:
            if "=" not in p:
                continue
            k, v = p.split("=", 1)
            d[k.strip()] = v.strip()
        if d.get("id"):
            # url rápida
            vid = d["id"]
            d["url_watch"] = f"https://www.youtube.com/watch?v={vid}"
            d["url_shorts"] = f"https://www.youtube.com/shorts/{vid}"
            results.append(d)
    return results


def _run_pipeline_job(started_by: str, forced: bool):
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
        st = _read_state()
        st["last_status"] = "skipped_already_running"
        st["last_finished_at"] = _tz_now().isoformat(timespec="seconds")
        _write_state(st)
        return

    try:
        for name, cmd in _pipeline_steps():
            _append_log(f"[STEP] {name}: {' '.join(cmd)}")
            code, out, err = _run(cmd)

            # log
            if out:
                _append_log(out)
            if err:
                _append_log(err)

            # si fue upload, parsea IDs y guarda en state
            if name == "upload":
                uploads = _parse_upload_results(out, err)
                if uploads:
                    st = _read_state()
                    st.setdefault("uploads", [])
                    # agrega timestamp al resultado
                    ts = _tz_now().isoformat(timespec="seconds")
                    for u in uploads:
                        u["ts"] = ts
                    # prepend (más nuevo primero)
                    st["uploads"] = (uploads + st["uploads"])[:50]
                    _write_state(st)

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
# Fuel file helpers (sin DB)
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

def _tail_log(n: int = 200) -> str:
    try:
        if not LOG_FILE.exists():
            return ""
        lines = LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""


# =========================
# Routes: public
# =========================
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


# =========================
# Routes: admin login/UI
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
        <div class="muted">Tip: define ADMIN_USER / ADMIN_PASSWORD o ADMIN_PASSWORD_HASH en Render.</div>
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
          <div class="pill"><span class="k">error_step:</span> {{ state.get('last_error_step') }}</div>
        </div>
        <div style="margin-top:10px">
          <a class="btn" href="/run?token={{ run_token }}&force=1" target="_blank">▶️ Forzar run ahora</a>
          <span style="color:#BFD8FF;font-size:13px;margin-left:10px">*usa tu RUN_TOKEN</span>
        </div>
      </div>

      <div class="card">
        <h3>Últimos uploads detectados</h3>
        {% if uploads %}
          <table>
            <thead>
              <tr>
                <th>Fecha</th><th>Tipo</th><th>ID</th><th>Links</th>
              </tr>
            </thead>
            <tbody>
              {% for u in uploads %}
              <tr>
                <td>{{ u.get('ts') }}</td>
                <td>{{ u.get('kind') }}</td>
                <td>{{ u.get('id') }}</td>
                <td>
                  <a href="{{ u.get('url_watch') }}" target="_blank">watch</a>
                  &nbsp;|&nbsp;
                  <a href="{{ u.get('url_shorts') }}" target="_blank">shorts</a>
                </td>
              </tr>
              {% endfor %}
            </tbody>
          </table>
        {% else %}
          <div style="color:#BFD8FF">Aún no hay uploads guardados. (Se llenará cuando corra el step upload)</div>
        {% endif %}
      </div>
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
    # por seguridad, no expongas el token real si no quieres; si quieres ocultarlo, deja vacío.
    run_token = os.getenv("RUN_TOKEN", "").strip()
    return render_template_string(
        ADMIN_HTML,
        state=state,
        uploads=uploads[:20],
        log_tail=_tail_log(200),
        run_token=run_token,
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
    .pill{background:#0B2B57;border:1px solid #78B0FF;border-radius:999px;padding:6px 10px}
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
      <div class="muted">
        Esto edita <code>sources/enap_semana.json</code>. Puedes dejar vigencia vacío si no quieres fechas.
      </div>

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

        # Guardamos solo lo que tu fetcher ya usa
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