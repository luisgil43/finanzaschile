FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV TZ=America/Santiago

# ----------------------------
# System deps
# ----------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    espeak-ng \
    bash \
    fonts-dejavu-core \
    ca-certificates \
    curl \
    tar \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ----------------------------
# Piper (neural TTS) + voice model
# ----------------------------
ARG PIPER_VERSION=1.2.0
ENV PIPER_HOME=/opt/piper
ENV PATH="/opt/piper:${PATH}"

# Usamos Piper por defecto en Render (puedes apagar con USE_PIPER=0)
ENV USE_PIPER=1

# Modelo/Config por defecto (voz más natural que espeak)
ENV PIPER_MODEL=/app/voices/es_AR-daniela-high.onnx
ENV PIPER_CONFIG=/app/voices/es_AR-daniela-high.onnx.json

RUN mkdir -p "${PIPER_HOME}" \
 && curl -L --fail "https://github.com/rhasspy/piper/releases/download/v${PIPER_VERSION}/piper_amd64.tar.gz" -o /tmp/piper.tar.gz \
 && tar -xzf /tmp/piper.tar.gz -C "${PIPER_HOME}" \
 && rm -f /tmp/piper.tar.gz \
 && chmod +x "${PIPER_HOME}/piper"

# Descarga del modelo (HuggingFace)
RUN mkdir -p /app/voices \
 && curl -L --fail "https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_AR/daniela/high/es_AR-daniela-high.onnx" -o "/app/voices/es_AR-daniela-high.onnx" \
 && curl -L --fail "https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_AR/daniela/high/es_AR-daniela-high.onnx.json" -o "/app/voices/es_AR-daniela-high.onnx.json"

# ----------------------------
# Python deps
# ----------------------------
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

# Render provee PORT
CMD ["sh", "-c", "gunicorn -b 0.0.0.0:${PORT} server:app"]