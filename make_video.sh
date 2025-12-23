set -euo pipefail

export LC_ALL=C
export LANG=C

IMG="out/frame_1080.png"
VOZ="out/locucion.m4a"      # generado por voice_from_json.py
BGM="assets/bg_music.mp3"   # opcional

OUT="out/finanzas_hoy.mp4"
OUT_SHORT="out/finanzas_hoy_short.mp4"

FPS=30

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

# Shorts deben ser <= 60s (seguro). Cap a 59s.
SHORT_DUR=$(python3 - <<PY
d=float("$DUR")
print(f"{min(d, 59.0):.3f}")
PY
)

# total de frames para zoompan (usa 'on', no 't')
TOTAL_FRAMES=$(python3 - <<PY
d=float("$DUR"); fps=$FPS
print(int(round(d*fps)))
PY
)

TOTAL_FRAMES_SHORT=$(python3 - <<PY
d=float("$SHORT_DUR"); fps=$FPS
print(int(round(d*fps)))
PY
)

# inicio del fade-out
FADEOUT_START=$(python3 - <<PY
d=float("$DUR")
print(max(0.0, d-0.4))
PY
)

FADEOUT_START_SHORT=$(python3 - <<PY
d=float("$SHORT_DUR")
print(max(0.0, d-0.4))
PY
)

# Expresión de zoom usando 'on' (número de fotograma)
# 1.0 -> 1.04 a lo largo de toda la duración
ZOOM_EXPR="1.0+0.01*(on/${TOTAL_FRAMES})"
ZOOM_EXPR_SHORT="1.0+0.01*(on/${TOTAL_FRAMES_SHORT})"

# -------------------------
# 1) VIDEO NORMAL (igual)
# -------------------------
ffmpeg -y -loop 1 -framerate $FPS -i "$IMG" \
  -t "$DUR" \
  -vf "zoompan=z='${ZOOM_EXPR}':d=1:s=1920x1080,fade=t=in:st=0:d=0.4,fade=t=out:st=${FADEOUT_START}:d=0.4" \
  -c:v libx264 -pix_fmt yuv420p -r $FPS out/video_sin_audio.mp4

# Mezcla de audio: voz + (opcional) música
if [ -f "$BGM" ]; then
  ffmpeg -y -i out/video_sin_audio.mp4 -i "$VOZ" -i "$BGM" \
    -filter_complex "[1:a]volume=1.0[a1];[2:a]volume=0.16[a2];[a1][a2]amix=inputs=2[aout]" \
    -map 0:v -map "[aout]" -c:v copy -c:a aac -shortest "$OUT"
else
  ffmpeg -y -i out/video_sin_audio.mp4 -i "$VOZ" \
    -map 0:v -map 1:a -c:v copy -c:a aac -shortest "$OUT"
fi

echo "✅ Generado $OUT"

# -------------------------
# 2) VIDEO SHORT (vertical 9:16, <=59s)
# - Fondo 9:16 con blur
# - Panel intacto al centro (sin recortar el contenido)
# -------------------------
ffmpeg -y -loop 1 -framerate $FPS -i "$IMG" \
  -t "$SHORT_DUR" \
  -filter_complex "\
[0:v]zoompan=z='${ZOOM_EXPR_SHORT}':d=1:s=1920x1080,fade=t=in:st=0:d=0.4,fade=t=out:st=${FADEOUT_START_SHORT}:d=0.4,split=2[vmain][vbg]; \
[vbg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=18:1[vbg2]; \
[vmain]scale=1080:-1[vfg]; \
[vbg2][vfg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[vout]" \
  -map "[vout]" -c:v libx264 -pix_fmt yuv420p -r $FPS out/video_sin_audio_short.mp4

# Audio para short (mismo audio; se corta por duración del video)
if [ -f "$BGM" ]; then
  ffmpeg -y -i out/video_sin_audio_short.mp4 -i "$VOZ" -i "$BGM" \
    -filter_complex "[1:a]volume=1.0[a1];[2:a]volume=0.16[a2];[a1][a2]amix=inputs=2[aout]" \
    -map 0:v -map "[aout]" -c:v copy -c:a aac -shortest "$OUT_SHORT"
else
  ffmpeg -y -i out/video_sin_audio_short.mp4 -i "$VOZ" \
    -map 0:v -map 1:a -c:v copy -c:a aac -shortest "$OUT_SHORT"
fi

echo "✅ Generado $OUT_SHORT"