"""Finds game objects used off the game thread, by following background code through the plugin's own methods.

usage: threads.py <target.json> <run dir> [results.json]

Space Engineers is not thread safe. Reading or changing game state from Task.Run, a thread, a timer or after an await can
corrupt the world or crash the server, and it passes every test, because it depends on timing. Results are leads to read:
some reads are harmless. A person or AI must judge each one, so this ends as `review`, and is never a pass. No leads proves
nothing: a plugin moved onto the game thread loses its leads, and can still change what it does.

Some game members also behave differently by thread. MyCubeGrid.BigOwners gives a subgrid with no owned blocks its parent's
owners on the game thread, and an empty list anywhere else. A catalog of those members is generated from the game's own IL by
stages/threadcatalog and kept in cache/threadcatalog by game build, so a game update regenerates it. Every plugin call to one
is listed, on any thread.

This only reads code, so it needs reference DLLs and no Windows: the game's, the loaders', and Microsoft's .NET Framework
reference assemblies. It covers the manifest's source directories and the Torch host project.
"""
import glob, hashlib, io, json, os, shutil, subprocess, sys, urllib.request, zipfile
import xml.etree.ElementTree as ET

MAGNETAR = 'ghcr.io/cometworks/magnetar:latest'
SDK = 'mcr.microsoft.com/dotnet/sdk:10.0'
TORCH = 'plugincheck-torch-proton:latest'
NET48 = 'https://api.nuget.org/v3-flatcontainer/microsoft.netframework.referenceassemblies.net48/1.0.3/microsoft.netframework.referenceassemblies.net48.1.0.3.nupkg'

target = json.load(open(sys.argv[1], encoding='utf-8'))
run_dir = os.path.abspath(sys.argv[2])
result_path = sys.argv[3] if len(sys.argv) > 3 else None
repo = target['repo']
work = os.path.join(run_dir, 'threads')
here = os.path.dirname(os.path.abspath(__file__))


def docker(*args):
    return subprocess.run(['docker', *args], capture_output=True, text=True, encoding='utf-8', errors='replace')


def finish(status, **fields):
    print(status, fields.get('reason', ''))
    if result_path:
        json.dump({'status': status, **fields}, open(result_path, 'w', encoding='utf-8'), indent=1)
    sys.exit(0)


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # stages run with -I
import repocopy
import pin
if 'magnetar' not in target and 'torch' not in target and not repocopy.companion_folders(repo, target):
    finish('not run', reason='needs a Magnetar manifest, a Torch project or a Quasar companion to know which source to read')

volume = 'plugincheck-ds-magnetar'
if docker('run', *pin.args(), '--rm', '-v', f'{volume}:/ds:ro', '--entrypoint', 'test', SDK, '-f', '/ds/DedicatedServer64/Sandbox.Game.dll').returncode:
    finish('not run', reason='no dedicated server yet. Run the magnetar stage first.')

source = os.path.join(work, 'source')
folders = []
if 'magnetar' in target:
    root = ET.parse(os.path.join(repo, target['magnetar']['manifest'])).getroot()
    folders += [d.text.strip() for d in root.findall('SourceDirectories/Directory')] or ['.']
if 'torch' in target and target['torch'].get('project'):
    folders.append(os.path.dirname(target['torch']['project']) or '.')
    if os.path.isdir(os.path.join(repo, 'Shared')) and 'Shared' not in folders:
        folders.append('Shared')
folders += [f for f in repocopy.companion_folders(repo, target) if f not in folders]
skip = repocopy.unlinked(shutil.ignore_patterns('.claude', 'bin', 'obj', '.git', '.vs', 'GameBinaries', 'TorchBinaries'))
listed = []
for folder in folders:
    shutil.copytree(os.path.join(repo, folder), os.path.join(source, folder), ignore=skip, dirs_exist_ok=True)
repocopy.add_implicit_usings(repo, folders, source)
for d, _, files in os.walk(source):
    for f in files:
        if f.endswith('.cs'):
            listed.append('/work/source/' + os.path.relpath(os.path.join(d, f), source).replace(os.sep, '/'))
open(os.path.join(work, 'sources.txt'), 'w', encoding='utf-8', newline='\n').write('\n'.join(listed))



def copy_out(image, folder, into):
    os.makedirs(into, exist_ok=True)
    name = f'plugincheck-libs-{os.getpid()}'
    docker('create', '--name', name, image)
    done = docker('cp', f'{name}:{folder}/.', into)
    docker('rm', '-v', name)
    return done.returncode == 0


refs = []
archive = zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(NET48, timeout=120).read()))
net48 = os.path.join(work, 'net48')
for item in archive.namelist():
    if item.startswith('build/.NETFramework/v4.8/') and item.endswith('.dll'):
        out = os.path.join(net48, os.path.basename(item))
        os.makedirs(net48, exist_ok=True)
        open(out, 'wb').write(archive.read(item))
refs.append('/work/net48')
if not copy_out(MAGNETAR, '/opt/magnetar/Libraries/MagnetarInterim', os.path.join(work, 'libs')):
    finish('not run', reason='could not copy the Magnetar libraries')
refs.append('/work/libs')
if 'torch' in target:
    if docker('image', 'inspect', TORCH).returncode:
        finish('not run', reason=f'the {TORCH} image is not built. See docker/torch-proton.')
    if not copy_out(TORCH, '/opt/torch', os.path.join(work, 'torch')):
        finish('not run', reason='could not copy the Torch libraries')
    refs.append('/work/torch')


