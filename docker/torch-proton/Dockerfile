# Torch on Proton. Built on docker/proton, so there is no Wine in this image.
#
# Torch is a WPF program, and hangs at startup with no display even with -nogui, so it needs Xvfb. Legacy does not. Xvfb only
# gives Torch a screen to open, so GLX is off, and the Mesa and LLVM graphics drivers it pulls in are removed.
FROM ubuntu:24.04 AS fetch

ARG TORCH_URL=https://build.torchapi.com/job/Torch/job/master/lastSuccessfulBuild/artifact/bin/torch-server.zip

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && mkdir -p /opt/torch \
    && curl -fsSL "$TORCH_URL" -o /tmp/torch.zip \
    && unzip -q /tmp/torch.zip -d /opt/torch \
    && test -f /opt/torch/Torch.Server.exe

FROM plugincheck-proton:latest

ENV DISPLAY=:99

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb libx11-6 libxext6 \
    && rm -rf /var/lib/apt/lists/* \
    && dpkg --purge --force-depends mesa-libgallium libllvm20 libxml2 libicu74 \
    && ! ldd /usr/bin/Xvfb | grep 'not found' \
    && mkdir -p -m 1777 /tmp/.X11-unix \
    && find / -xdev -type f -perm /6000 -exec chmod a-s {} +

COPY --from=fetch --chown=server:server /opt/torch /opt/torch
COPY --chmod=755 entrypoint.sh /usr/local/bin/torch-entrypoint

USER server
ENTRYPOINT ["torch-entrypoint"]
