#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C
export LANG=C

IMG="out/frame_1080.png"
VOZ="out/locucion.m4a"
BGM="assets/bg_music.mp3"

OUT="out/finanzas_hoy.mp4"
OUT_SHORT="out/finanzas_hoy_short.mp4"

FPS=30
FFMPEG_LOGLEVEL="${FFMPEG_LOGLEVEL:-error}"

GENERATE_FULL_VIDEO="${GENERATE_FULL_VIDEO:-1}"
GENERATE_SHORT_VIDEO="${GENERATE_SHORT_VIDEO:-1}"

if [ ! -f "$IMG" ]; then
  echo "❌ Falta $IMG"
  exit 1
fi

if [ ! -f "$VOZ" ]; then
  echo "❌ Falta $VOZ"
  exit 1
fi

DUR=$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$VOZ" | tr -d '\r\n')
DUR=$(python3 - <<PY
d=float("$DUR")
print(f"{d:.3f}")
PY
)

SHORT_DUR=$(python3 - <<PY
d=float("$DUR")
print(f"{min(d, 59.0):.3f}")
PY
)

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

ZOOM_EXPR="1.0+0.01*(on/${TOTAL_FRAMES})"
ZOOM_EXPR_SHORT="1.0+0.01*(on/${TOTAL_FRAMES_SHORT})"

mix_audio () {
  local INVIDEO="$1"
  local OUTVIDEO="$2"

  if [ -f "$BGM" ]; then
    ffmpeg -hide_banner -loglevel "$FFMPEG_LOGLEVEL" -y -i "$INVIDEO" -i "$VOZ" -i "$BGM" \
      -filter_complex "[1:a]volume=1.0[a1];[2:a]volume=0.16[a2];[a1][a2]amix=inputs=2[aout]" \
      -map 0:v -map "[aout]" -c:v copy -c:a aac -shortest "$OUTVIDEO"
  else
    ffmpeg -hide_banner -loglevel "$FFMPEG_LOGLEVEL" -y -i "$INVIDEO" -i "$VOZ" \
      -map 0:v -map 1:a -c:v copy -c:a aac -shortest "$OUTVIDEO"
  fi
}

if [ "$GENERATE_FULL_VIDEO" = "1" ]; then
  ffmpeg -hide_banner -loglevel "$FFMPEG_LOGLEVEL" -y -loop 1 -framerate $FPS -i "$IMG" \
    -t "$DUR" \
    -vf "zoompan=z='${ZOOM_EXPR}':d=1:s=1920x1080,fade=t=in:st=0:d=0.4,fade=t=out:st=${FADEOUT_START}:d=0.4" \
    -c:v libx264 -pix_fmt yuv420p -r $FPS out/video_sin_audio.mp4

  mix_audio "out/video_sin_audio.mp4" "$OUT"
  rm -f out/video_sin_audio.mp4
  echo "✅ Generado $OUT"
else
  echo "ℹ️ GENERATE_FULL_VIDEO=0 -> saltando video normal"
fi

if [ "$GENERATE_SHORT_VIDEO" = "1" ]; then
  ffmpeg -hide_banner -loglevel "$FFMPEG_LOGLEVEL" -y -loop 1 -framerate $FPS -i "$IMG" \
    -t "$SHORT_DUR" \
    -filter_complex "\
[0:v]zoompan=z='${ZOOM_EXPR_SHORT}':d=1:s=1920x1080,fade=t=in:st=0:d=0.4,fade=t=out:st=${FADEOUT_START_SHORT}:d=0.4,split=2[vmain][vbg]; \
[vbg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=18:1[vbg2]; \
[vmain]scale=1080:-1[vfg]; \
[vbg2][vfg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[vout]" \
    -map "[vout]" -c:v libx264 -pix_fmt yuv420p -r $FPS out/video_sin_audio_short.mp4

  mix_audio "out/video_sin_audio_short.mp4" "$OUT_SHORT"
  rm -f out/video_sin_audio_short.mp4
  echo "✅ Generado $OUT_SHORT"
else
  echo "ℹ️ GENERATE_SHORT_VIDEO=0 -> saltando short"
fi