def game_build():
    """A hash of the game's assemblies and the catalog's generator, which is what a catalog is true for."""
    sums = docker('run', *pin.args(), '--rm', '-v', f'{volume}:/ds:ro', '--entrypoint', 'bash', SDK, '-c',
                  'cd /ds/DedicatedServer64; sha256sum Sandbox.*.dll SpaceEngineers.*.dll VRage*.dll | sort -k2')
    if sums.returncode:
        return None
    # The generator counts too, so a change to what it finds is not hidden behind a cached catalog.
    generator = open(os.path.join(here, 'threadcatalog', 'Program.cs'), 'rb').read()
    return hashlib.sha256(sums.stdout.encode() + generator).hexdigest()[:16]


def compare_catalogs(old_path, new_path):
    """What a game update changed, because a member that starts checking the thread can break a plugin that never changed."""
    ids = lambda path: {m['id'] for m in json.load(open(path, encoding='utf-8'))['members']}
    old, new = ids(old_path), ids(new_path)
    print(f'  since the last catalog: {len(new - old)} members added, {len(old - new)} removed')
    for m in sorted(new - old)[:15]:
        print(f'  + {m}')
    for m in sorted(old - new)[:15]:
        print(f'  - {m}')


def thread_catalog():
    """The catalog for this game build, generated the first time that build is seen."""
    build = game_build()
    if not build:
        finish('not run', reason='could not read the game assemblies to tell which build this is')
    folder = os.path.normpath(os.path.join(here, '..', 'cache', 'threadcatalog'))
    path = os.path.join(folder, build + '.json')
    if os.path.exists(path) and not os.environ.get('PLUGINCHECK_REFRESH'):
        return path, build
    print(f'generating the thread catalog for game build {build}')
    earlier = sorted(glob.glob(os.path.join(folder, '*.json')), key=os.path.getmtime)
    shutil.copytree(os.path.join(here, 'threadcatalog'), os.path.join(work, 'catalog-src'), dirs_exist_ok=True)
    made = docker('run', *pin.args(), '--rm', '--cpus', '4', '--memory', '6g', '-v', f'{work}:/work', '-v', f'{volume}:/ds:ro', '--entrypoint', 'bash', SDK, '-c',
                  'set -e; cd /work/catalog-src; dotnet build -c Release -v q -nologo -o /work/catalog-out >/work/catalog-build.log 2>&1; '
                  'dotnet /work/catalog-out/threadcatalog.dll /ds/DedicatedServer64 /work/catalog.json')
    print('  ' + made.stdout.strip()[-300:])
    produced = os.path.join(work, 'catalog.json')
    if made.returncode or not os.path.exists(produced):
        finish('not run', reason='could not generate the thread catalog', output=(made.stdout + made.stderr)[-800:])
    os.makedirs(folder, exist_ok=True)
    shutil.copy(produced, path)
    if earlier and os.path.basename(earlier[-1]) != build + '.json':
        compare_catalogs(earlier[-1], path)
    return path, build


catalog_path, build = thread_catalog()
shutil.copy(catalog_path, os.path.join(work, 'catalog.json'))
shutil.copytree(os.path.join(here, 'threadcheck'), os.path.join(work, 'src'), dirs_exist_ok=True)
command = ('set -e; cd /work/src; dotnet build -c Release -v q -nologo -o /work/out >/work/build.log 2>&1; '
           f'dotnet /work/out/threadcheck.dll /work/sources.txt /work/catalog.json {" ".join(refs)} /ds/DedicatedServer64')
r = docker('run', *pin.args(), '--rm', '--cpus', '4', '--memory', '6g', '-v', f'{work}:/work', '-v', f'{volume}:/ds:ro', '--entrypoint', 'bash', SDK, '-c', command)
try:
    report = json.loads(r.stdout.strip().splitlines()[-1])
except (IndexError, ValueError):
    build = os.path.join(work, 'build.log')
    detail = open(build, encoding='utf-8', errors='replace').read()[-800:] if os.path.exists(build) else ''
    finish('not run', reason='the analysis did not finish', output=(r.stdout + r.stderr)[-800:] + detail)

for x in report['findings'] + report['threadSensitive']:
    x['file'] = x['file'].replace('/work/source/', '')
print(f"{report['files']} files, {report['roots']} background entry points, {report['compileErrors']} compile errors in the analysis, "
      f"{len(report['findings'])} leads, {len(report['threadSensitive'])} calls to thread-sensitive members (catalog of {report['catalogued']} for game build {build})")
if report['compileErrors'] > 25:
    print('Many compile errors means missing references, so some calls were not followed. Treat this as incomplete.')
for e in report.get('errorSample', [])[:6]:
    print('  ' + e[:150])
for x in report['findings'][:40]:
    print(f"LEAD  {x['file']}:{x['line']}  {x['member']}\n      from {x['path']}")
# Most useful first: a returned value goes on to steer the plugin, and background code is where the two answers differ.
report['threadSensitive'].sort(key=lambda x: (x['dependsOnCaller'] != 'result', x['context'] != 'background', x['depth'], x['file'], x['line']))
for x in report['threadSensitive']:
    print(f"THREAD-SENSITIVE  {x['file']}:{x['line']}  {x['member']} in {x['inMember']}  ({x['context']})")
    print(f"      its {x['dependsOnCaller']} depends on the calling thread: {x['seed']}")
    if ' > ' in x['via']:
        print(f"      through {x['via']}")
print('These are leads. None of them says the plugin is wrong, and having none would not say it is right.')
finish('review', **report, covered=folders, gameBuild=build)
