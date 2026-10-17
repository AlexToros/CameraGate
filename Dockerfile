FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LISTEN_HOST=0.0.0.0 DATA_DIR=/data
WORKDIR /app
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libstdc++6 \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && useradd --uid 10001 --create-home viewer \
 && mkdir /data && chown viewer:viewer /data
COPY server.py store.py schema.sql streams.py gateway_client.py vesta.py ./
COPY index.html app.js style.css sw.js manifest.webmanifest *icon*.png ./
USER viewer
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"
CMD ["python", "server.py"]
