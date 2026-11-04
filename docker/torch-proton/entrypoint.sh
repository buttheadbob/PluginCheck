#!/bin/bash
# Installs or updates this image's own dedicated server in /ds, then starts Torch through Proton, laid out as a real server is.
set -euo pipefail

DS_UPDATE="${DS_UPDATE:-1}"
ds_exe=/ds/DedicatedServer64/SpaceEngineersDedicated.exe

if [ "$DS_UPDATE" = 1 ] || ! [ -f "$ds_exe" ]; then
  # steamcmd exit codes are unreliable, so the executable is the success marker.
  for attempt in 1 2 3; do
    /opt/steamcmd/steamcmd.sh \
      +@sSteamCmdForcePlatformType windows \
      +force_install_dir /ds \
      +login anonymous \
      +app_update 298740 \
      +quit && break
    echo "steamcmd attempt $attempt failed"
    sleep 5
  done
fi
test -f "$ds_exe" || { echo "the dedicated server is not installed in /ds" >&2; exit 2; }

ln -sfn /ds/DedicatedServer64 /opt/torch/DedicatedServer64
ln -sfn /ds/Content /opt/torch/Content
# On a real install these sit next to Torch.Server.exe. Steam will not start without them there.
for dll in steam_appid.txt steamclient.dll steamclient64.dll steamwebrtc.dll steamwebrtc64.dll \
           tier0_s.dll tier0_s64.dll vstdlib_s.dll vstdlib_s64.dll; do
  if [ -e "/ds/DedicatedServer64/$dll" ]; then ln -sfn "/ds/DedicatedServer64/$dll" "/opt/torch/$dll"; fi
done

# The plugin goes in Torch's own Plugins folder, and run data stays on the /run-data volume. /seed only hands files in.
mkdir -p /opt/torch/Plugins /run-data/Instance /run-data/Logs
cp /seed/plugins/* /opt/torch/Plugins/
cp -r /seed/data/. /run-data/
ln -sfn /run-data/Logs /opt/torch/Logs
cp /run-data/Torch.cfg /opt/torch/Torch.cfg

mkdir -p -m 700 "$XDG_RUNTIME_DIR"
rm -f /tmp/.X99-lock
# Torch's WPF needs a screen to open. Xvfb warns about every newer keyboard symbol it does not know, so its output is shown only
# if it fails to start.
Xvfb :99 -screen 0 1024x768x24 -extension GLX >/tmp/xvfb.log 2>&1 &
sleep 2
kill -0 $! 2>/dev/null || { cat /tmp/xvfb.log >&2; exit 3; }

# Torch writes to a log file, so it is also shown on the container's console.
(until compgen -G '/run-data/Logs/Torch-*.log' >/dev/null; do sleep 1; done; exec tail -n +1 -F /run-data/Logs/Torch-*.log) &

cd /opt/torch
exec /opt/proton/proton run Torch.Server.exe -noupdate -nogui -console -autostart
