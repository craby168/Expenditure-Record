FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    APP_DATA_DIR=/data \
    APP_HOST=0.0.0.0 \
    APP_PORT=8080

WORKDIR /app
RUN groupadd --gid 10001 expense && useradd --uid 10001 --gid expense --no-create-home expense \
    && mkdir /data && chown expense:expense /data
COPY backend/ /app/backend/
COPY index.html app.js styles.css store.js server-store.js cloud-store.js /app/
COPY assets/company-logo.jpg /app/assets/company-logo.jpg

USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).read()"
CMD ["python", "-m", "backend.app", "serve"]
