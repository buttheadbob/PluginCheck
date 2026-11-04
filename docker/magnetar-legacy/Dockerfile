# MagnetarLegacy on Proton, for load testing the .NET Framework 4.8 build of a plugin. Interim runs natively in the official
# image, and Legacy has no Linux build. Built on docker/proton, so there is no Wine in this image. A failure here is a lead:
# confirm it on Windows before calling it a plugin defect.
FROM ubuntu:24.04 AS fetch

ARG MAGNETAR_VERSION=2.4.3.9
ARG MAGNETAR_SHA256

# The digest is the one GitHub publishes for this release.
RUN test -n "$MAGNETAR_SHA256" \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl 7zip \
    && curl -fsSL "https://github.com/CometWorks/magnetar/releases/download/v$MAGNETAR_VERSION/MagnetarForWindows-$MAGNETAR_VERSION.7z" -o /tmp/magnetar.7z \
    && echo "$MAGNETAR_SHA256  /tmp/magnetar.7z" | sha256sum --check --strict - \
    && mkdir -p /tmp/unpack /opt/magnetar \
    && 7z x -bso0 -o/tmp/unpack /tmp/magnetar.7z \
    && cp -r "$(dirname "$(find /tmp/unpack -name MagnetarLegacy.exe | head -1)")/." /opt/magnetar/ \
    && test -f /opt/magnetar/MagnetarLegacy.exe

FROM plugincheck-proton:latest

COPY --from=fetch --chown=server:server /opt/magnetar /opt/magnetar
COPY --chmod=755 entrypoint.sh /usr/local/bin/legacy-entrypoint
ENTRYPOINT ["legacy-entrypoint"]
