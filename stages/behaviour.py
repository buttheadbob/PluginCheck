"""Runs a plugin on real servers in a generated world, and compares what it did with its published build.

usage: behaviour.py <target.json> <run dir> [results.json]

Every other stage proves a plugin loads, or lists code to read. This one runs what it does, because a plugin can change which grids it
acts on with every stage still passing. Nothing here is written per plugin:

  discover   a probe plugin loads beside the plugin and reads its commands and settings
  generate   one idle run, then each command, on its own and with each on/off setting its code can reach flipped
  compare    each scenario runs on every build: the working tree's Magnetar and Torch builds, and the published builds

Published builds come from MagnetarHub, QuasarHub's listed repositories and torchapi.com, into cache/behaviour. A plugin found on none of
them is new, and has no baseline. Comparisons cross platforms on purpose: a port should behave like what it came from, so a difference
between a Torch build and a Magnetar build is worth reading, whichever developer made them.

The AI operator reads what each command touches, and commands that do not overlap share a server. Writers go first, so the first command in
each server starts on a clean world, and a later command that shows a difference reruns alone. See classification.py. With no current reading,
each command gets its own server and every setting is flipped.

Every build runs at least twice, and a grid that differs between runs of one build depends on timing, so it is marked unstable, with the builds it
happened on. A build run only once could never be flagged. PLUGINCHECK_REPEATS sets how many runs each build gets. Every world
is thrown away with its server, and each result is kept in cache/behaviour by build, game, settings and tools. This ends as `review`: a difference says two builds disagree, not which is right, and no differences
says only that these scenarios agreed. See README.md for what it cannot prove.

Servers run side by side, each pinned to its own CORES_PER_SERVER logical cores so they cannot slow each other's timing. How many is worked
out from what Docker can see. PLUGINCHECK_BEHAVIOUR_WORKERS overrides it, and PLUGINCHECK_BEHAVIOUR_CORES sets the cores each.

PLUGINCHECK_BEHAVIOUR_ONLY limits a run to scenarios containing that text, for working on this stage.
"""
import concurrent.futures, glob, hashlib, io, itertools, json, os, queue, re, shutil, subprocess, sys, threading, time, urllib.request, zipfile
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
sys.path.insert(0, os.path.join(here, 'behaviour'))
import classification, fixture, githubtoken, startlog

MAGNETAR = 'ghcr.io/cometworks/magnetar:latest'
TORCH = os.environ.get('PLUGINCHECK_TORCH_IMAGE', 'plugincheck-torch-proton:latest')
SDK = 'mcr.microsoft.com/dotnet/sdk:10.0'
VOLUMES = {'magnetar': 'plugincheck-ds-magnetar', 'torch': 'plugincheck-ds-torch'}
HUBS = {'MagnetarHub': 'https://codeload.github.com/CometWorks/magnetar-hub/zip/refs/heads/main',
        'QuasarHub': 'https://codeload.github.com/CometWorks/quasar-hub/zip/refs/heads/main'}
TORCH_LIST = 'https://torchapi.com/api/plugins'
TORCH_DOWNLOAD = 'https://torchapi.com/plugin/download/'
NO_CRASH_REPORTS = 'crash.steampowered.com:127.0.0.1'
PROBE = 'PluginCheckProbe'
PROBE_ID = '6f0c1f3e-52a4-4b7e-9e1d-3c9a7d2e4b10'
TORCH_PROBE_ID = '6f0c1f3e-52a4-4b7e-9e1d-3c9a7d2e4b11'
START_TIMEOUT = 20 * 60
IDLE_SECONDS = 120
COMPARED = ('isStatic', 'size', 'blocks', 'owners', 'parent', 'connection')
# Measured on AutoStation: 16 servers on 2 cores finished about 1.4 times faster than 8 on 4, with the same results.
CORES_PER_SERVER = int(os.environ.get('PLUGINCHECK_BEHAVIOUR_CORES', '2'))
# A server settles near 1 GB and peaked at 0.95 GiB when measured. This leaves room for the compile and world load at the start of each one.
MEMORY_PER_SERVER_GIB = 1.8

sys.stdout.reconfigure(line_buffering=True)
target = json.load(open(sys.argv[1], encoding='utf-8'))
run_dir = os.path.abspath(sys.argv[2])
result_path = sys.argv[3] if len(sys.argv) > 3 else None
repo = target['repo']
work = os.path.join(run_dir, 'behaviour')
cache = os.path.normpath(os.path.join(here, '..', 'cache', 'behaviour'))
counter = itertools.count(1)
magnetar_cache_lock = threading.Lock()
magnetar_cache = None


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


def get(url, timeout=120):
    return urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'PluginCheck', 'Accept': 'application/json, application/zip, */*', **githubtoken.headers(url)}), timeout=timeout).read()


def status(**fields):
    """For the live panel. check.py reads these lines, and they never reach the logs."""
    print('@@' + json.dumps(fields), flush=True)


def read_text(path):
    return open(path, encoding='utf-8', errors='replace').read() if os.path.exists(path) else ''


