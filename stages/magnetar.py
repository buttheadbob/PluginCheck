"""Load test: compiles a plugin from source in the official Magnetar image, the way the hub loader does, and loads a world.

usage: magnetar.py <target.json> <run dir> [results.json]

The plugin is registered as a dev folder set to a Release build. Only the manifest and its source directories are copied
in, so a file that the loader would not see cannot hide a failure. The dedicated server lives in the
plugincheck-ds-magnetar volume, which only this stage uses, so an update or layout change here cannot affect Torch. Run data
stays on a Docker volume, as on a real server. Mounted Windows folders only hand files in, and logs are copied out.
"""
import json, os, re, shutil, subprocess, sys, time
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

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
    skip = shutil.ignore_patterns('bin', 'obj')
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
    return subprocess.run(['docker', 'exec', container, 'bash', '-c', 'cat /data/Magnetar/info.log 2>/dev/null'],
                          capture_output=True, text=True, encoding='utf-8', errors='replace').stdout


def running():
    r = subprocess.run(['docker', 'inspect', '-f', '{{.State.Running}}', container], capture_output=True, text=True)
    return r.stdout.strip() == 'true'


seed()
seed_world()
subprocess.run(['docker', 'run', '-d', '--name', container, '--user', '0:0', '--cpus', '6', '--memory', '12g', '--entrypoint', 'bash',
                '-e', 'DS_DIR=/ds', '-e', f'SERVER_NAME=PluginCheck {name}',
                '-v', 'plugincheck-ds-magnetar:/ds',
                '-v', f'{volume}:/data', '-v', f'{data}:/seed:ro',
                '-v', f'{os.path.join(run_dir, "source")}:/plugin:ro',
                IMAGE, '-c', 'cp -r /seed/. /data/; exec tini -- magnetar-entrypoint -consent deny'], check=True, stdout=subprocess.DEVNULL)
logs = open(log_path, 'wb')
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
text = info_log()
subprocess.run(['docker', 'stop', '-t', '60', container], capture_output=True)
follower.wait(timeout=30)
logs.close()
# The regcheck stage reads the plugin the loader built, and logcheck reads the server's logs.
subprocess.run(['docker', 'cp', f'{container}:/data/Magnetar/.', config], capture_output=True)
subprocess.run(['docker', 'cp', f'{container}:/data/Instance/.', os.path.join(data, 'Instance')], capture_output=True)
subprocess.run(['docker', 'rm', '-v', container], capture_output=True)
subprocess.run(['docker', 'volume', 'rm', volume], capture_output=True)
if text:
    open(os.path.join(run_dir, 'magnetar-info.log'), 'w', encoding='utf-8').write(text)
for kind, rule in FAILURES:
    for line in text.splitlines():
        if rule.search(line):
            checks.append({'level': 'fail', 'kind': kind, 'text': line.strip()[:300]})
# Magnetar registers its own commands too, so only a registration from another owner counts.
reg = [(n, o) for n, o in re.findall(r'Registered (\d+) command module\(s\) from (.+)', text) if not o.startswith('Magnetar')]
enabled = re.search(r'Enabled Plugins: (.*)', text)
if not enabled or enabled.group(1).strip() == 'None':
    checks.append({'level': 'fail', 'kind': 'plugin load', 'text': 'the plugin was not enabled, so nothing about it was tested'})
elif not reg:
    checks.append({'level': 'fail', 'kind': 'command registration', 'text': 'no command modules were registered by the plugin'})
print(f'state={state} after {elapsed}s, registrations={reg}, problems={len(checks)}')
for c in checks[:30]:
    print(f"{c['kind']}: {c['text']}")
ok = state == 'loaded' and not checks
if result_path:
    json.dump({'status': 'pass' if ok else 'fail', 'state': state, 'seconds': elapsed, 'registered': reg, 'problems': checks},
              open(result_path, 'w', encoding='utf-8'), indent=1)
sys.exit(0 if ok else 1)
