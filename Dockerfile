FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV TZ=America/Santiago

# Dependencias de sistema: ffmpeg + espeak-ng + fonts (Pillow) + bash
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    espeak-ng \
    bash \
    fonts-dejavu-core \
    ca-certificates \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

# Render provee PORT (necesitamos shell para expandir ${PORT})
CMD ["sh", "-c", "gunicorn -b 0.0.0.0:${PORT} server:app"]