"""Finds game objects used off the game thread, by following background code through the plugin's own methods.

usage: threads.py <target.json> <run dir> [results.json]

Space Engineers is not thread safe. Reading or changing game state from Task.Run, a thread, a timer or after an await can
corrupt the world or crash the server, and it passes every test, because it depends on timing. Results are leads to read:
some reads are harmless. A person or AI must judge each one, so this ends as `review`, and is never a pass by itself.

This only reads code, so it needs reference DLLs and no Windows: the game's, the loaders', and Microsoft's .NET Framework
reference assemblies. It covers the manifest's source directories and the Torch host project.
"""
import io, json, os, shutil, subprocess, sys, urllib.request, zipfile
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


if 'magnetar' not in target and 'torch' not in target:
    finish('not run', reason='needs a Magnetar manifest or a Torch project to know which source to read')

volume = 'plugincheck-ds-magnetar'
if docker('run', '--rm', '-v', f'{volume}:/ds:ro', '--entrypoint', 'test', SDK, '-f', '/ds/DedicatedServer64/Sandbox.Game.dll').returncode:
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
skip = shutil.ignore_patterns('bin', 'obj', '.git', '.vs', 'GameBinaries', 'TorchBinaries')
listed = []
for folder in folders:
    shutil.copytree(os.path.join(repo, folder), os.path.join(source, folder), ignore=skip, dirs_exist_ok=True)
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
    docker('rm', name)
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

shutil.copytree(os.path.join(here, 'threadcheck'), os.path.join(work, 'src'), dirs_exist_ok=True)
command = ('set -e; cd /work/src; dotnet build -c Release -v q -nologo -o /work/out >/work/build.log 2>&1; '
           f'dotnet /work/out/threadcheck.dll /work/sources.txt {" ".join(refs)} /ds/DedicatedServer64')
r = docker('run', '--rm', '--cpus', '4', '--memory', '6g', '-v', f'{work}:/work', '-v', f'{volume}:/ds:ro', '--entrypoint', 'bash', SDK, '-c', command)
try:
    report = json.loads(r.stdout.strip().splitlines()[-1])
except (IndexError, ValueError):
    build = os.path.join(work, 'build.log')
    detail = open(build, encoding='utf-8', errors='replace').read()[-800:] if os.path.exists(build) else ''
    finish('not run', reason='the analysis did not finish', output=(r.stdout + r.stderr)[-800:] + detail)

for x in report['findings']:
    x['file'] = x['file'].replace('/work/source/', '')
print(f"{report['files']} files, {report['roots']} background entry points, {report['compileErrors']} compile errors in the analysis, {len(report['findings'])} leads")
if report['compileErrors'] > 25:
    print('Many compile errors means missing references, so some calls were not followed. Treat this as incomplete.')
for e in report.get('errorSample', [])[:6]:
    print('  ' + e[:150])
for x in report['findings'][:40]:
    print(f"LEAD  {x['file']}:{x['line']}  {x['member']}\n      from {x['path']}")
finish('review' if report['findings'] else 'pass', **report, covered=folders)
