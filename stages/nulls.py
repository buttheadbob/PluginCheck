"""Lists uses of game values that can really be null, with no check before them.

usage: nulls.py <target.json> <run dir> [results.json]

The game's assemblies have no nullable annotations, so the compiler reports nothing about them, and a test run only hits a null
when the situation arises, such as a dead player. The member list is in stages/nullcheck/Program.cs. Add to it as real cases
turn up. MySession.Static is left out on purpose: it is set whenever a world is loaded, and it would bury everything else.

Results are leads for a person or AI to read, so this ends as `review` when it finds any. It only compares text, so it can
miss a real case, and a clean result does not prove the code is safe.
"""
import io, json, os, shutil, subprocess, sys, urllib.request, zipfile
import xml.etree.ElementTree as ET

MAGNETAR = 'ghcr.io/cometworks/magnetar:latest'
TORCH = 'plugincheck-torch-proton:latest'
SDK = 'mcr.microsoft.com/dotnet/sdk:10.0'
NET48 = 'https://api.nuget.org/v3-flatcontainer/microsoft.netframework.referenceassemblies.net48/1.0.3/microsoft.netframework.referenceassemblies.net48.1.0.3.nupkg'

target = json.load(open(sys.argv[1], encoding='utf-8'))
run_dir = os.path.abspath(sys.argv[2])
result_path = sys.argv[3] if len(sys.argv) > 3 else None
repo = target['repo']
work = os.path.join(run_dir, 'nulls')
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
    name = f'plugincheck-nullrefs-{os.getpid()}'
    docker('create', '--name', name, image)
    done = docker('cp', f'{name}:{folder}/.', into)
    docker('rm', name)
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
if not folders:
    finish('not run', reason='needs a Magnetar manifest or a Torch project to know which source to read')

source = os.path.join(work, 'source')
skip = shutil.ignore_patterns('bin', 'obj', '.git', '.vs', 'GameBinaries', 'TorchBinaries')
for folder in folders:
    shutil.copytree(os.path.join(repo, folder), os.path.join(source, folder), ignore=skip, dirs_exist_ok=True)
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

shutil.copytree(os.path.join(here, 'nullcheck'), os.path.join(work, 'src'), dirs_exist_ok=True)
command = ('set -e; cd /work/src; dotnet build -c Release -v q -nologo -o /work/out >/work/build.log 2>&1; '
           f'dotnet /work/out/nullcheck.dll /work/sources.txt {" ".join(refs)} /ds/DedicatedServer64')
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
print(f"{report['files']} files, {report['uses']} uses of nullable game values, {report['compileErrors']} compile errors in the analysis, {len(findings)} with no check before them")
for e in report.get('errorSample', [])[:5]:
    print('  ' + e[:150])
for x in sorted(findings, key=lambda v: (v['member'], v['file'], v['line']))[:60]:
    print(f"LEAD  {x['file']}:{x['line']}  {x['expression'][:60]}  ({x['member']}: {x['why']})")
finish('review' if findings else 'pass', **report, covered=folders)
