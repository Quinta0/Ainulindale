#!/bin/bash
# Ainulindale: one container, three jobs.
#
#   1. gluetun      VPN tunnel + kill switch (the base image)
#   2. slskd        Soulseek client, behind the tunnel, watched and restarted here
#   3. discovery    discover.py on a cron schedule, driving the Explo binary
#
# slskd and Explo are downloaded on first start into /config/bin, pinned by
# SLSKD_VERSION / EXPLO_VERSION. Change a version and restart to update.

set -u
say() { echo "[ainulindale] $*"; }

BIN=/config/bin
mkdir -p /config/gluetun /config/slskd /config/explo "$BIN"
export PATH="$BIN:$PATH"

# --- internal API keys ---------------------------------------------------------
# Only slskd, Explo and gluetun talk to each other, all over localhost, so the
# keys are generated once and kept. Set them in .env only to reach the APIs yourself.
if [ ! -s /config/.keys ]; then
  key() { od -An -tx1 -N24 /dev/urandom | tr -d ' \n'; }
  printf 'GEN_GLUETUN_KEY=%s\nGEN_SLSKD_KEY=%s\n' "$(key)" "$(key)" > /config/.keys
  chmod 600 /config/.keys
fi
. /config/.keys
export GLUETUN_API_KEY="${GLUETUN_API_KEY:-$GEN_GLUETUN_KEY}"
export SLSKD_API_KEY="${SLSKD_API_KEY:-$GEN_SLSKD_KEY}"
export SLSKD_VPN_GLUETUN_API_KEY="$GLUETUN_API_KEY"
export HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE="{\"auth\":\"apikey\",\"apikey\":\"$GLUETUN_API_KEY\"}"

# --- port forwarding: one switch, both spellings, both programs -------------------
# (VPN_PORT_FORWARDING is the pre-2.1 name; on/off are accepted too)
case "${PORT_FORWARDING:-${VPN_PORT_FORWARDING:-false}}" in
  true|on|yes|1) PF=true ;; *) PF=false ;;
esac
export VPN_PORT_FORWARDING=$([ $PF = true ] && echo on || echo off)
export PORT_FORWARD_ONLY=$VPN_PORT_FORWARDING
export SLSKD_VPN_PORT_FORWARDING=$PF
unset PORT_FORWARDING # gluetun reads it as a deprecated alias and warns

# --- let Explo reach Navidrome on the LAN through gluetun's firewall --------------
if [ -z "${FIREWALL_OUTBOUND_SUBNETS:-}" ]; then
  host="${SYSTEM_URL#*://}"; host="${host%%[:/]*}"
  if [[ "$host" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    export FIREWALL_OUTBOUND_SUBNETS="$host/32"
  elif [ -n "$host" ]; then
    say "WARN: NAVIDROME_URL uses a hostname ($host). Use its LAN IP, or set FIREWALL_OUTBOUND_SUBNETS, or the VPN firewall blocks it."
  fi
fi

# --- run slskd/Explo as a normal user (opt-in) -----------------------------------
PUID="${PUID:-0}" PGID="${PGID:-0}"
if [ "$PUID" != 0 ] || [ "$PGID" != 0 ]; then
  RUN="su-exec $PUID:$PGID"
  chown -R "$PUID:$PGID" /config/slskd /config/explo
  export HOME=/config/explo
else
  RUN=""
fi

# --- 1. VPN -----------------------------------------------------------------------
/gluetun-entrypoint &
GLUETUN=$!
say "waiting for the VPN tunnel"
until /gluetun-entrypoint healthcheck >/dev/null 2>&1; do
  kill -0 $GLUETUN 2>/dev/null || { say "gluetun exited"; exit 1; }
  sleep 3
done
say "VPN is up"

# --- downloads (through the tunnel) -----------------------------------------------
case "$(uname -m)" in
  x86_64)  SL_ARCH=x64   EX_ARCH=amd64 ;;
  aarch64) SL_ARCH=arm64 EX_ARCH=arm64 ;;
  *) say "unsupported architecture $(uname -m)"; exit 1 ;;
esac
SLSKD_HOME="$BIN/slskd-$SLSKD_VERSION"
EXPLO_HOME="$BIN/explo-$EXPLO_VERSION"

get() { # url dest: retries until it works, the VPN can be slow to settle
  until wget -q -T 60 -O "$2.part" "$1"; do say "download failed, retrying: $1"; sleep 30; done
  mv "$2.part" "$2"
}

if [ ! -x "$SLSKD_HOME/slskd" ]; then
  say "downloading slskd $SLSKD_VERSION"
  get "https://github.com/slskd/slskd/releases/download/$SLSKD_VERSION/slskd-$SLSKD_VERSION-linux-musl-$SL_ARCH.zip" "$BIN/slskd.zip"
  rm -rf "$SLSKD_HOME" && unzip -q "$BIN/slskd.zip" -d "$SLSKD_HOME" && rm "$BIN/slskd.zip"
  chmod +x "$SLSKD_HOME/slskd"