def capacity():
    """How many servers fit side by side, and which logical cores each gets, from what Docker can really use."""
    info = docker('info', '--format', '{{.NCPU}} {{.MemTotal}}').stdout.split()
    cores, memory = (int(info[0]), int(info[1]) // 2 ** 30) if len(info) == 2 else (CORES_PER_SERVER, MEMORY_PER_SERVER_GIB)
    fits = max(min(cores // CORES_PER_SERVER, int(memory / MEMORY_PER_SERVER_GIB)), 1)
    workers = int(os.environ.get('PLUGINCHECK_BEHAVIOUR_WORKERS', fits))
    print(f'{cores} logical cores and {memory} GiB in Docker: {workers} servers at a time, {CORES_PER_SERVER} cores each')
    slots = queue.Queue()
    for i in range(workers):
        first = (i * CORES_PER_SERVER) % max(cores - CORES_PER_SERVER + 1, 1)
        slots.put(f'{first}-{first + CORES_PER_SERVER - 1}')
    return workers, slots


# ---- World and instruments, built once per run ----

def copy_world_files():
    """The game's own saves that the fixture starts from."""
    out = os.path.join(work, 'world-source')
    os.makedirs(out, exist_ok=True)
    done = docker('run', '--rm', '-v', f'{VOLUMES["magnetar"]}:/ds:ro', '-v', f'{out}:/out', '--entrypoint', 'bash', SDK, '-c',
                  'cp "/ds/Content/CustomWorlds/Empty World/"SANDBOX_0_0_0_.sbs "/ds/Content/CustomWorlds/Empty World/"Sandbox*.sbc /out/ && '
                  'cp "/ds/Content/CustomWorlds/Moon Base/PC/SANDBOX_0_0_0_.sbs" /out/moon.sbs && '
                  'cp "/ds/Content/CustomWorlds/Moon Base/PC/"*.vx2 /out/')
    if done.returncode:
        finish('not run', reason='could not copy the game saves the fixture needs: ' + done.stderr[-300:])
    save = os.path.join(work, 'fixture')
    fixture.build(out, os.path.join(out, 'moon.sbs'), save)
    return save


def build_torch_probe():
    """The probe for Torch, built like any .NET Framework plugin against Torch's and the game's own assemblies."""
    if docker('image', 'inspect', TORCH).returncode:
        return None, f'the {TORCH} image is not built'
    libs = os.path.join(work, 'torch-libs')
    os.makedirs(libs, exist_ok=True)
    name = f'plugincheck-behaviour-libs-{os.getpid()}'
    docker('create', '--name', name, TORCH)
    copied = docker('cp', f'{name}:/opt/torch/.', libs)
    docker('rm', '-v', name)
    if copied.returncode:
        return None, 'could not copy the Torch libraries'
    source = os.path.join(work, 'probe-source')
    shutil.copytree(os.path.join(here, 'behaviour', 'torchprobe'), os.path.join(source, 'torchprobe'), dirs_exist_ok=True)
    shutil.copytree(os.path.join(here, 'behaviour', 'probe'), os.path.join(source, 'probe'), dirs_exist_ok=True)
    made = docker('run', '--rm', '--cpus', '4', '--memory', '6g', '-v', f'{work}:/work', '-v', f'{VOLUMES["torch"]}:/ds:ro', '--entrypoint', 'bash', SDK, '-c',
                  'set -e; cd /work/probe-source/torchprobe; dotnet build -c Release -v q -nologo -p:LibFolder=/work/torch-libs '
                  '-p:GameFolder=/ds/DedicatedServer64 -o /work/probe-out >/work/probe-build.log 2>&1')
    dll = os.path.join(work, 'probe-out', PROBE + '.dll')
    if made.returncode or not os.path.exists(dll):
        return None, 'the Torch probe did not build: ' + read_text(os.path.join(work, 'probe-build.log'))[-400:]
    probe_zip = os.path.join(work, PROBE + '.zip')
    with zipfile.ZipFile(probe_zip, 'w') as z:
        z.write(dll, PROBE + '.dll')
        z.write(os.path.join(here, 'behaviour', 'torchprobe', 'manifest.xml'), 'manifest.xml')
    shutil.rmtree(libs, ignore_errors=True)
    return probe_zip, None


# ---- Builds to compare ----

def working_builds():
    builds = []
    if 'magnetar' in target:
        manifest = target['magnetar']['manifest']
        root = ET.parse(os.path.join(repo, manifest)).getroot()
        source = os.path.join(work, 'working-magnetar-source')
        os.makedirs(source, exist_ok=True)
        shutil.copy(os.path.join(repo, manifest), source)
        skip = shutil.ignore_patterns('bin', 'obj')
        for d in root.findall('SourceDirectories/Directory'):
            shutil.copytree(os.path.join(repo, d.text.strip()), os.path.join(source, d.text.strip()), ignore=skip, dirs_exist_ok=True)
        builds.append({'name': 'working-magnetar', 'host': 'magnetar', 'source': source, 'manifest': os.path.basename(manifest),
                       'id': root.findtext('Id').strip(), 'plugin': root.findtext('FriendlyName').strip(), 'published': False})
    if 'torch' in target:
        path = os.path.join(repo, target['torch']['zip'])
        if os.path.exists(path):
            builds.append({'name': 'working-torch', 'host': 'torch', 'zip': path, 'id': target['torch']['guid'], 'plugin': target['name'], 'published': False})
    return builds


def hub_build(wanted):
    """The published source of a plugin on a hub, by its id, from the repository and commit the hub pins."""
    for hub, url in HUBS.items():
        try:
            archive = zipfile.ZipFile(io.BytesIO(get(url)))
        except OSError as e:
            print(f'could not read {hub}: {e}')
            continue
        for item in archive.namelist():
            if not item.endswith('.xml'):
                continue
            try:
                root = ET.fromstring(archive.read(item))
            except ET.ParseError:
                continue
            if (root.findtext('Id') or '').strip().lower() != wanted.lower():
                continue
            repo_id, commit = (root.findtext('RepoId') or '').strip(), (root.findtext('Commit') or '').strip()
            if not repo_id or not commit:
                continue
            folder = os.path.join(cache, f'{repo_id.replace("/", "-")}-{commit[:12]}')
            if not os.path.isdir(folder):
                print(f'downloading the published {repo_id} at {commit[:12]} from {hub}')
                source = zipfile.ZipFile(io.BytesIO(get(f'https://codeload.github.com/{repo_id}/zip/{commit}', 300)))
                for info in source.infolist():
                    relative = info.filename.split('/', 1)[-1]
                    if relative and not info.is_dir() and '..' not in relative:
                        path = os.path.join(folder, relative)
                        os.makedirs(os.path.dirname(path), exist_ok=True)
                        open(path, 'wb').write(source.read(info))
            manifest = os.path.basename(item)
            open(os.path.join(folder, manifest), 'wb').write(archive.read(item))
            return {'name': f'published-{hub.lower()}', 'host': 'magnetar', 'source': folder, 'manifest': manifest, 'id': wanted,
                    'plugin': root.findtext('FriendlyName').strip(), 'published': True, 'version': commit[:12], 'from': hub}
    return None


def torch_build(guid):
    """The published build of a plugin on torchapi.com, by its guid. These are compiled, with no source."""
    try:
        listing = json.loads(get(TORCH_LIST))['plugins']
    except (OSError, ValueError, KeyError) as e:
        print(f'could not read torchapi.com: {e}')
        return None
    plugin = next((p for p in listing if (p.get('guid') or '').lower() == guid.lower()), None)
    if not plugin:
        return None
    version = plugin.get('latestVersion') or 'latest'
    path = os.path.join(cache, f'{guid}-{re.sub(r"[^A-Za-z0-9.]", "_", version)}.zip')
    if not os.path.exists(path):
        print(f'downloading the published {plugin.get("name")} {version} from torchapi.com')
        os.makedirs(cache, exist_ok=True)
        open(path, 'wb').write(get(TORCH_DOWNLOAD + guid, 300))
    return {'name': 'published-torch', 'host': 'torch', 'zip': path, 'id': guid, 'plugin': plugin.get('name'), 'published': True, 'version': version, 'from': 'torchapi.com'}


def published_builds():
    found, ids = [], set()
    for build in working_builds():
        ids.add(build['id'])
    if 'quasar' in target:
        ids.add(ET.parse(os.path.join(repo, target['quasar']['manifest'])).getroot().findtext('Id').strip())
    for wanted in sorted(ids):
        for build in (hub_build(wanted), torch_build(wanted)):
            if build and not any(b['name'] == build['name'] for b in found):
                found.append(build)
    return found


# ---- One server ----

def scenario_file(build, batch):
    lines = [f'plugin={build["plugin"]}', f'mode={batch[0].get("mode", "run")}']
    lines += [f'set:{k}={v}' for k, v in batch[0].get('settings', {}).items()]
    for plan in batch:
        # A command read as changing nothing has nothing to wait for.
        minimum, quiet = (2, 3) if plan.get('readonly') else (plan.get('min', 10), 10)
        lines.append(f"step={minimum}|{quiet}|{plan.get('command', '')}")
    return '\n'.join(lines) + '\n'


def dedicated_config(world_path):
    return f'''<?xml version="1.0" encoding="utf-8"?>
<MyConfigDedicated xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <SessionSettings>
    <GameMode>Survival</GameMode>
    <OnlineMode>PRIVATE</OnlineMode>
    <MaxPlayers>4</MaxPlayers>
  </SessionSettings>
  <IP>0.0.0.0</IP>
  <SteamPort>8766</SteamPort>
  <ServerPort>27016</ServerPort>
  <ServerName>PluginCheck behaviour</ServerName>
  <WorldName>Fixture</WorldName>
  <LoadWorld>{world_path}</LoadWorld>
  <PauseGameWhenEmpty>false</PauseGameWhenEmpty>
  <RemoteApiEnabled>false</RemoteApiEnabled>
</MyConfigDedicated>
'''


def seed_magnetar(build, seed, folder):
    probe_folder = os.path.join(folder, 'source', PROBE)
    shutil.copytree(os.path.join(here, 'behaviour', 'probe'), probe_folder)
    name = build['plugin']
    shutil.copytree(build['source'], os.path.join(folder, 'source', name), ignore=shutil.ignore_patterns('.git', 'bin', 'obj'))
    if magnetar_cache:
        shutil.copytree(magnetar_cache, os.path.join(seed, 'Magnetar'), dirs_exist_ok=True)
    plugins = [(build['id'], name, build['manifest']), (PROBE_ID, PROBE, PROBE + '.xml')]
    dev = ''.join(f'    <LocalFolderConfig>\n      <Id>{escape(i)}</Id>\n      <DebugBuild>false</DebugBuild>\n    </LocalFolderConfig>\n' for i, _, _ in plugins)
    write(os.path.join(seed, 'Magnetar', 'Profiles', 'Current.xml'), f'''<?xml version="1.0"?>
<Profile xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <Name>Current</Name>
  <GitHub />
  <DevFolder>
{dev}  </DevFolder>
  <Local />
  <Mods />
</Profile>
''')
    local = ''.join(f'    <LocalPlugin>\n      <Name>{escape(n)}</Name>\n      <Folder>/plugin/{escape(n)}</Folder>\n      <File>{escape(m)}</File>\n      <Enabled>true</Enabled>\n    </LocalPlugin>\n'
                    for _, n, m in plugins)
    write(os.path.join(seed, 'Magnetar', 'Sources', 'sources.xml'), f'''<?xml version="1.0"?>
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
{local}  </LocalPluginSources>
</SourcesConfig>
''')
    write(os.path.join(seed, 'Instance', 'SpaceEngineers-Dedicated.cfg'), dedicated_config('/data/Instance/Saves/Fixture'))


def start_magnetar(build, seed, folder, container, volume, cores):
    # With Magnetar's downloads cached these servers ask GitHub for little, so a token stays out of them unless it is needed.
    token_args, token_env = ([], None) if magnetar_cache else githubtoken.for_container()
    subprocess.run(['docker', 'run', '-d', '--name', container, '--user', '0:0', '--cpuset-cpus', cores, '--memory', f'{int(MEMORY_PER_SERVER_GIB * 1024)}m', '--entrypoint', 'bash', *token_args,
                    '-e', 'DS_DIR=/ds', '-e', 'DS_UPDATE=0', '-e', 'SERVER_NAME=PluginCheck behaviour',
                    '-v', f'{VOLUMES["magnetar"]}:/ds', '-v', f'{volume}:/data', '-v', f'{seed}:/seed:ro',
                    '-v', f'{os.path.join(folder, "source")}:/plugin:ro', MAGNETAR, '-c', 'cp -r /seed/. /data/; exec tini -- magnetar-entrypoint -consent deny'],
                   check=True, stdout=subprocess.DEVNULL, env=token_env)


def seed_torch(build, seed, folder, probe_zip):
    plugins = os.path.join(folder, 'plugins')
    os.makedirs(plugins)
    shutil.copy(build['zip'], os.path.join(plugins, 'plugin.zip'))
    shutil.copy(probe_zip, plugins)
    write(os.path.join(seed, 'Torch.cfg'), f'''<?xml version="1.0" encoding="utf-8"?>
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
  <Plugins><guid>{escape(build['id'])}</guid><guid>{TORCH_PROBE_ID}</guid></Plugins>
</TorchConfig>
''')
    write(os.path.join(seed, 'Instance', 'SpaceEngineers-Dedicated.cfg'), dedicated_config('Z:\\run-data\\Instance\\Saves\\Fixture'))


def start_torch(build, seed, folder, container, volume, cores):
    subprocess.run(['docker', 'run', '-d', '--name', container, '--cpuset-cpus', cores, '--memory', f'{int(MEMORY_PER_SERVER_GIB * 1024)}m', '--add-host', NO_CRASH_REPORTS, '-e', 'DS_UPDATE=0',
                    '-v', f'{VOLUMES["torch"]}:/ds', '-v', f'{volume}:/run-data', '-v', f'{seed}:/seed/data:ro',
                    '-v', f'{os.path.join(folder, "plugins")}:/seed/plugins:ro', TORCH], check=True, stdout=subprocess.DEVNULL)


def run_server(build, batch, save, probe_zip):
    cores = slots.get()
    status(kind='server', cores=cores, build=build['name'], scenario=' + '.join(p.get('name', 'discover') for p in batch))
    try:
        return serve(build, batch, save, probe_zip, cores)
    finally:
        status(kind='done', cores=cores)
        slots.put(cores)


def serve(build, batch, save, probe_zip, cores):
    """One disposable server for the scenarios that share it. Returns what the probe recorded for each, or why it could not."""
    number = next(counter)
    folder = os.path.join(work, 'runs', f'{number:04}-{build["name"]}')
    seed = os.path.join(folder, 'seed')
    os.makedirs(seed)
    shutil.copytree(save, os.path.join(seed, 'Instance', 'Saves', 'Fixture'))
    write(os.path.join(seed, 'Instance', 'probe-scenario.txt'), scenario_file(build, batch))
    container = f'plugincheck-behaviour-{os.getpid()}-{number}'
    volume = container + '-data'
    magnetar = build['host'] == 'magnetar'
    if magnetar:
        seed_magnetar(build, seed, folder)
        start_magnetar(build, seed, folder, container, volume, cores)
        instance, info = '/data/Instance', '/data/Magnetar/info.log'
    else:
        seed_torch(build, seed, folder, probe_zip)
        start_torch(build, seed, folder, container, volume, cores)
        instance, info = '/run-data/Instance', None

    start, state = time.time(), 'timeout'
    while time.time() - start < START_TIMEOUT:
        if docker('exec', container, 'test', '-f', instance + '/probe.json').returncode == 0:
            state = 'recorded'
            break
        # Torch logs a fatal error and then hangs instead of exiting.
        if not magnetar and docker('exec', container, 'bash', '-c', 'grep -qF "[FATAL]" /run-data/Logs/Torch-*.log').returncode == 0:
            state = 'fatal'
            break
        if docker('inspect', '-f', '{{.State.Running}}', container).stdout.strip() != 'true':
            state = 'exited'
            break
        time.sleep(1)
    seconds = int(time.time() - start)
    # The record is written and the world is thrown away, so a clean shutdown only wastes the core. Torch ignores the signal under Proton
    # and would sit out the whole timeout. Logs flush as they are written, so nothing is lost.
    docker('kill', container)
    open(os.path.join(folder, 'container.log'), 'w', encoding='utf-8').write(docker('logs', container).stdout)
    docker('cp', f'{container}:{instance}/probe.json', os.path.join(folder, 'probe.json'))
    if magnetar:
        docker('cp', f'{container}:/data/Magnetar/.', os.path.join(folder, 'magnetar'))
    else:
        docker('cp', f'{container}:/run-data/Logs/.', os.path.join(folder, 'logs'))
    docker('rm', '-f', '-v', container)
    docker('volume', 'rm', volume)
    shutil.rmtree(seed, ignore_errors=True)
    shutil.rmtree(os.path.join(folder, 'source'), ignore_errors=True)
    shutil.rmtree(os.path.join(folder, 'plugins'), ignore_errors=True)

    log = read_text(os.path.join(folder, 'magnetar', 'info.log'))
    blocked = startlog.loader_stopped(log) if magnetar else None
    if magnetar:
        remember_magnetar_cache(folder)
        shutil.rmtree(os.path.join(folder, 'magnetar', 'DevFolder'), ignore_errors=True)
        shutil.rmtree(os.path.join(folder, 'magnetar', 'GitHub'), ignore_errors=True)
    text = log + read_text(os.path.join(folder, 'container.log')) + ''.join(read_text(p) for p in glob.glob(os.path.join(folder, 'logs', '*.log')))
    mark = build['plugin'].lower()
    lines = sorted({re.sub(r'^\S+ \S+\s*', '', l).strip()[:200] for l in text.splitlines() if mark in l.lower() and re.search(r'convert|error|exception|fail|warn', l, re.I)})
    record = {'build': build['name'], 'state': state, 'seconds': seconds, 'blocked': blocked, 'log': lines[:40]}
    path = os.path.join(folder, 'probe.json')
    if os.path.exists(path):
        try:
            record.update(json.load(open(path, encoding='utf-8')))
        except ValueError as e:
            record['failed'] = f'the probe wrote something unreadable: {e}'
    elif not blocked:
        record['failed'] = f'the server {state} after {seconds}s with no record. Last lines: {startlog.last_lines(text)[:400]}'
    shutil.rmtree(os.path.join(folder, 'magnetar', 'Instance'), ignore_errors=True)
    steps = record.pop('steps', None)
    if steps is None:
        return [{**record, 'scenario': plan.get('name', 'discover')} for plan in batch]
    return [{**record, **step, 'scenario': plan['name'], 'sharedAfter': position} for position, (plan, step) in enumerate(zip(batch, steps))]


def remember_magnetar_cache(folder):
    """Keeps what Magnetar downloaded, because every server start otherwise spends GitHub's limit on the same files."""
    global magnetar_cache
    source = os.path.join(folder, 'magnetar', 'GitHub')
    with magnetar_cache_lock:
        if magnetar_cache or not os.path.isdir(source):
            return
        keep = os.path.join(work, 'magnetar-cache')
        shutil.copytree(source, os.path.join(keep, 'GitHub'))
        magnetar_cache = keep


def tool_hash():
    """Everything of ours that shapes a result."""
    digest = hashlib.sha256()
    for path in ('behaviour.py', os.path.join('behaviour', 'fixture.py')):
        digest.update(open(os.path.join(here, path), 'rb').read())
    for folder in ('probe', 'torchprobe'):
        digest.update(classification.folder_hash(os.path.join(here, 'behaviour', folder)).encode())
    return digest.hexdigest()[:16]


def game_hash(host):
    found = docker('run', '--rm', '-v', f'{VOLUMES[host]}:/ds:ro', '--entrypoint', 'sha256sum', SDK, '/ds/DedicatedServer64/Sandbox.Game.dll').stdout.split()
    return found[0][:16] if found else 'unknown'


def build_hash(build):
    if build['published']:
        return build['version']
    if 'source' in build:
        return classification.folder_hash(build['source'])
    return hashlib.sha256(open(build['zip'], 'rb').read()).hexdigest()[:16]


REPEATS = max(2, int(os.environ.get('PLUGINCHECK_REPEATS', '2')))


def job_key(build, batch, repeat):
    """What a result depends on. If any part of this changes, the server runs again. Fixed for the run, apart from the scenario."""
    parts = [build['name'], fingerprints[build['name']], tools, games[build['host']], repeat,
             [[p.get('command'), p.get('settings'), p.get('min')] for p in batch]]
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:24]


def remembered(build, batch, repeat):
    path = os.path.join(cache, 'results', job_key(build, batch, repeat) + '.json')
    if os.environ.get('PLUGINCHECK_REFRESH') or not os.path.exists(path):
        return None
    return [{**r, 'reused': True} for r in json.load(open(path, encoding='utf-8'))]


def run_and_remember(build, batch, repeat):
    """A published build never changes, and neither does unchanged code, so a result only has to be earned once. Failures are never kept."""
    results = run_with_retry(build, batch, save, probe_zip)
    if all(r.get('state') == 'recorded' and 'after' in r for r in results):
        folder = os.path.join(cache, 'results')
        os.makedirs(folder, exist_ok=True)
        json.dump(results, open(os.path.join(folder, job_key(build, batch, repeat) + '.json'), 'w', encoding='utf-8'))
    return results


def run_and_remember_discovery(build):
    results = run_with_retry(build, [{'mode': 'discover'}], save, probe_zip)
    if results[0].get('state') == 'recorded' and 'commands' in results[0]:
        folder = os.path.join(cache, 'results')
        os.makedirs(folder, exist_ok=True)
        json.dump(results, open(os.path.join(folder, job_key(build, [{'mode': 'discover'}], 0) + '.json'), 'w', encoding='utf-8'))
    return results


def run_with_retry(build, batch, save, probe_zip):
    """Magnetar not starting says nothing about the plugin. A rate limit can be waited out, and a dropped download tried again."""
    attempts = 0
    while True:
        records_here = run_server(build, batch, save, probe_zip)
        record = records_here[0]
        if not record.get('blocked'):
            return records_here
        if 'rate limit' in record['blocked']:
            wait = startlog.rate_limit_wait()
            print(f'  waiting {wait}s for GitHub\'s rate limit', flush=True)
            time.sleep(wait)
            continue
        attempts += 1
        if attempts >= 3:
            return records_here
        print(f"  Magnetar did not start for {build['name']}, trying again: {record['blocked'][:160]}", flush=True)
        time.sleep(20)


# ---- What to run ----

def argument(kind):
    if kind == 'Boolean':
        return 'true'
    if kind in ('Int32', 'Int64', 'UInt32', 'UInt64', 'Single', 'Double'):
        return '1'
    return 'player-large'


def command_lines(command):
    """Ways to call a command: required values filled in, then with every optional on/off switched on, if it has any."""
    required = [argument(p['type']) for p in command['parameters'] if not p['optional']]
    base = ' '.join([command['line'], *required])
    yield base
    switches = [p for p in command['parameters'] if p['optional'] and p['type'] == 'Boolean']
    if switches and len(switches) == len([p for p in command['parameters'] if p['optional']]):
        yield ' '.join([base, *['true'] * len(switches)])


def generate(discovered, relevant):
    """One idle run, then every way to call every command, on its own and with each on/off setting flipped: those it reads, or all with no reading."""
    commands, settings = {}, {}
    for found in discovered.values():
        for command in found.get('commands', []):
            for line in command_lines(command):
                commands.setdefault(line.lower(), line)
        for setting in found.get('settings', []):
            if setting['type'] == 'Boolean':
                settings.setdefault(setting['name'], str(setting['default'].lower() != 'true').lower())
    plans = [{'name': 'idle', 'min': IDLE_SECONDS}]
    for key, line in sorted(commands.items()):
        plans.append({'name': key, 'command': line})
        reads = relevant.get(classification.best_match(line, relevant))
        for name, value in sorted(settings.items()):
            if reads is None or name in reads:
                plans.append({'name': f'{key} with {name}={value}', 'command': line, 'settings': {name: value}})
    only = os.environ.get('PLUGINCHECK_BEHAVIOUR_ONLY')
    return [p for p in plans if only.lower() in p['name'].lower()] if only else plans


def fits(found, plan):
    """Whether a build has what a scenario needs. A build without a command or setting cannot run it, and that is itself worth reading."""
    lines = {c['line'].lower() for c in found.get('commands', [])}
    if plan.get('command') and not any(plan['name'].startswith(l) for l in lines):
        return False
    names = {s['name'] for s in found.get('settings', [])}
    return all(k in names for k in plan.get('settings', {}))


# ---- Comparing ----

def differences(first, second):
    """Where two records of the same scenario disagree at the end, grid by grid."""
    out = {}
    a, b = first.get('after', {}).get('grids', {}), second.get('after', {}).get('grids', {})
    for name in sorted(set(a) | set(b)):
        if name not in a or name not in b:
            out[name, 'exists'] = (name in a, name in b)
            continue
        for field in COMPARED:
            if a[name].get(field) != b[name].get(field):
                out[name, field] = (a[name].get(field), b[name].get(field))
    return out


def touched(record):
    """Real changes to the world. Which grid of a docked pair is the parent can move on its own, and where a grid is and how fast it goes are
    recorded to explain a result, not compared, so none of those count."""
    return {k: v for k, v in changes(record).items() if k[1] not in ('parent', 'connection', 'position', 'speed')}


def undeclared(record, known):
    """What a command changed that its classification says it does not write. The record watches grids, entities, players and factions."""
    _, writes = classification.footprint(record.get('command'), known)
    if '*' in writes:
        return []
    found = []
    if touched(record) and 'grids' not in writes:
        found.append('grids')
    before, after = record.get('before', {}), record.get('after', {})
    if before.get('identities') != after.get('identities') and 'players' not in writes:
        found.append('players')
    if before.get('factions') != after.get('factions') and 'factions' not in writes:
        found.append('factions')
    return found


def changes(record):
    """What a scenario did to the world, which says whether it reached the plugin's logic at all."""
    return differences({'after': record.get('before', {})}, record)


def disagreements(first, second):
    """Where two runs of a scenario changed the world differently. Each compares with its own start, because some things differ from
    the moment a world loads. Which grid of a docked pair is the parent moves on its own, so it is recorded but not compared."""
    a, b = touched(first), touched(second)
    return {key: (a[key][1] if key in a else 'no change', b[key][1] if key in b else 'no change')
            for key in sorted(set(a) | set(b)) if key not in a or key not in b or a[key][1] != b[key][1]}


# ---- The run ----

if 'magnetar' not in target and 'torch' not in target:
    finish('not present', reason='this stage runs Magnetar and Torch plugins')
if docker('run', '--rm', '-v', f'{VOLUMES["magnetar"]}:/ds:ro', '--entrypoint', 'test', SDK, '-f', '/ds/DedicatedServer64/Sandbox.Game.dll').returncode:
    finish('not run', reason='no dedicated server yet. Run the magnetar stage first.')

shutil.rmtree(work, ignore_errors=True)
os.makedirs(work)
workers, slots = capacity()
save = copy_world_files()
# The magnetar stage has already downloaded what Magnetar needs, so no server here has to ask GitHub for it again.
# Failing that, what an earlier run kept for this game build.
already = os.path.join(run_dir, 'data', 'Magnetar', 'GitHub')
hashed = docker('run', '--rm', '-v', f'{VOLUMES["magnetar"]}:/ds:ro', '--entrypoint', 'sha256sum', MAGNETAR, '/ds/DedicatedServer64/Sandbox.Game.dll').stdout.split()
kept = os.path.normpath(os.path.join(here, '..', 'cache', 'magnetar-github', hashed[0][:16])) if hashed else ''
for source in (already, kept):
    if source and os.path.isdir(source) and any(os.path.isdir(os.path.join(source, n, 'Bin')) for n in os.listdir(source)):
        magnetar_cache = os.path.join(work, 'magnetar-cache')
        shutil.copytree(source, os.path.join(magnetar_cache, 'GitHub'))
        break
builds = working_builds() + published_builds()
if not builds:
    finish('not run', reason='no build to run: no Magnetar manifest, no Torch zip, and nothing published')
print('builds: ' + ', '.join(f"{b['name']}" + (f" ({b['version']} from {b['from']})" if b['published'] else '') for b in builds))
if not any(b['published'] for b in builds):
    print('Not found on MagnetarHub, QuasarHub or torchapi.com, so this is a new plugin with no baseline.')

probe_zip = None
if any(b['host'] == 'torch' for b in builds):
    probe_zip, problem = build_torch_probe()
    if problem:
        print(problem)
        builds = [b for b in builds if b['host'] != 'torch']
        if not builds:
            finish('not run', reason=problem)

print('discovering each build', flush=True)
fingerprints = {b['name']: build_hash(b) for b in builds}
tools = tool_hash()
games = {host: game_hash(host) for host in {b['host'] for b in builds}}
discovered = {}
for build in builds:
    known_build = remembered(build, [{'mode': 'discover'}], 0)
    discovered[build['name']] = (known_build or run_and_remember_discovery(build))[0]
blocked = [r['blocked'] for r in discovered.values() if r.get('blocked')]
broken = {n: r.get('failed') or r.get('blocked') for n, r in discovered.items() if r.get('failed') or r.get('blocked')}
for name, reason in broken.items():
    print(f'FAILED  {name} left out of every comparison, because it could not be discovered: {str(reason)[:300]}')
usable = [b for b in builds if b['name'] not in broken]
if not usable:
    finish('unable to verify' if blocked else 'fail', reason=(blocked or list(broken.values()))[0][:500], discovered=discovered)

plans = generate({n: r for n, r in discovered.items() if n not in broken}, classification.settings_read(target['name'], repo))
print(f"{len(plans)} scenarios from {sum(len(r.get('commands', [])) for r in discovered.values())} commands and "
      f"{sum(len(r.get('settings', [])) for r in discovered.values())} settings", flush=True)
known = classification.load(target['name'], repo)
if known is None:
    print('No current AI reading of these commands, so each one gets its own server. See the CLASSIFY lines from the commands stage.')
else:
    print(f'{len(known)} commands classified, so those that do not overlap share a server')
jobs = []
skipped = []
for build in usable:
    fitting = []
    for plan in plans:
        if fits(discovered[build['name']], plan):
            fitting.append(plan)
        else:
            skipped.append({'build': build['name'], 'scenario': plan['name']})
    for batch in classification.batches(fitting, known or {}):
        batch = [{**p, 'readonly': not classification.footprint(p.get('command'), known or {})[1]} if p.get('command') else p for p in batch]
        for repeat in range(REPEATS):
            jobs.append((build, batch, repeat))
kept_results = [(j, remembered(*j)) for j in jobs]
kept_results = [(j, r) for j, r in kept_results if r]
kept_jobs = {id(j[1]) + j[2] * 1000003 + hash(j[0]['name']) for j, _ in kept_results}
jobs = [j for j in jobs if id(j[1]) + j[2] * 1000003 + hash(j[0]['name']) not in kept_jobs]
print(f'{len(jobs)} server starts, {workers} at a time, {len(kept_results)} results reused from earlier runs', flush=True)

records = {}
unstable, real, quiet, failures = {}, [], [], []
pairs = [(a['name'], b['name']) for a in usable for b in usable if a['published'] and not b['published']]
working = [b['name'] for b in usable if not b['published']]
pairs += [(working[i], working[j]) for i in range(len(working)) for j in range(i + 1, len(working))]


def clock(seconds):
    seconds = int(seconds)
    return f'{seconds // 3600}h{seconds % 3600 // 60:02}m' if seconds >= 3600 else f'{seconds // 60}m{seconds % 60:02}s'


def compare(plan):
    """Posts what one scenario found the moment all its servers are done, instead of keeping it for the end."""
    for build in usable:
        a = records.get((build['name'], plan['name'], 0))
        for repeat in range(1, REPEATS):
            b = records.get((build['name'], plan['name'], repeat))
            if a and b:
                for (grid, field) in disagreements(a, b):
                    builds = unstable.setdefault((plan['name'], grid, field), set())
                    if build['name'] not in builds:
                        builds.add(build['name'])
                        print(f"UNSTABLE  {plan['name']}  {grid} {field} differs between runs of {build['name']}", flush=True)
    for first, second in pairs:
        a, b = records.get((first, plan['name'], 0)), records.get((second, plan['name'], 0))
        if not a or not b or 'after' not in a or 'after' not in b:
            continue
        found = [{'scenario': plan['name'], 'grid': grid, 'field': field, first.replace('-', '_'): left, second.replace('-', '_'): right,
                  'between': [first, second], 'unstable': (plan['name'], grid, field) in unstable} for (grid, field), (left, right) in disagreements(a, b).items()]
        if a.get('replies') != b.get('replies'):
            found.append({'scenario': plan['name'], 'grid': '', 'field': 'replies', first.replace('-', '_'): a.get('replies'), second.replace('-', '_'): b.get('replies'), 'between': [first, second], 'unstable': False})
        for d in found:
            left, right = d['between']
            print(f"{'UNSTABLE' if d['unstable'] else 'DIFF'}  {d['scenario']}  {d['grid']} {d['field']}: {left}={d[left.replace('-', '_')]}  {right}={d[right.replace('-', '_')]}")
        real.extend(found)
    done = [r for (_, s, _), r in records.items() if s == plan['name'] and 'after' in r]
    if done and not any(changes(r) for r in done):
        quiet.append(plan['name'])


waiting = {p['name']: 0 for p in plans}
for _, batch, _ in jobs + [j for j, _ in kept_results]:
    for plan in batch:
        waiting[plan['name']] += 1
by_name = {p['name']: p for p in plans}
after_stage = int(os.environ.get('PLUGINCHECK_SECONDS_AFTER', '0'))
started = time.time()
wrongly_classified = {}


def take(build, repeat, results):
    """Stores what one server recorded, and posts whatever that finishes."""
    for record in results:
        name = record['scenario']
        records[build['name'], name, repeat] = record
        if record.get('failed') or record.get('state') != 'recorded':
            failure = {'build': build['name'], 'scenario': name, 'reason': str(record.get('failed') or record.get('blocked') or record.get('state'))[:400]}
            failures.append(failure)
            print(f"FAILED  {failure['build']}  {failure['scenario']}: {failure['reason'][:200]}", flush=True)
        elif known and undeclared(record, known):
            wrongly_classified[name] = undeclared(record, known)
            print(f"FAILED  {name} changed {', '.join(wrongly_classified[name])}, which its classification does not list. It reruns alone, and the reading is wrong.", flush=True)
        waiting[name] -= 1
        if waiting[name] == 0:
            compare(by_name[name])


for (build, batch, repeat), results in kept_results:
    take(build, repeat, results)

with concurrent.futures.ThreadPoolExecutor(workers) as pool:
    futures = {pool.submit(run_and_remember, b, batch, r): (b, batch, r) for b, batch, r in jobs}
    for done, future in enumerate(concurrent.futures.as_completed(futures), 1):
        build, batch, repeat = futures[future]
        results = future.result()
        elapsed = time.time() - started
        left = (len(jobs) - done) * elapsed / done
        status(kind='progress', done=done, total=len(jobs), left=int(left), overall=int(left + after_stage), failed=len(failures))
        moved = sum(len(touched(r)) for r in results if 'after' in r)
        print(f"[{done}/{len(jobs)} {done * 100 // len(jobs)}%] {clock(left)} left here, {clock(left + after_stage)} left overall  |  "
              f"{build['name']}  {' + '.join(p['name'] for p in batch)}: {results[0]['state']} in {results[0]['seconds']}s, {moved} changes", flush=True)
        take(build, repeat, results)

# Only the first command in a server starts clean. A later one ran after another command, so a difference it shows must be seen on its own.
later = {r['scenario'] for r in records.values() if r.get('sharedAfter')}
suspect = ({d['scenario'] for d in real} & later) | set(wrongly_classified)
if suspect:
    print(f'{len(suspect)} scenarios that shared a server showed a difference, so they rerun alone before anything is reported', flush=True)
    again = [(b, {**by_name[n]}, r) for n in sorted(suspect) for b in usable if fits(discovered[b['name']], by_name[n]) for r in range(REPEATS)]
    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(run_and_remember, b, [p], r): (b, r) for b, p, r in again}
        for future in concurrent.futures.as_completed(futures):
            build, repeat = futures[future]
            for record in future.result():
                records[build['name'], record['scenario'], repeat] = record
    real[:] = [d for d in real if d['scenario'] not in suspect]
    unstable_kept = {k: v for k, v in unstable.items() if k[0] not in suspect}
    unstable.clear()
    unstable.update(unstable_kept)
    for name in sorted(suspect):
        compare(by_name[name])

json.dump([dict(r) for r in records.values()], open(os.path.join(run_dir, 'behaviour-records.json'), 'w', encoding='utf-8'), indent=1)

print(f"{len(records)} results, {len(failures)} failed, {len(real)} differences, {sum(d['unstable'] for d in real)} of them unstable, "
      f"{len(quiet)} of {len(plans)} scenarios changed nothing in the world")
print('Each difference is a lead: it says two builds disagree, not which is right. No differences would say only that these scenarios agreed.')
crashed = any('stack' in f['reason'].lower() for f in failures)
finish('fail' if crashed else 'review', builds=[{k: b.get(k) for k in ('name', 'host', 'published', 'version', 'from')} for b in builds], scenarios=[p['name'] for p in plans],
       servers=len(jobs), reused=len(kept_results), failures=failures, differences=real, unstable=[{'scenario': k[0], 'grid': k[1], 'field': k[2], 'builds': sorted(v)} for k, v in unstable.items()],
       noChange=quiet, skipped=skipped, wronglyClassified=wrongly_classified, filtered=bool(os.environ.get('PLUGINCHECK_BEHAVIOUR_ONLY')), buildsLeftOut=broken,
       note='A difference is a lead. No differences proves nothing beyond these scenarios.')
