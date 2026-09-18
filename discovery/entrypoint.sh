#!/bin/sh
# Replaces Explo's /start.sh. Same image, same binary, but instead of one cron
# line per playlist there is a single scheduled "tick" of discover.py, which
# decides what (if anything) needs doing. See discover.py for the logic.

set -u

PUID="${PUID:-0}"
PGID="${PGID:-0}"
SCHEDULE="${DISCOVERY_SCHEDULE:-15 0 * * *}"
ENV_FILE="${WEB_ENV_PATH:-/opt/explo/.env}"
ENV_DUMP=/run/ainulindale.env
SCRIPT=/ainulindale/discover.py

say() { echo "[setup] $*"; }

# yt-dlp ages quickly; refresh at start (best effort) and nightly, like upstream does
pip install --disable-pip-version-check --root-user-action=ignore --no-cache-dir --upgrade yt-dlp >/dev/null 2>&1 \
  && say "yt-dlp is up to date" || say "WARN: could not update yt-dlp (no network yet?)"

if [ "$PUID" != "0" ] || [ "$PGID" != "0" ]; then
  groupmod -o -g "$PGID" explo
  usermod -o -u "$PUID" explo
  RUNNER="su-exec explo"
  mkdir -p /opt/explo/config
  chown -R explo:explo /opt/explo/config
  chown explo:explo /opt/explo /data 2>/dev/null
else
  RUNNER=""
fi

# All settings arrive as container environment variables (from the stack's .env),
# so Explo only needs this file to exist.
[ -e "$ENV_FILE" ] || : > "$ENV_FILE"
[ -n "$RUNNER" ] && chown explo:explo "$ENV_FILE" 2>/dev/null

if [ -n "$RUNNER" ] && ! $RUNNER test -w /data; then
  say "WARN: /data is not writable for ${PUID}:${PGID}; downloads will fail until you chown the explo music folder"
fi

# cron jobs start with a bare environment; hand them ours
export -p > "$ENV_DUMP"
chmod 600 "$ENV_DUMP"

JOB=". $ENV_DUMP; cd /opt/explo && $RUNNER python3 -u $SCRIPT run >> /proc/1/fd/1 2>&1"
{
  echo "$SCHEDULE $JOB"
  echo "59 23 * * * pip install --disable-pip-version-check --root-user-action=ignore --no-cache-dir --upgrade yt-dlp >> /proc/1/fd/1 2>&1"
} > /etc/crontabs/root
chmod 600 /etc/crontabs/root
say "discovery tick scheduled: $SCHEDULE ($(date +%Z))"

if [ "${EXPLO_WEB_UI:-false}" = "true" ]; then
  say "starting Explo web UI on ${WEB_ADDR:-:7288} (viewing only; do not add schedules there)"
  WEB_UI=true $RUNNER /opt/explo/explo --config "$ENV_FILE" &
fi

if [ "${RUN_ON_START:-false}" = "true" ]; then
  say "RUN_ON_START=true, running one tick now (safe: already-imported weeks are skipped)"
  (cd /opt/explo && $RUNNER python3 -u "$SCRIPT" run) &
fi

exec crond -f -l 2
