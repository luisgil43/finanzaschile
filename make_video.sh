#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C
export LANG=C

IMG="out/frame_1080.png"
VOZ="out/locucion.m4a"      # generado por voice_from_json.py
BGM="assets/bg_music.mp3"   # opcional

OUT_SHORT="out/finanzas_hoy_short.mp4"

FPS=30

if [ ! -f "$IMG" ]; then
  echo "❌ No existe $IMG"
  exit 1
fi

# duración basada en la voz, si existe; si no, 10s
if [ -f "$VOZ" ]; then
  DUR=$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$VOZ" | tr -d '\r\n')
  DUR=$(python3 - <<PY
d=float("$DUR")
print(f"{d:.3f}")
PY
)
else
  DUR=10.000
fi

# Shorts <= 60s. Cap a 59s.
SHORT_DUR=$(python3 - <<PY
d=float("$DUR")
print(f"{min(d, 59.0):.3f}")
PY
)

# total de frames para zoompan (usa 'on')
TOTAL_FRAMES_SHORT=$(python3 - <<PY
d=float("$SHORT_DUR"); fps=$FPS
print(int(round(d*fps)))
PY
)

FADEOUT_START_SHORT=$(python3 - <<PY
d=float("$SHORT_DUR")
print(max(0.0, d-0.4))
PY
)

# zoom suave (1.0 -> 1.01 aprox)
ZOOM_EXPR_SHORT="1.0+0.01*(on/${TOTAL_FRAMES_SHORT})"

echo "🎬 Generando SHORT: dur=$SHORT_DUR fps=$FPS frames=$TOTAL_FRAMES_SHORT"

# =========================
# VIDEO SHORT (vertical 9:16, <=59s) - SIN BLUR (ahorra RAM)
# - Panel al centro con pad (fondo negro)
# - 1 solo ffmpeg (menos procesos = menos picos)
# =========================

if [ -f "$BGM" ] && [ -f "$VOZ" ]; then
  ffmpeg -y \
    -loop 1 -framerate "$FPS" -i "$IMG" \
    -i "$VOZ" \
    -i "$BGM" \
    -t "$SHORT_DUR" \
    -filter_complex "\
[0:v]zoompan=z='${ZOOM_EXPR_SHORT}':d=1:s=1920x1080,\
fade=t=in:st=0:d=0.4,\
fade=t=out:st=${FADEOUT_START_SHORT}:d=0.4,\
scale=1080:-1,\
pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,\
format=yuv420p[vout]; \
[1:a]volume=1.0[a1]; \
[2:a]volume=0.16[a2]; \
[a1][a2]amix=inputs=2:normalize=0[aout]" \
    -map "[vout]" -map "[aout]" \
    -c:v libx264 -preset ultrafast -crf 30 -pix_fmt yuv420p -r "$FPS" \
    -c:a aac -shortest \
    -threads 1 \
    "$OUT_SHORT"

elif [ -f "$VOZ" ]; then
  ffmpeg -y \
    -loop 1 -framerate "$FPS" -i "$IMG" \
    -i "$VOZ" \
    -t "$SHORT_DUR" \
    -filter_complex "\
[0:v]zoompan=z='${ZOOM_EXPR_SHORT}':d=1:s=1920x1080,\
fade=t=in:st=0:d=0.4,\
fade=t=out:st=${FADEOUT_START_SHORT}:d=0.4,\
scale=1080:-1,\
pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,\
format=yuv420p[vout]" \
    -map "[vout]" -map 1:a \
    -c:v libx264 -preset ultrafast -crf 30 -pix_fmt yuv420p -r "$FPS" \
    -c:a aac -shortest \
    -threads 1 \
    "$OUT_SHORT"

else
  # sin voz: igual genera el short
  ffmpeg -y \
    -loop 1 -framerate "$FPS" -i "$IMG" \
    -t "$SHORT_DUR" \
    -vf "zoompan=z='${ZOOM_EXPR_SHORT}':d=1:s=1920x1080,fade=t=in:st=0:d=0.4,fade=t=out:st=${FADEOUT_START_SHORT}:d=0.4,scale=1080:-1,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,format=yuv420p" \
    -c:v libx264 -preset ultrafast -crf 30 -pix_fmt yuv420p -r "$FPS" \
    -an \
    -threads 1 \
    "$OUT_SHORT"
fi

echo "✅ Generado $OUT_SHORT"