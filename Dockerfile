FROM debian:trixie-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pil calibre fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home kindle \
    && mkdir -p /app/data /kindle \
    && chown kindle:kindle /app/data

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp QT_QPA_PLATFORM=offscreen
COPY server.py library.py cover_tools.py moaan.py ./
COPY web ./web
USER 1000:1000
ARG VERSION=0.2.0
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="Kindle Desk" \
      org.opencontainers.image.version=$VERSION \
      org.opencontainers.image.revision=$VCS_REF
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/state', timeout=3).read()"
CMD ["python3", "server.py", "--host", "0.0.0.0", "--mount", "/kindle", "--data", "/app/data", "--no-eject"]
