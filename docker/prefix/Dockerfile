# Installs .NET Framework 4.8 into a Wine prefix, for docker/proton to copy. That installer exits with status 67 under Proton.
# Nothing runs in this image, and none of Wine reaches the images that do. Wine and winetricks are pinned so an update cannot
# change results without warning. The dependency list and Wine overrides follow the Pterodactyl Torch egg.
FROM ubuntu:24.04

ARG WINE_VERSION=11.0.0.0~noble-1
ARG WINETRICKS_VERSION=20260125

ENV WINEPREFIX=/opt/wine WINEARCH=win64 WINEDEBUG=-all DISPLAY=:99 XDG_RUNTIME_DIR=/tmp/xdg

RUN dpkg --add-architecture i386 \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg unzip cabextract xvfb xauth lib32gcc-s1 \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://dl.winehq.org/wine-builds/winehq.key | gpg --dearmor -o /etc/apt/keyrings/winehq.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/winehq.gpg] https://dl.winehq.org/wine-builds/ubuntu/ noble main" > /etc/apt/sources.list.d/winehq.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       "winehq-stable=$WINE_VERSION" "wine-stable=$WINE_VERSION" "wine-stable-amd64=$WINE_VERSION" "wine-stable-i386=$WINE_VERSION" \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL "https://raw.githubusercontent.com/Winetricks/winetricks/$WINETRICKS_VERSION/src/winetricks" -o /usr/local/bin/winetricks \
    && chmod +x /usr/local/bin/winetricks

# Disabling mscoree and mshtml stops wineboot waiting on a dialog asking to install Mono and Gecko, which nobody can click
# here. Plugins use real .NET Framework. Each dependency is its own step, so a failure names it and a retry keeps the rest.
# /tmp is saved with each step, so its X lock must go first or Xvfb exits and the installers run with no display.
RUN mkdir -p -m 700 /tmp/xdg && rm -f /tmp/.X99-lock && (Xvfb :99 -screen 0 1024x768x24 & ) && sleep 2 \
    && WINEDLLOVERRIDES="mscoree,mshtml=" wineboot --init && wineserver -w && wine --version
RUN rm -f /tmp/.X99-lock && (Xvfb :99 -screen 0 1024x768x24 & ) && sleep 2 && winetricks -q corefonts && wineserver -w
RUN mkdir -p -m 700 /tmp/xdg && rm -f /tmp/.X99-lock && (Xvfb :99 -screen 0 1024x768x24 & ) && sleep 2 && winetricks -q vcrun2022 && wineserver -w
RUN mkdir -p -m 700 /tmp/xdg && rm -f /tmp/.X99-lock && (Xvfb :99 -screen 0 1024x768x24 & ) && sleep 2 && winetricks -q dotnet48 && wineserver -w
RUN rm -rf /root/.cache/winetricks /tmp/*

RUN mkdir /opt/steamcmd \
    && curl -fsSL https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz | tar -xz -C /opt/steamcmd \
    && (/opt/steamcmd/steamcmd.sh +quit || true)

ENV WINEDLLOVERRIDES=mscoree=n,b;mshtml=n,b
