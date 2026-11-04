# Proton on a clean Ubuntu, for running Windows servers without Wine in the image. Torch and Magnetar Legacy build on this.
#
# Valve's own Proton needs a Steam login to download, so this uses GE-Proton. It runs through Proton's own launcher, not umu,
# whose nested sandbox is awkward inside Docker.
#
# Kept lean: anything a running server does not use is left out, and each removal was tested by loading a real server. The
# launcher needs libvulkan and its graphics DLLs even with nothing to draw, and fails without them, so those stay.
#
# The .NET Framework 4.8 installer exits with status 67 under Proton, so a throwaway stage takes the finished prefix from the
# docker/prefix image, which installs it with Wine. Only that prefix is copied here. Wine is not in this image.
FROM plugincheck-prefix:latest AS prefix

# The installer caches are not needed once .NET is in. The .NET compiler service would otherwise run at every start, rebuilding
# files that are lost with the container.
RUN cd "$WINEPREFIX/drive_c" \
    && rm -rf "ProgramData/Package Cache" windows/Installer/* users/*/AppData/Local/Temp/* windows/temp/* windows/logs/* \
       windows/Microsoft.NET/Framework64/v4.0.30319/SetupCache windows/Microsoft.NET/Framework/v4.0.30319/SetupCache \
    && for service in clr_optimization_v4.0.30319_32 clr_optimization_v4.0.30319_64; do \
         wine reg add "HKLM\\System\\CurrentControlSet\\Services\\$service" /v Start /t REG_DWORD /d 4 /f; \
       done \
    && wineserver -w

FROM ubuntu:24.04 AS fetch

ARG PROTON_VERSION=GE-Proton11-12

# Mono and Gecko replace .NET and a browser, neither used here. The voice data, speech model and Xalia are accessibility tools,
# and Windows fonts are already in the prefix.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && mkdir -p /opt/proton \
    && cd /tmp \
    && curl -fsSLO "https://github.com/GloriousEggroll/proton-ge-custom/releases/download/$PROTON_VERSION/$PROTON_VERSION-x86_64.tar.gz" \
    && curl -fsSLO "https://github.com/GloriousEggroll/proton-ge-custom/releases/download/$PROTON_VERSION/$PROTON_VERSION-x86_64.sha512sum" \
    && sha512sum --check --strict "$PROTON_VERSION-x86_64.sha512sum" \
    && tar -xzf "$PROTON_VERSION-x86_64.tar.gz" -C /opt/proton --strip-components=1 \
    && test -x /opt/proton/proton \
    && cd /opt/proton/files \
    && rm -rf share/wine/mono share/wine/gecko share/espeak-ng-data share/libtashkeel_model.ort share/xalia share/fonts/*

FROM ubuntu:24.04

ENV PROTON=/opt/proton \
    STEAM_COMPAT_DATA_PATH=/opt/compat \
    STEAM_COMPAT_CLIENT_INSTALL_PATH=/opt/steamclient \
    WINEPREFIX=/opt/compat/pfx \
    PATH=/opt/proton/files/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PROTON_USE_XALIA=0 \
    XDG_RUNTIME_DIR=/tmp/xdg \
    WINEDEBUG=-all \
    WINEDLLOVERRIDES="mscoree=n,b;mshtml=n,b" \
    HOME=/home/server

# The server runs as its own user, so nothing it does in the container runs as root. Docker gives a new named volume the owner of
# the folder it mounts over, which is why these folders are made here.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates python3 lib32gcc-s1 libvulkan1 libfreetype6 libgnutls30t64 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1001 --create-home server \
    && mkdir -p /opt/compat /opt/steamclient/linux64 /opt/steamclient/linux32 /ds /data /run-data /home/server/.steam/sdk64 /home/server/.steam/sdk32 \
    && ln -sf /opt/steamcmd/linux64/steamclient.so /home/server/.steam/sdk64/steamclient.so \
    && ln -sf /opt/steamcmd/linux32/steamclient.so /home/server/.steam/sdk32/steamclient.so \
    && ln -sf /opt/steamcmd/linux64/steamclient.so /opt/steamclient/linux64/steamclient.so \
    && ln -sf /opt/steamcmd/linux32/steamclient.so /opt/steamclient/linux32/steamclient.so \
    && chown -R server:server /opt/compat /opt/steamclient /ds /data /run-data /home/server \
    && find / -xdev -type f -perm /6000 -exec chmod a-s {} +

COPY --from=fetch /opt/proton /opt/proton
COPY --from=prefix --chown=server:server /opt/wine /opt/compat/pfx
# Proton's Steam bridge loads the Linux Steam library, which steamcmd includes. No Steam client or sign-in is needed.
COPY --from=prefix --chown=server:server /opt/steamcmd /opt/steamcmd

USER server
