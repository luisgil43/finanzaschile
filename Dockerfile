FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV TZ=America/Santiago

# Dependencias de sistema: ffmpeg + espeak-ng (fallback) + fonts (Pillow) + bash + wget
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    espeak-ng \
    bash \
    fonts-dejavu-core \
    ca-certificates \
    wget \
    tar \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

# =========================
# Piper TTS (neural, offline)
# =========================
# Instalamos el binario y una voz ES liviana (x_low) para Render free.
# Queda:
#   /app/piper
#   /app/voices/es_ES-carlfm-x_low.onnx
#   /app/voices/es_ES-carlfm-x_low.onnx.json
RUN mkdir -p /app/voices \
  && wget -qO /tmp/piper_amd64.tar.gz https://github.com/rhasspy/piper/releases/download/v1.2.0/piper_amd64.tar.gz \
  && tar xzf /tmp/piper_amd64.tar.gz -C /tmp \
  && mv /tmp/piper/piper /app/piper \
  && chmod +x /app/piper \
  && rm -rf /tmp/piper /tmp/piper_amd64.tar.gz \
  && wget -qO /app/voices/es_ES-carlfm-x_low.onnx https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/es/es_ES/carlfm/x_low/es_ES-carlfm-x_low.onnx \
  && wget -qO /app/voices/es_ES-carlfm-x_low.onnx.json https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/es/es_ES/carlfm/x_low/es_ES-carlfm-x_low.onnx.json

# Defaults para Piper (puedes sobreescribir en Render env vars si quieres)
ENV PIPER_BIN=/app/piper
ENV PIPER_VOICE_ONNX=/app/voices/es_ES-carlfm-x_low.onnx

# Render provee PORT (necesitamos shell para expandir ${PORT})
CMD ["sh", "-c", "gunicorn -b 0.0.0.0:${PORT} server:app"]