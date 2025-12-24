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
    libstdc++6 \
    libgomp1 \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ----------------------------
# Piper (neural TTS) + voice model (España)
# ----------------------------
ARG PIPER_VERSION=1.2.0
ENV PIPER_HOME=/opt/piper
ENV PATH="/opt/piper:${PATH}"

# Usamos Piper por defecto
ENV USE_PIPER=1

# Voz España (puedes cambiar por otra si quieres)
ENV PIPER_MODEL=/app/voices/es_ES-sharvard-medium.onnx
ENV PIPER_CONFIG=/app/voices/es_ES-sharvard-medium.onnx.json

RUN set -eux; \
  mkdir -p "${PIPER_HOME}"; \
  ARCH="$(dpkg --print-architecture)"; \
  case "$ARCH" in \
    amd64)  PARCH="amd64" ;; \
    arm64)  PARCH="aarch64" ;; \
    *) echo "Unsupported arch: $ARCH" && exit 1 ;; \
  esac; \
  echo "Downloading Piper for arch=$ARCH (package=$PARCH)"; \
  curl -L --fail "https://github.com/rhasspy/piper/releases/download/v${PIPER_VERSION}/piper_${PARCH}.tar.gz" -o /tmp/piper.tar.gz; \
  tar -xzf /tmp/piper.tar.gz -C "${PIPER_HOME}"; \
  rm -f /tmp/piper.tar.gz; \
  chmod +x "${PIPER_HOME}/piper"

# Descarga del modelo (HuggingFace)
RUN set -eux; \
  mkdir -p /app/voices; \
  curl -L --fail "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/es/es_ES/sharvard/medium/es_ES-sharvard-medium.onnx?download=true" \
    -o "/app/voices/es_ES-sharvard-medium.onnx"; \
  curl -L --fail "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/es/es_ES/sharvard/medium/es_ES-sharvard-medium.onnx.json?download=true" \
    -o "/app/voices/es_ES-sharvard-medium.onnx.json"

# ----------------------------
# Python deps
# ----------------------------
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

# Render provee PORT
CMD ["sh", "-c", "gunicorn -b 0.0.0.0:${PORT} server:app"]