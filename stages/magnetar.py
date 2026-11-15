"""Load test: compiles a plugin from source in the official Magnetar image, the way the hub loader does, and loads a world.

usage: magnetar.py <target.json> <run dir> [results.json]

The plugin is registered as a dev folder set to a Release build. Only the manifest and its source directories are copied
in, so a file that the loader would not see cannot hide a failure. The dedicated server lives in the
plugincheck-ds-magnetar volume, which Torch and Legacy never touch, so an update or layout change here cannot affect them. Run data
stays on a Docker volume, as on a real server. Files are copied in before the server starts and logs copied out, so the server holds no
folder of this computer (containers.py), and every docker call has a time limit.
"""
import json, os, re, shutil, subprocess, sys, time
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import containers, githubtoken, repocopy, startlog
import pin
from containers import LOOK_SECONDS, STOP_SECONDS, copy, docker, docker_long, start_server

IMAGE = 'ghcr.io/cometworks/magnetar:latest'
TIMEOUT = 45 * 60
READY = 'Game ready'
FAILURES = [
    ('command registration', re.compile(r'Failed to register|Unexpected error registering')),
    ('compile', re.compile(r'Failed to compile|Compilation failed|error CS\d{4}')),
    ('plugin load', re.compile(r'Failed to (load|initiali[sz]e|instantiate)|threw an exception|Failed to (patch|apply)', re.I)),
]

target = json.load(open(sys.argv[1], encoding='utf-8'))
run_dir = os.path.abspath(sys.argv[2])
result_path = sys.argv[3] if len(sys.argv) > 3 else None
name = target['name']
repo = target['repo']
manifest = target['magnetar']['manifest']
source = os.path.join(run_dir, 'source', name)
data = os.path.join(run_dir, 'data')
config = os.path.join(data, 'Magnetar')
container = f'plugincheck-magnetar-{int(time.time())}'
volume = container + '-data'
log_path = os.path.join(run_dir, 'magnetar-container.log')
checks = []


def game_build():
    """A hash of the game's main assembly. Magnetar's core plugins are compiled against it, so a cache only holds for one build."""
    found = docker_long('run', *pin.args(), '--rm', '-v', 'plugincheck-ds-magnetar:/ds:ro', '--entrypoint', 'sha256sum', IMAGE, '/ds/DedicatedServer64/Sandbox.Game.dll').stdout.split()
    return found[0][:16] if found else None


build_id = game_build()
# GitHub's release downloads can take long enough that Magnetar gives up on them, so what it fetched once is kept between runs.
kept = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'cache', 'magnetar-github', build_id) if build_id else None


def complete(folder):
    return os.path.isdir(os.path.join(folder, 'Bin'))


def restore_downloads():
    if kept and os.path.isdir(kept):
        shutil.copytree(kept, os.path.join(config, 'GitHub'), dirs_exist_ok=True)


def keep_downloads():
    fetched = os.path.join(config, 'GitHub')
    if not kept or not os.path.isdir(fetched):
        return
    for name in os.listdir(fetched):
        if complete(os.path.join(fetched, name)) and not os.path.isdir(os.path.join(kept, name)):
            shutil.copytree(os.path.join(fetched, name), os.path.join(kept, name))


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)


def seed():
    os.makedirs(source)
    shutil.copy(os.path.join(repo, manifest), source)
    root = ET.parse(os.path.join(repo, manifest)).getroot()
    # Pulsar replaces a dev folder's id with the manifest's <Id>, so the profile must name that, not the folder.
    plugin_id = root.findtext('Id').strip()
    skip = repocopy.unlinked(shutil.ignore_patterns('bin', 'obj'))
    for d in root.findall('SourceDirectories/Directory'):
        rel = d.text.strip()
        shutil.copytree(os.path.join(repo, rel), os.path.join(source, rel), ignore=skip)

    write(os.path.join(config, 'Profiles', 'Current.xml'), f'''<?xml version="1.0"?>
<Profile xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <Name>Current</Name>
  <GitHub />
  <DevFolder>
    <LocalFolderConfig>
      <Id>{escape(plugin_id)}</Id>
      <DebugBuild>false</DebugBuild>
    </LocalFolderConfig>
  </DevFolder>
  <Local />
  <Mods />
</Profile>
''')
    write(os.path.join(config, 'Sources', 'sources.xml'), f'''<?xml version="1.0"?>
<SourcesConfig xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <ShowWarning>false</ShowWarning>
  <MaxSourceAge>2</MaxSourceAge>
  <RemoteHubSources>
    <RemoteHub>
      <Name>MagnetarHub</Name>
      <Repo>CometWorks/magnetar-hub</Repo>
      <Branch>main</Branch>
      <Enabled>true</Enabled>
      <Trusted>true</Trusted>
    </RemoteHub>
  </RemoteHubSources>
  <LocalPluginSources>
    <LocalPlugin>
      <Name>{escape(name)}</Name>
      <Folder>/plugin/{escape(name)}</Folder>
      <File>{escape(manifest)}</File>
      <Enabled>true</Enabled>
    </LocalPlugin>
  </LocalPluginSources>
</SourcesConfig>
''')


