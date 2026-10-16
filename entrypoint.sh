#!/bin/sh
# Run as PUID:PGID (default 1000:1000) so files in /data are owned by the host user.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p /data
    chown -R "$PUID:$PGID" /data
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups python -u web.py "$@"
fi

exec python -u web.py "$@"
