# Ainulindale: gluetun + slskd + Explo discovery in one container.
# slskd and Explo are fetched at runtime (see start.sh); bump these to update.
FROM qmcgaw/gluetun:v3.41.3

ENV SLSKD_VERSION=0.26.2 \
    EXPLO_VERSION=v1.2.0 \
    SLSKD_APP_DIR=/config/slskd \
    SLSKD_DOWNLOADS_DIR=/music/slskd \
    SLSKD_INCOMPLETE_DIR=/music/slskd/incomplete \
    SLSKD_REMOTE_CONFIGURATION=true \
    SLSKD_VPN=true \
    SLSKD_VPN_GLUETUN_URL=http://localhost:8000 \
    SLSKD_URL=http://localhost:5030 \
    SLSKD_DIR=/music/slskd/ \
    DOWNLOAD_DIR=/music/explo/ \
    WEB_DATA_PATH=/config/explo \
    WEB_ENV_PATH=/config/explo/.env \
    DOTNET_BUNDLE_EXTRACT_BASE_DIR=/tmp/dotnet

# runtime for slskd (.NET, musl build), Explo (glibc Go binary + ffmpeg + ytmusicapi) and the scripts
RUN apk add --no-cache bash jq unzip tini su-exec tzdata python3 py3-pip ffmpeg gcompat icu-libs libstdc++ libgcc \
 && pip install --no-cache-dir --break-system-packages ytmusicapi \
 && rm -rf /gluetun && ln -s /config/gluetun /gluetun

COPY start.sh discovery/discover.py slskd/healthcheck.sh slskd/slskd.yml /ainulindale/
RUN chmod +x /ainulindale/*.sh

VOLUME /config
EXPOSE 5030 7288
HEALTHCHECK --interval=60s --timeout=20s --start-period=5m --retries=3 \
  CMD /gluetun-entrypoint healthcheck && wget -q -T 5 -O /dev/null http://localhost:5030/health
ENTRYPOINT ["/sbin/tini", "-g", "--", "/ainulindale/start.sh"]
