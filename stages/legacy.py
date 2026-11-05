"""Load test: MagnetarLegacy, the .NET Framework 4.8 launcher, compiles a plugin from source and loads a world, under Proton.

usage: legacy.py <target.json> <run dir> [results.json]

The magnetar stage covers Interim on .NET 10. A plugin compiles and runs differently on 4.8, so both matter. Like the magnetar
stage, it registers the plugin as a dev folder with a Release build, and copies in only the manifest and its source directories.
It has its own dedicated server in the plugincheck-ds-legacy volume. Proton is not Windows: a failure here is a lead, to confirm
on real Windows before calling it a plugin defect.

Run data must live on a Docker volume. .NET under Wine, which Proton is built on, fails with BadImageFormatException loading a larger DLL from a Windows
folder mounted through Docker Desktop, while the same file loads from a volume. So settings are seeded in, and logs copied out.
"""
import json, os, re, shutil, subprocess, sys, time
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

IMAGE = os.environ.get('PLUGINCHECK_LEGACY_IMAGE', 'plugincheck-magnetar-legacy:latest')
TIMEOUT = 45 * 60
# Steam's library hits internal asserts here and would send each crash dump to Valve.
NO_CRASH_REPORTS = 'crash.steampowered.com:127.0.0.1'
READY = 'Game ready'
FAILURES = [
    ('command registration', re.compile(r'Failed to register|Unexpected error registering')),
    ('compile', re.compile(r'Failed to compile|Compilation failed|error CS\d{4}')),
    ('plugin load', re.compile(r'Failed to (load|initiali[sz]e|instantiate)|threw an exception', re.I)),
]

target = json.load(open(sys.argv[1], encoding='utf-8'))
run_dir = os.path.abspath(sys.argv[2])
result_path = sys.argv[3] if len(sys.argv) > 3 else None
name = target['name']
repo = target['repo']
manifest = target['magnetar']['manifest']
base = os.path.join(run_dir, 'legacy')
source = os.path.join(base, 'source', name)
data = os.path.join(base, 'data')
config = os.path.join(data, 'Magnetar')
container = f'plugincheck-legacy-{int(time.time())}'
volume = container + '-data'
log_path = os.path.join(run_dir, 'legacy-container.log')
checks = []


def docker(*args):
    return subprocess.run(['docker', *args], capture_output=True, text=True, encoding='utf-8', errors='replace')


def finish(status, **fields):
    print(status, fields.get('reason', ''))
    if result_path:
        json.dump({'status': status, **fields}, open(result_path, 'w', encoding='utf-8'), indent=1)
    sys.exit(1 if status == 'fail' else 0)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)


if docker('image', 'inspect', IMAGE).returncode:
    finish('not run', reason=f'the {IMAGE} image is not built. See docker/magnetar-legacy.')

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
      <Folder>Z:\\plugin\\{escape(name)}</Folder>
      <File>{escape(manifest)}</File>
      <Enabled>true</Enabled>
    </LocalPlugin>
  </LocalPluginSources>
</SourcesConfig>
''')
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
  <PremadeCheckpointPath>Z:\\ds\\Content\\CustomWorlds\\Star System</PremadeCheckpointPath>
  <RemoteApiEnabled>false</RemoteApiEnabled>
</MyConfigDedicated>
''')

subprocess.run(['docker', 'run', '-d', '--name', container, '--cpus', '6', '--memory', '12g', '--add-host', NO_CRASH_REPORTS,
                '-v', 'plugincheck-ds-legacy:/ds', '-v', f'{volume}:/data', '-v', f'{data}:/seed:ro',
                '-v', f'{os.path.join(base, "source")}:/plugin:ro', IMAGE],
               check=True, stdout=subprocess.DEVNULL)


def read(path):
    if not os.path.exists(path):
        return ''
    with open(path, encoding='utf-8', errors='replace') as f:
        return f.read()


def game_ready():
    return docker('exec', container, 'bash', '-c', f'grep -lq "{READY}" /data/Instance/*.log').returncode == 0


def running():
    return docker('inspect', '-f', '{{.State.Running}}', container).stdout.strip() == 'true'


start, state = time.time(), 'timeout'
while time.time() - start < TIMEOUT:
    if game_ready():
        time.sleep(20)
        state = 'loaded'
        break
    if not running():
        state = 'exited'
        break
    time.sleep(10)

elapsed = int(time.time() - start)
output = docker('logs', container)
open(log_path, 'w', encoding='utf-8').write(output.stdout + output.stderr)
docker('stop', '-t', '60', container)
docker('cp', f'{container}:/data/Magnetar/info.log', os.path.join(run_dir, 'legacy-info.log'))
docker('rm', '-f', '-v', container)
docker('volume', 'rm', volume)

text = read(os.path.join(run_dir, 'legacy-info.log'))
for kind, rule in FAILURES:
    for line in text.splitlines():
        if rule.search(line):
            checks.append({'level': 'fail', 'kind': kind, 'text': line.strip()[:300]})
registered = [(n, o) for n, o in re.findall(r'Registered (\d+) command module\(s\) from (.+)', text) if not o.startswith('Magnetar')]
enabled = re.search(r'Enabled Plugins: (.*)', text)
if not enabled or enabled.group(1).strip() == 'None':
    checks.append({'level': 'fail', 'kind': 'plugin load', 'text': 'the plugin was not enabled, so nothing about it was tested'})
elif not registered:
    checks.append({'level': 'fail', 'kind': 'command registration', 'text': 'no command modules were registered by the plugin'})

print(f'state={state} after {elapsed}s, registrations={registered}, problems={len(checks)}')
for c in checks[:30]:
    print(f"{c['kind']}: {c['text']}")
ok = state == 'loaded' and not checks
finish('pass' if ok else 'fail', state=state, seconds=elapsed, registered=registered, problems=checks, note='Ran under Proton, not Windows')
