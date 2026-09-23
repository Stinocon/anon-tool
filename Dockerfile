# anon-tool — local, deterministic anonymization. One container, one command.
#
#   docker compose up -d          (recommended: publishes 127.0.0.1:1407 only)
#   docker build -t anon-tool .   &&   docker run --rm -p 127.0.0.1:1407:1407 \
#       -v "$HOME/.anon:/data" anon-tool
#
# The image ships the engine, its own converter and the front-end. The DATA (entities.txt, the
# maps) lives in the mounted volume, never in the image.
#
# The converter is installed from requirements-anydoc.txt with --require-hashes: every candidate
# artifact is pinned to its published sha256, so a swapped wheel cannot enter the image. Refresh
# the pins with `python3 scripts/pin-converter.py > requirements-anydoc.txt`.

FROM python:3.12-slim

# WITH_CONVERTER=0 builds a slimmer image that handles text formats only (md/txt/csv/json/yaml).
ARG WITH_CONVERTER=1

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ANON_HOME=/data \
    ANON_CONVERTER=/app/convert.py \
    PORT=1407

WORKDIR /app
COPY anon.py deanon.py suggest.py ./
COPY requirements-anydoc.txt ./
COPY catalogs ./catalogs
COPY web ./web
COPY docker-entrypoint.sh /usr/local/bin/entrypoint
# Staged OUTSIDE /app: a smarter image must not claim a converter it does not have (/api/state
# would answer `converter: true` and the UI would offer docx/pdf uploads that then fail). COPY
# cannot be conditional, so presence is decided by the same RUN that installs the dependency.
COPY convert.py /opt/convert.py

RUN chmod +x /usr/local/bin/entrypoint \
 && if [ "$WITH_CONVERTER" = "1" ]; then \
        pip install --no-cache-dir --require-hashes -r requirements-anydoc.txt \
        && mv /opt/convert.py /app/convert.py ; \
    fi \
 && rm -f /opt/convert.py \
 && useradd --create-home --uid 10001 anon \
 && mkdir -p /data/maps /data/catalogs \
 && chown -R anon:anon /data /app

# Non-root, and the only writable paths are the mounted volume and /tmp.
USER anon
EXPOSE 1407

# The healthcheck speaks HTTP to the UI it just started. It is a real end-to-end probe: the
# server only answers when the engine imported cleanly.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python3 -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/' % os.environ.get('PORT','1407'),timeout=3)" || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint"]
