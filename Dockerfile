FROM python:3.12-slim

# ffmpeg precisa pro moviepy gerar thumbnails
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instala deps primeiro (cache de layer)
# --use-deprecated=legacy-resolver: instagrapi 2.7 quer Pillow==12.2.0 (pin)
# e moviepy 2.x quer pillow<12 — o resolver novo recusa, mas o legado aceita
# (warnings, mas funciona na prática)
COPY requirements.txt .
RUN pip install --no-cache-dir --use-deprecated=legacy-resolver -r requirements.txt

# Copia o código
COPY . .

# Pasta de dados (montada como volume em prod)
ENV DATA_DIR=/data
RUN mkdir -p /data

EXPOSE 8000

# Roda diretamente uvicorn (run.py é só wrapper amigável pro dev)
CMD ["python", "-m", "uvicorn", "web.main:app", "--host", "0.0.0.0", "--port", "8000", "--forwarded-allow-ips=*", "--proxy-headers"]
