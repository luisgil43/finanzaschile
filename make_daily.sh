#!/usr/bin/env bash
set -euo pipefail

# ----- Entorno estable para cron/launchd -----
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export LANG=es_ES.UTF-8
export LC_ALL=es_ES.UTF-8
export TZ=America/Santiago
umask 022

cd "$(dirname "$0")"

# ----- Lock para evitar solapes -----
LOCK=".run.lock"
if [ -f "$LOCK" ]; then
  echo "Otro proceso en curso. Saliendo."
  exit 0
fi
trap 'rm -f "$LOCK"' EXIT
touch "$LOCK"

# ----- Logs (rotación simple: 7 días) -----
mkdir -p logs
LOG="logs/$(date +%Y-%m-%d).log"
find logs -name '*.log' -mtime +7 -delete || true

# ----- Venv / Python -----
VENV="/Users/luisenriquegilmoya/Desktop/APP/Finanzas_chile/.venv"
if [ -x "$VENV/bin/python" ]; then
  PY="$VENV/bin/python"
else
  PY="$(command -v python3)"
fi

# ----- Variables secretas y de comportamiento -----
set -a
[ -f .env ] && . ./.env
set +a
export YT_PRIVACY="${YT_PRIVACY:-public}"  # private | unlisted | public
# export YT_PLAYLIST_ID="PLxxxxxxxxxxxxx"  # si quieres playlist

# ----- Carpetas -----
mkdir -p data out

{
  echo "== $(date) ==================================================="
  echo "📡 1) Descargando datos..."
  "$PY" fetch_to_json.py

  echo "🖼️  2) Renderizando panel..."
  "$PY" render_panel.py

  echo "🔊  3) Generando locución..."
  "$PY" voice_from_json.py

  echo "🎬 4) Componiendo video..."
  /bin/bash ./make_video.sh

  echo "⏫ 5) Subiendo a YouTube..."
  # Retry suave (hasta 2 intentos)
  if ! "$PY" upload_to_youtube.py; then
    echo "Reintentando subida en 60s..."
    sleep 60
    "$PY" upload_to_youtube.py
  fi

  echo "✅ Todo OK: $(date)"
} >> "$LOG" 2>&1