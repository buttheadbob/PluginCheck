"""Load test: Quasar starts a managed Magnetar server with the plugin and its companion deployed, as a real install does.

usage: quasar_managed.py <target.json> <run dir> [results.json]

Separate from the magnetar stage on purpose: a Quasar-managed server gets Quasar.Agent and companions, which can change
how Magnetar behaves, and not every server runs under Quasar. It shares nothing with the other stages: the runtime Quasar
downloads lives in plugincheck-ds-quasar, and all other state in a volume deleted after the run.

Steps, in the order a real install takes them:
  1. 1.4.1 writes the migration receipt. See quasar.py for why release.json is corrected for this step.
  2. Quasar installs its managed runtime. The companion compiles against those game files, so this comes first.
  3. The UI plugin and companion are built the way Quasar's installer does, into .quasar/companions/<id>.
  4. Quasar starts the server, which must load the plugin and the companion and reach a loaded world.

Servers, profiles and dev folders are seeded as the JSON files Quasar reads, since creating them is otherwise UI only.
The admin Steam ID is a placeholder that is never signed in with.
"""
import json, os, re, shutil, subprocess, sys, time
import xml.etree.ElementTree as ET

BRIDGE, LATEST = '1.4.1.1', 'latest'
TEST_ADMIN = '76561190000000000'
SERVER = 'plugincheck'
TOOLS = '/data/ManagedRuntime/Tools'
DS = f'{TOOLS}/SpaceEngineersDedicatedServer'
TIMEOUT = 30 * 60
HOLD = os.environ.get('PLUGINCHECK_HOLD') == '1'
UI_PORT = 18080
SKIP = shutil.ignore_patterns('bin', 'obj', '.git', '.vs', 'Build', 'GameBinaries', 'TorchBinaries', 'node_modules', 'Directory.Build.props')

target = json.load(open(sys.argv[1], encoding='utf-8'))
run_dir = os.path.abspath(sys.argv[2])
result_path = sys.argv[3] if len(sys.argv) > 3 else None
name = target['name']
conf = target['quasar']
repo = target['repo']
stamp = int(time.time())
volume = f'plugincheck-quasarrun-{stamp}'
work = os.path.join(run_dir, 'managed')
source = os.path.join(work, 'plugins', name)
seed = os.path.join(work, 'seed')
os.makedirs(os.path.join(work, 'plugins'))
shutil.copytree(repo, source, ignore=SKIP)
package = json.load(open(os.path.join(source, conf['packageManifest']), encoding='utf-8'))
companions = [c for c in package.get('companionPlugins', []) if isinstance(c, dict)]
manifest = target['magnetar']['manifest']
plugin_id = ET.parse(os.path.join(repo, manifest)).getroot().findtext('Id').strip()


def docker(*args):
    return subprocess.run(['docker', *args], capture_output=True, text=True, encoding='utf-8', errors='replace')


def finish(status, **fields):
    if not HOLD:
        docker('volume', 'rm', '-f', volume)
    print(status, fields.get('reason', ''))
    if result_path:
        json.dump({'status': status, **fields}, open(result_path, 'w', encoding='utf-8'), indent=1)
    sys.exit(0 if status == 'pass' else 1)


