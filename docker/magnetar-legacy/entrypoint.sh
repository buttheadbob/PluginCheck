#!/bin/bash
# Installs or updates this image's own dedicated server in /ds, then runs MagnetarLegacy with all state under /data.
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

# The run's settings. .NET under Wine, which Proton is built on, cannot load larger DLLs from a mounted Windows folder, so /data must stay a Docker volume.
cp -r /seed/. /data/

# Steam looks for these next to the program that started, as it does for Torch.
for dll in steam_appid.txt steamclient.dll steamclient64.dll steamwebrtc.dll steamwebrtc64.dll \
           tier0_s.dll tier0_s64.dll vstdlib_s.dll vstdlib_s64.dll; do
  if [ -e "/ds/DedicatedServer64/$dll" ]; then ln -sfn "/ds/DedicatedServer64/$dll" "/opt/magnetar/$dll"; fi
done

mkdir -p -m 700 "$XDG_RUNTIME_DIR"

# Magnetar and the game write to log files, so they are also shown on the container's console.
tail -q -n +1 -F /data/Magnetar/info.log 2>/dev/null &
(until compgen -G '/data/Instance/*.log' >/dev/null; do sleep 1; done; exec tail -q -n +1 -F /data/Instance/*.log) &

cd /opt/magnetar
exec /opt/proton/proton run MagnetarLegacy.exe -consent deny -config 'Z:\data\Magnetar' -ds64 'Z:\ds\DedicatedServer64' -path 'Z:\data\Instance' "$@"
