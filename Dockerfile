FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    TZ=Europe/Paris

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

RUN useradd --system --uid 10001 --no-create-home --home-dir /app oubliez \
    && mkdir -p /data /backups && chown oubliez:oubliez /data /backups

USER oubliez
VOLUME ["/data"]
# /backups : dossier de sauvegarde, à monter depuis votre pool (voir docker-compose.yml)
EXPOSE 27480

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:27480/healthz', timeout=4).status == 200 else 1)"

# Un seul processus : le planificateur des envois tourne dans l'application.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "27480", "--proxy-headers", "--forwarded-allow-ips", "*"]