def write(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(value, f, indent=1)


def start(label, image, script, extra=()):
    container = f'plugincheck-quasarmanaged-{label}-{stamp}'
    subprocess.run(['docker', 'run', '-d', '--name', container, '--cpus', '6', '--memory', '12g', '--entrypoint', 'bash', *extra,
                    '-e', f'QUASAR_ADMIN_STEAM_ID={TEST_ADMIN}',
                    '-e', 'QUASAR_HOST_ID=plugincheck', '-e', 'QUASAR_LOG_MIN_LEVEL=Info',
                    '-v', f'{volume}:/data', '-v', f'plugincheck-ds-quasar:{DS}',
                    '-v', f'{os.path.join(work, "plugins")}:/plugins',
                    image, '-c', script], check=True, stdout=subprocess.DEVNULL)
    return container


def run(container, script):
    return docker('exec', container, 'bash', '-c', script)


def health(container):
    return run(container, "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/api/health").stdout.strip()


def alive(container):
    return docker('inspect', '-f', '{{.State.Running}}', container).stdout.strip() == 'true'


def stop(container, label):
    logs = docker('logs', container)
    open(os.path.join(run_dir, f'quasar-managed-{label}.log'), 'w', encoding='utf-8').write(logs.stdout + logs.stderr)
    docker('stop', '-t', '60', container)
    docker('rm', '-f', container)


def wait(container, ready, seconds=TIMEOUT):
    start_time = time.time()
    while time.time() - start_time < seconds:
        if not alive(container):
            return False
        if ready():
            return True
        time.sleep(10)
    return False


# Seed the files the Quasar UI would write.
write(os.path.join(seed, 'dev-folders.json'), [{
    'name': name, 'folderPath': f'/plugins/{name}', 'dataFile': manifest, 'pluginId': plugin_id, 'debugBuild': False}])
write(os.path.join(seed, 'ConfigProfiles', SERVER, 'profile.json'), {
    'configProfileId': SERVER, 'name': 'PluginCheck',
    'sessionSettings': {'onlineMode': 1},
    'plugins': [{'pluginId': plugin_id, 'displayName': name}], 'mods': [], 'offeredDefaultPlugins': []})
write(os.path.join(seed, 'Magnetars', SERVER, 'server.json'), {
    'uniqueName': SERVER, 'displayName': 'PluginCheck', 'inGameServerName': f'PluginCheck {name}', 'inGameWorldName': 'PluginCheck',
    'goalState': 1, 'autoStart': True, 'configProfileId': SERVER, 'worldSaveName': 'World', 'worldTemplateId': '', 'serverPort': 27016})
docker('volume', 'create', 'plugincheck-ds-quasar')
docker('volume', 'create', volume)
copied = docker('run', '--rm', '--entrypoint', 'bash', '-v', f'{volume}:/data', '-v', f'{seed}:/seed:ro',
                f'ghcr.io/cometworks/quasar:{LATEST}', '-c', 'cp -r /seed/. /data/')
if copied.returncode:
    finish('not run', reason='could not seed the data volume: ' + copied.stderr[:300])

# 1. The migration receipt.
image = f'ghcr.io/cometworks/quasar:{BRIDGE}'
docker('pull', image)
packaged = json.loads(docker('run', '--rm', '--entrypoint', 'cat', image, '/opt/quasar/release.json').stdout)
fixed = os.path.join(work, 'release.json')
json.dump({**packaged, 'version': BRIDGE}, open(fixed, 'w', encoding='utf-8'))
container = start('receipt', image, 'exec /opt/quasar/Quasar', ('-e', 'QUASAR_PRESERVE_SERVERS_ON_SHUTDOWN=false', '-v', f'{fixed}:/opt/quasar/release.json:ro'))
wrote = wait(container, lambda: run(container, 'test -f /data/Updates/migration-1.4.1.json').returncode == 0, 300)
stop(container, 'receipt')
if not wrote:
    finish('not run', reason='Quasar 1.4.1.1 did not write the migration receipt')

# 2. The managed runtime. Only the first run downloads it.
latest = f'ghcr.io/cometworks/quasar:{LATEST}'
exe = f'{DS}/DedicatedServer64/Sandbox.Game.dll'
if docker('run', '--rm', '--entrypoint', 'test', '-v', f'plugincheck-ds-quasar:{DS}', latest, '-f', exe).returncode:
    print('Installing the managed runtime, which only the first run does', flush=True)
    container = start('install', latest, 'exec /opt/quasar/Quasar')
    installed = wait(container, lambda: run(container, f'test -f {exe}').returncode == 0)
    time.sleep(30)
    stop(container, 'install')
    if not installed:
        finish('not run', reason='Quasar did not install the managed dedicated server')

# 3. Build the way Quasar's installer does, then 4. start the server.
world = f'/data/Magnetars/{SERVER}/DedicatedServer/Saves/World'
build = [f'set -e; mkdir -p "{world}"; cp -r "{DS}/Content/CustomWorlds/Star System/." "{world}/"; cd "/plugins/{name}"',
         f'dotnet build "{package["projectPath"]}" -c Release -v:minimal -p:QuasarPluginAbstractionsAssembly=/opt/quasar/Quasar.Plugin.Abstractions.dll -p:DeployToQuasar=false']
for c in companions:
    build.append(f'dotnet build "{c["projectPath"]}" -c Release -v:minimal -o ".quasar/companions/{c["id"]}" -p:Platform=x64 '
                 f'-p:CopyLocalLockFileAssemblies=true -p:MagnetarProtocolAssembly=/opt/quasar/Agent/Magnetar.Protocol.dll -p:DS64={DS}/DedicatedServer64')
build += ['echo PLUGINCHECK_BUILD_OK', 'cd /opt/quasar', 'exec /opt/quasar/Quasar']
# The UI is published to this machine only. A local request counts as trusted, so no Steam sign-in is needed.
ui = ('-p', f'127.0.0.1:{UI_PORT}:8080', '-e', f'QUASAR_PUBLIC_BASE_URL=http://127.0.0.1:{UI_PORT}', '-e', 'QUASAR_UI_PLUGIN_DIRS=/plugins')
container = start('server', latest, '; '.join(build), ui)


def logs_text():
    files = run(container, f'find /data/Magnetars/{SERVER} -name info.log -o -name "SpaceEngineersDedicated*.log"').stdout.split()
    return '\n'.join(run(container, f'cat "{f}"').stdout for f in files)


ready = wait(container, lambda: 'Game ready' in logs_text())
time.sleep(15 if ready else 0)
text = logs_text()
open(os.path.join(run_dir, 'quasar-managed-server.log'), 'w', encoding='utf-8').write(text)
quasar_built = 'PLUGINCHECK_BUILD_OK' in (docker('logs', container).stdout + docker('logs', container).stderr)
if HOLD:
    # Left running with no time limit, since whoever is using it may not be back soon.
    print(f'Left running at http://127.0.0.1:{UI_PORT}. To remove it: docker rm -f {container} && docker volume rm {volume}', flush=True)
    logs = docker('logs', container)
    open(os.path.join(run_dir, 'quasar-managed-quasar.log'), 'w', encoding='utf-8').write(logs.stdout + logs.stderr)
else:
    stop(container, 'quasar')

enabled = re.search(r'Enabled Plugins: (.*)', text)
registered = [(n, o) for n, o in re.findall(r'Registered (\d+) command module\(s\) from (.+)', text) if not o.startswith('Magnetar')]
companion_seen = {c['id']: os.path.splitext(c.get('entryAssembly', ''))[0] in text for c in companions}
problems = sorted({l.strip()[:300] for l in text.splitlines() if re.search(r'\[(ERROR|FATAL)\]|Failed to (load|register|compile)', l)})
ok = ready and quasar_built and enabled and name.lower()[:4] in enabled.group(1).lower() and registered and all(companion_seen.values()) and not problems
print(f'ready={ready} built={quasar_built} enabled={enabled.group(1) if enabled else None} registered={registered} '
      f'companions={companion_seen} problems={len(problems)}')
for p in problems[:20]:
    print(p)
finish('pass' if ok else 'fail', ready=ready, built=quasar_built, enabled=enabled.group(1) if enabled else None, registered=registered,
       companions=companion_seen, problems=problems)
