# Contexto de construcción: la raíz del repositorio (ver compose.yaml).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DYNDNSARR_HUB_HOST=0.0.0.0 \
    DYNDNSARR_HUB_DATA_DIR=/data

COPY packages/shared /src/packages/shared
COPY apps/hub /src/apps/hub
RUN pip install /src/packages/shared /src/apps/hub && rm -rf /src

RUN useradd --system --uid 10001 --no-create-home dyndnsarr \
    && mkdir /data \
    && chown dyndnsarr:dyndnsarr /data
VOLUME /data
USER dyndnsarr

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]

CMD ["dyndnsarr-hub"]