def seed_world():
    # Test servers stay private and send no statistics, so they are no noise for Steam's server list or the developers.
    write(os.path.join(data, 'Instance', 'SpaceEngineers-Dedicated.cfg'), f'''<?xml version="1.0" encoding="utf-8"?>
<MyConfigDedicated xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <SessionSettings>
    <GameMode>Survival</GameMode>
    <OnlineMode>PRIVATE</OnlineMode>
    <MaxPlayers>4</MaxPlayers>
  </SessionSettings>
  <IP>0.0.0.0</IP>
  <SteamPort>8766</SteamPort>
  <ServerPort>27016</ServerPort>
  <ServerName>PluginCheck {escape(name)}</ServerName>
  <WorldName>PluginCheck</WorldName>
  <PremadeCheckpointPath>/ds/Content/CustomWorlds/Star System</PremadeCheckpointPath>
  <RemoteApiEnabled>false</RemoteApiEnabled>
</MyConfigDedicated>
''')


def info_log():
    return docker('exec', container, 'bash', '-c', 'cat /data/Magnetar/info.log 2>/dev/null', timeout=LOOK_SECONDS).stdout


def running():
    r = docker('inspect', '-f', '{{.State.Running}}', container, timeout=LOOK_SECONDS)
    # A look that got no answer is not a server that ended.
    return r.returncode == containers.TIMED_OUT or r.stdout.strip() == 'true'


seed()
seed_world()
restore_downloads()
token_args, token_env = githubtoken.for_container()
problem = start_server(container, [*pin.args(), '--user', '0:0', '--cpus', '6', '--memory', '12g', '--entrypoint', 'bash', *token_args,
                                   '-e', 'DS_DIR=/ds', '-e', f'SERVER_NAME=PluginCheck {name}',
                                   '-v', 'plugincheck-ds-magnetar:/ds', '-v', f'{volume}:/data',
                                   IMAGE, '-c', 'cp -r /seed/. /data/; exec tini -- magnetar-entrypoint'],
                       run_dir, {data: '/seed', os.path.join(run_dir, 'source'): '/plugin'}, token_env)
if problem:
    docker('rm', '-f', '-v', container)
    docker('volume', 'rm', volume)
    reason = f'the server did not start: {problem}'
    print('not run', reason)
    if result_path:
        json.dump({'status': 'not run', 'reason': reason}, open(result_path, 'w', encoding='utf-8'), indent=1)
    sys.exit(0)
logs = open(log_path, 'wb')
# Ended at the stop below, and never waited on for longer than 30 seconds.
follower = subprocess.Popen(['docker', 'logs', '-f', container], stdout=logs, stderr=subprocess.STDOUT)

start, state = time.time(), 'timeout'
while time.time() - start < TIMEOUT:
    text = info_log()
    logs.flush()
    server = open(log_path, encoding='utf-8', errors='replace').read()
    if READY in server or READY in text:
        time.sleep(20)
        state = 'loaded'
        break
    if not running():
        state = 'exited'
        break
    time.sleep(10)

elapsed = int(time.time() - start)
docker('stop', '-t', '60', container, timeout=STOP_SECONDS)
try:
    follower.wait(timeout=30)
except subprocess.TimeoutExpired:
    follower.kill()
logs.close()
# The regcheck stage reads the plugin the loader built, and logcheck reads the server's logs.
for why in filter(None, (copy(f'{container}:/data/Magnetar/.', config), copy(f'{container}:/data/Instance/.', os.path.join(data, 'Instance')))):
    print(why)
docker('rm', '-f', '-v', container)
docker('volume', 'rm', volume)
keep_downloads()
# Read the copy: a container that has exited cannot be asked, and that is when the log matters most.
copied = os.path.join(config, 'info.log')
if os.path.exists(copied):
    text = open(copied, encoding='utf-8', errors='replace').read()
if text:
    open(os.path.join(run_dir, 'magnetar-info.log'), 'w', encoding='utf-8').write(text)
# Magnetar registers its own commands too, so only a registration from another owner counts.
reg = [(n, o) for n, o in re.findall(r'Registered (\d+) command module\(s\) from (.+)', text) if not o.startswith('Magnetar')]
stopped = startlog.loader_stopped(text)
if not stopped:
    for kind, rule in FAILURES:
        for line in text.splitlines():
            if rule.search(line):
                checks.append({'level': 'fail', 'kind': kind, 'text': line.strip()[:300]})
    problem = startlog.plugin_problem(text, reg, state)
    if problem:
        checks.append({'level': 'fail', 'kind': problem[0], 'text': problem[1]})
print(f'state={state} after {elapsed}s, registrations={reg}, problems={len(checks)}')
if stopped:
    print(stopped)
for c in checks[:30]:
    print(f"{c['kind']}: {c['text']}")
ok = state == 'loaded' and not checks and not stopped
status = 'unable to verify' if stopped else 'pass' if ok else 'fail'
outcome = {'status': status, 'state': state, 'seconds': elapsed, 'registered': reg, 'problems': checks}
if stopped:
    outcome['reason'] = stopped
if result_path:
    json.dump(outcome, open(result_path, 'w', encoding='utf-8'), indent=1)
sys.exit(1 if status == 'fail' else 0)
