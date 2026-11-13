"""Load test: starts Torch under Proton with the plugin zip in its Plugins folder, and reads what Torch logs.

usage: torch.py <target.json> <run dir> [results.json]

Set up as a real server is: the zip is copied into Torch's own Plugins folder, and run data stays on a Docker volume. Mounted
Windows folders only hand files in. Runs in its own container with its own dedicated server in the plugincheck-ds-torch volume. Torch logs a line for every
command it registers, so this lists exactly which commands exist. Proton is not Windows: a failure here is a lead, to
confirm on real Torch before reporting it as a plugin defect.

The zip comes from the plugin's own Windows build, since .NET Framework plugins cannot be built in a Linux container.
"""
import glob, json, os, re, shutil, subprocess, sys, time
from xml.sax.saxutils import escape
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # stages run with -I
import pin

IMAGE = os.environ.get('PLUGINCHECK_TORCH_IMAGE', 'plugincheck-torch-proton:latest')
TIMEOUT = 40 * 60
# Steam's library can hit internal asserts here and would send each crash dump to Valve.
NO_CRASH_REPORTS = 'crash.steampowered.com:127.0.0.1'
READY = 'Starting server watchdog'
FAILURE = re.compile(r'\[(ERROR|FATAL)\]', re.I)
# Torch logs a fatal error and then hangs instead of exiting, so waiting for the process would run to the timeout.
FATAL = re.compile(r'\[FATAL\]')

target = json.load(open(sys.argv[1], encoding='utf-8'))
run_dir = os.path.abspath(sys.argv[2])
result_path = sys.argv[3] if len(sys.argv) > 3 else None
conf = target['torch']
name = target['name']
zip_path = os.path.join(target['repo'], conf['zip'])
data = os.path.join(run_dir, 'torch-data')
plugins = os.path.join(run_dir, 'torch-plugins')
container = f'plugincheck-torch-{int(time.time())}'
volume = container + '-data'


def docker(*args):
    return subprocess.run(['docker', *args], capture_output=True, text=True, encoding='utf-8', errors='replace')


def finish(status, **fields):
    print(status, fields.get('reason', ''))
    if result_path:
        json.dump({'status': status, **fields}, open(result_path, 'w', encoding='utf-8'), indent=1)
    sys.exit(0 if status == 'pass' else 1)


if docker('image', 'inspect', IMAGE).returncode:
    finish('not run', reason=f'the {IMAGE} image is not built. See docker/torch-proton.')
if not os.path.exists(zip_path):
    finish('not run', reason=f'{conf["zip"]} does not exist. Build the plugin for Torch first.')

os.makedirs(os.path.join(data, 'Instance'))
os.makedirs(plugins)
shutil.copy(zip_path, plugins)
built = time.strftime('%Y-%m-%d %H:%M', time.localtime(os.path.getmtime(zip_path)))

open(os.path.join(data, 'Torch.cfg'), 'w', encoding='utf-8').write(f'''<?xml version="1.0" encoding="utf-8"?>
<TorchConfig xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <InstanceName>Instance</InstanceName>
  <InstancePath>Z:\\run-data\\Instance</InstancePath>
  <Autostart>true</Autostart>
  <NoGui>true</NoGui>
  <RestartOnCrash>false</RestartOnCrash>
  <RestartOnGameUpdate>false</RestartOnGameUpdate>
  <GetTorchUpdates>false</GetTorchUpdates>
  <GetPluginUpdates>false</GetPluginUpdates>
  <EnableAnalytics>false</EnableAnalytics>
  <LocalPlugins>true</LocalPlugins>
  <Plugins><guid>{escape(conf['guid'])}</guid></Plugins>
</TorchConfig>
''')
open(os.path.join(data, 'Instance', 'SpaceEngineers-Dedicated.cfg'), 'w', encoding='utf-8').write(f'''<?xml version="1.0" encoding="utf-8"?>
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

subprocess.run(['docker', 'run', *pin.args(), '-d', '--name', container, '--cpus', '6', '--memory', '12g', '--add-host', NO_CRASH_REPORTS,
                '-v', 'plugincheck-ds-torch:/ds', '-v', f'{volume}:/run-data',
                '-v', f'{data}:/seed/data:ro', '-v', f'{plugins}:/seed/plugins:ro', IMAGE],
               check=True, stdout=subprocess.DEVNULL)


def torch_log():
    return docker('exec', container, 'bash', '-c', 'cat /run-data/Logs/Torch-*.log 2>/dev/null').stdout


start, state = time.time(), 'timeout'
while time.time() - start < TIMEOUT:
    current = torch_log()
    if READY in current:
        time.sleep(15)
        state = 'loaded'
        break
    if FATAL.search(current):
        time.sleep(5)
        state = 'fatal'
        break
    if docker('inspect', '-f', '{{.State.Running}}', container).stdout.strip() != 'true':
        state = 'exited'
        break
    time.sleep(10)

output = docker('logs', container)
open(os.path.join(run_dir, 'torch-container.log'), 'w', encoding='utf-8').write(output.stdout + output.stderr)
text = torch_log()
docker('stop', '-t', '60', container)
docker('cp', f'{container}:/run-data/Logs', os.path.join(run_dir, 'torch-logs'))
for dump in glob.glob(os.path.join(run_dir, 'torch-logs', '*.dmp')):
    os.remove(dump)
docker('rm', '-f', '-v', container)
docker('volume', 'rm', volume)
loaded = re.search(r"Loading plugin '([^']+)' \(([^)]+)\)", text)
counted = re.search(r'Loaded (\d+) plugins', text)
commands = re.findall(r"Registering command '([^']+)'", text)
# Torch registers its own commands first, then the plugins' after "Loaded torch session".
marker = text.find('Loaded torch session')
plugin_commands = re.findall(r"Registering command '([^']+)'", text[marker:]) if marker >= 0 else []
problems = sorted({l.strip()[:300] for l in text.splitlines() if FAILURE.search(l)})
plugin_loaded = bool(loaded) and counted and int(counted.group(1)) > 0
ok = state == 'loaded' and plugin_loaded and plugin_commands and not problems
print(f'state={state} after {int(time.time() - start)}s, zip built {built}, plugin={loaded.groups() if loaded else None}, '
      f'commands={len(commands)} ({len(plugin_commands)} from plugins), problems={len(problems)}')
for p in problems[:25]:
    print(p)
finish('pass' if ok else 'fail', state=state, zipBuilt=built, plugin=loaded.groups() if loaded else None, commands=commands,
       pluginCommands=plugin_commands, problems=problems, note='Ran under Proton, not Windows')
