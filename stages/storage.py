"""Lists what a plugin does with files and state that goes wrong on a cluster or a network mount, which no single server test shows.

usage: storage.py <target.json> <run dir> [results.json]

The kinds are explained at the top of stages/storagecheck/Program.cs. UNGUARDED, GAME-THREAD and READ-DEFAULT are about any shared or network
folder. NODE-FILE and NODE-STATE only matter on a cluster, which is not a supported setup for every plugin, so they are for information.
Results are leads to read, so this ends as `review` when it finds any. A clean result proves nothing: a path handed in from outside, a call through a
delegate, or a loader's own config store is not seen.
"""
import io, json, os, shutil, subprocess, sys, urllib.request, zipfile
import xml.etree.ElementTree as ET

MAGNETAR = 'ghcr.io/cometworks/magnetar:latest'
TORCH = 'plugincheck-torch-proton:latest'
SDK = 'mcr.microsoft.com/dotnet/sdk:10.0'
NET48 = 'https://api.nuget.org/v3-flatcontainer/microsoft.netframework.referenceassemblies.net48/1.0.3/microsoft.netframework.referenceassemblies.net48.1.0.3.nupkg'
ORDER = ('UNGUARDED', 'READ-DEFAULT', 'GAME-THREAD', 'NODE-FILE', 'NODE-STATE')

target = json.load(open(sys.argv[1], encoding='utf-8'))
run_dir = os.path.abspath(sys.argv[2])
result_path = sys.argv[3] if len(sys.argv) > 3 else None
repo = target['repo']
work = os.path.join(run_dir, 'storage')
here = os.path.dirname(os.path.abspath(__file__))


def docker(*args):
    return subprocess.run(['docker', *args], capture_output=True, text=True, encoding='utf-8', errors='replace')


def finish(status, **fields):
    print(status, fields.get('reason', ''))
    if result_path:
        json.dump({'status': status, **fields}, open(result_path, 'w', encoding='utf-8'), indent=1)
    sys.exit(0)


def copy_out(image, folder, into):
    os.makedirs(into, exist_ok=True)
    name = f'plugincheck-storagerefs-{os.getpid()}'
    docker('create', '--name', name, image)
    done = docker('cp', f'{name}:{folder}/.', into)
    docker('rm', '-v', name)
    return done.returncode == 0


volume = 'plugincheck-ds-magnetar'
if docker('run', '--rm', '-v', f'{volume}:/ds:ro', '--entrypoint', 'test', SDK, '-f', '/ds/DedicatedServer64/Sandbox.Game.dll').returncode:
    finish('not run', reason='no dedicated server yet. Run the magnetar stage first.')

folders = []
if 'magnetar' in target:
    root = ET.parse(os.path.join(repo, target['magnetar']['manifest'])).getroot()
    folders += [d.text.strip() for d in root.findall('SourceDirectories/Directory')] or ['.']
if target.get('torch', {}).get('project'):
    folders.append(os.path.dirname(target['torch']['project']) or '.')
    if os.path.isdir(os.path.join(repo, 'Shared')) and 'Shared' not in folders:
        folders.append('Shared')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # stages run with -I
import repocopy
folders += [f for f in repocopy.companion_folders(repo, target) if f not in folders]
if not folders:
    finish('not run', reason='needs a Magnetar manifest, a Torch project or a Quasar companion to know which source to read')

source = os.path.join(work, 'source')
skip = shutil.ignore_patterns('.claude', 'bin', 'obj', '.git', '.vs', 'GameBinaries', 'TorchBinaries')
for folder in folders:
    shutil.copytree(os.path.join(repo, folder), os.path.join(source, folder), ignore=skip, dirs_exist_ok=True)
repocopy.add_implicit_usings(repo, folders, source)
listed = ['/work/source/' + os.path.relpath(os.path.join(d, f), source).replace(os.sep, '/')
          for d, _, fs in os.walk(source) for f in fs if f.endswith('.cs')]
open(os.path.join(work, 'sources.txt'), 'w', encoding='utf-8', newline='\n').write('\n'.join(listed))

archive = zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(NET48, timeout=120).read()))
net48 = os.path.join(work, 'net48')
os.makedirs(net48, exist_ok=True)
for item in archive.namelist():
    if item.startswith('build/.NETFramework/v4.8/') and item.endswith('.dll'):
        open(os.path.join(net48, os.path.basename(item)), 'wb').write(archive.read(item))
refs = ['/work/net48']
if copy_out(MAGNETAR, '/opt/magnetar/Libraries/MagnetarInterim', os.path.join(work, 'libs')):
    refs.append('/work/libs')
if 'torch' in target and docker('image', 'inspect', TORCH).returncode == 0 and copy_out(TORCH, '/opt/torch', os.path.join(work, 'torch')):
    refs.append('/work/torch')

shutil.copytree(os.path.join(here, 'storagecheck'), os.path.join(work, 'src'), dirs_exist_ok=True)
command = ('set -e; cd /work/src; dotnet build -c Release -v q -nologo -o /work/out >/work/build.log 2>&1; '
           f'dotnet /work/out/storagecheck.dll /work/sources.txt {" ".join(refs)} /ds/DedicatedServer64')
r = docker('run', '--rm', '--cpus', '4', '--memory', '6g', '-v', f'{work}:/work', '-v', f'{volume}:/ds:ro', '--entrypoint', 'bash', SDK, '-c', command)
try:
    report = json.loads(r.stdout.strip().splitlines()[-1])
except (IndexError, ValueError):
    build = os.path.join(work, 'build.log')
    detail = open(build, encoding='utf-8', errors='replace').read()[-800:] if os.path.exists(build) else ''
    finish('not run', reason='the analysis did not finish', output=(r.stdout + r.stderr)[-800:] + detail)

findings = report['findings']
for x in findings:
    x['file'] = x['file'].replace('/work/source/', '')
# Clusters are Magnetar and Quasar. A Torch server is one process, so per node leads say nothing about its own code.
torch_folder = os.path.dirname(target.get('torch', {}).get('project', '')).replace(os.sep, '/')
if torch_folder:
    findings = report['findings'] = [x for x in findings if not (x['kind'].startswith('NODE') and x['file'].startswith(torch_folder + '/'))]
print(f"{report['files']} files, {report['roots']} places the game calls on its update thread, {report['compileErrors']} compile errors in the analysis, {len(findings)} leads")
for e in report.get('errorSample', [])[:5]:
    print('  ' + e[:150])
for x in sorted(findings, key=lambda v: (ORDER.index(v['kind']), v['file'], v['line'])):
    print(f"LEAD  {x['kind']}  {x['file']}:{x['line']}  {x['code'][:80]}")
    print(f"      {x['why']}" + (f"  ({x['via']})" if x.get('via') else ''))
print('These are leads, and the NODE lines only matter on a cluster. Having none proves nothing.')
finish('review' if findings else 'pass', **report, covered=folders)