fi
if [ ! -x "$EXPLO_HOME/explo" ]; then
  say "downloading Explo $EXPLO_VERSION"
  mkdir -p "$EXPLO_HOME"
  get "https://github.com/LumePart/Explo/releases/download/$EXPLO_VERSION/explo-linux-$EX_ARCH" "$EXPLO_HOME/explo"
  get "https://raw.githubusercontent.com/LumePart/Explo/$EXPLO_VERSION/src/downloader/youtube_music/search_ytmusic.py" "$EXPLO_HOME/search_ytmusic.py"
  chmod +x "$EXPLO_HOME/explo"
fi
if [ ! -x "$BIN/yt-dlp" ]; then
  say "downloading yt-dlp"
  get https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp "$BIN/yt-dlp"
  chmod +x "$BIN/yt-dlp"
else
  yt-dlp -U >/dev/null 2>&1 && say "yt-dlp is up to date"
fi
# drop versions that are no longer pinned
find "$BIN" -mindepth 1 -maxdepth 1 -type d \( -name 'slskd-*' -o -name 'explo-*' \) \
  ! -name "slskd-$SLSKD_VERSION" ! -name "explo-$EXPLO_VERSION" -exec rm -rf {} +
export EXPLO_BIN="$EXPLO_HOME/explo"

# --- 2. slskd, with the watchdog that used to be the autoheal container -----------
[ -e /config/slskd/slskd.yml ] || cp /ainulindale/slskd.yml /config/slskd/slskd.yml
mkdir -p "$SLSKD_INCOMPLETE_DIR" "${DOWNLOAD_DIR%/}"
[ -n "$RUN" ] && chown "$PUID:$PGID" "$SLSKD_DOWNLOADS_DIR" "$SLSKD_INCOMPLETE_DIR" "${DOWNLOAD_DIR%/}"
if grep -qE '^(directories|soulseek|web):' /config/slskd/slskd.yml; then
  say "WARN: config/slskd/slskd.yml has directories:/soulseek:/web: blocks; they override .env (old file?)"
fi
(
  while :; do
    $RUN "$SLSKD_HOME/slskd" &
    pid=$!
    sleep 180 # start period: first login can take a while
    fails=0
    while kill -0 $pid 2>/dev/null; do
      if out="$(timeout 20 /ainulindale/healthcheck.sh 2>&1)"; then fails=0
      else fails=$((fails + 1)); say "slskd check failed ($fails/3): $out"; fi
      [ $fails -ge 3 ] && { say "restarting slskd"; kill $pid; break; }
      sleep 60
    done
    wait $pid
    sleep 10
  done
) &

# --- 3. discovery schedule -----------------------------------------------------------
ENV_FILE="${WEB_ENV_PATH:-/config/explo/.env}"
[ -e "$ENV_FILE" ] || : > "$ENV_FILE" # settings come from the environment, Explo just needs the file
export -p > /run/ainulindale.env && chmod 600 /run/ainulindale.env

# One runner for cron and the web UI: environment, Explo's folder, the right user.
cat > /run/ainulindale-py <<RUNNER
. /run/ainulindale.env
cd "$EXPLO_HOME" && exec $RUN python3 -u /ainulindale/discover.py "\$@"
RUNNER
# A tick's output goes to the container log and to a file the web UI shows.
mkdir -p /config/logs
LOG=/config/logs/discovery.log
[ "$(stat -c %s $LOG 2>/dev/null || echo 0)" -gt 5000000 ] && mv $LOG $LOG.1
echo "sh /run/ainulindale-py run 2>&1 | tee -a $LOG >> /proc/1/fd/1" > /run/ainulindale.job
touch /config/explo/ainulindale.lock
[ -n "$RUN" ] && chown "$PUID:$PGID" /config/explo/ainulindale.lock

{
  echo "${DISCOVERY_SCHEDULE:-15 0 * * *} sh /run/ainulindale.job"
  echo "59 23 * * * $BIN/yt-dlp -U >> /proc/1/fd/1 2>&1"
} > /etc/crontabs/root
chmod 600 /etc/crontabs/root
crond -l 2
say "discovery tick scheduled: ${DISCOVERY_SCHEDULE:-15 0 * * *} ($(date +%Z))"

# --- 4. web UI (status + Soulseek search), restarted if it ever dies ----------------
( while :; do python3 -u /ainulindale/ui.py; sleep 5; done ) &

if [ "${EXPLO_WEB_UI:-false}" = true ]; then
  say "Explo web UI on :7289 (viewing only, do not add schedules there)"
  (cd "$EXPLO_HOME" && WEB_UI=true WEB_ADDR=:7289 $RUN ./explo --config "$ENV_FILE") &
fi
if [ "${RUN_ON_START:-false}" = true ]; then
  say "RUN_ON_START=true, running one tick now (already-imported weeks are skipped)"
  sh /run/ainulindale.job & # discover.py itself waits for slskd to log in
fi

# The container lives as long as the VPN does; restart policy brings it all back.
wait $GLUETUN
say "gluetun exited, stopping"
exit 1
