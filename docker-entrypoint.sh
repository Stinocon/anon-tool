#!/bin/sh
# docker-entrypoint.sh — prepare the data volume, then start the UI.
set -eu

mkdir -p /data/maps /data/catalogs

# Seed the shipped catalogs on first run. A catalogue already present in the volume always wins:
# the volume is the operator's data, the image only provides defaults.
if [ -d /app/catalogs ] && [ -z "$(ls -A /data/catalogs 2>/dev/null || true)" ]; then
  cp -R /app/catalogs/. /data/catalogs/ 2>/dev/null || true
fi

# Inside the container the socket must listen on 0.0.0.0; the isolation comes from publishing the
# port on 127.0.0.1 only (docker-compose.yml). The server refuses a non-loopback bind without
# --allow-lan precisely so that this decision is explicit and visible in one place.
exec python3 /app/web/server.py --host 0.0.0.0 --port "${PORT:-1407}" --allow-lan
