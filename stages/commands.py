"""Finds player-level commands that can reach another player's data.

usage: commands.py <target.json> <run dir> [results.json]

A command any player can run must not show them data about other players, and a compiler has no idea whose data it is.
stages/commandcheck follows each player-level command through the plugin. It reports game reads keyed by a player who is not the caller,
reads from someone else's player, grid or faction, and anything traced from a named player or a list of players into a reply, which
also finds a plugin's own stored data. A command with no [Permission] is admin only on both hosts, so it is left out.

A warning reads as a person would report it, with the commands as players type them:

  !economy top and !economy check <player> (PlayerCommands.cs#L116-L122) let any player read every balance.

Some of these are intended, such as a public leaderboard, so a hit is a warning and this never fails. It ends as `review`, and a person
or AI must judge each one. It reads code, so a command that reaches data another way can be missed, and no warnings proves nothing.
"""
import io, json, os, re, shutil, subprocess, sys, urllib.request, zipfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import classification
import xml.etree.ElementTree as ET

MAGNETAR = 'ghcr.io/cometworks/magnetar:latest'
TORCH = 'plugincheck-torch-proton:latest'
SDK = 'mcr.microsoft.com/dotnet/sdk:10.0'
NET48 = 'https://api.nuget.org/v3-flatcontainer/microsoft.netframework.referenceassemblies.net48/1.0.3/microsoft.netframework.referenceassemblies.net48.1.0.3.nupkg'

target = json.load(open(sys.argv[1], encoding='utf-8'))
run_dir = os.path.abspath(sys.argv[2])
result_path = sys.argv[3] if len(sys.argv) > 3 else None
repo = target['repo']
work = os.path.join(run_dir, 'commands')
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
    name = f'plugincheck-commandrefs-{os.getpid()}'
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

shutil.copytree(os.path.join(here, 'commandcheck'), os.path.join(work, 'src'), dirs_exist_ok=True)
command = ('set -e; cd /work/src; dotnet build -c Release -v q -nologo -o /work/out >/work/build.log 2>&1; '
           f'dotnet /work/out/commandcheck.dll /work/sources.txt {" ".join(refs)} /ds/DedicatedServer64')
r = docker('run', '--rm', '--cpus', '4', '--memory', '6g', '-v', f'{work}:/work', '-v', f'{volume}:/ds:ro', '--entrypoint', 'bash', SDK, '-c', command)
try:
    report = json.loads(r.stdout.strip().splitlines()[-1])
except (IndexError, ValueError):
    build = os.path.join(work, 'build.log')
    detail = open(build, encoding='utf-8', errors='replace').read()[-800:] if os.path.exists(build) else ''
    finish('not run', reason='the analysis did not finish', output=(r.stdout + r.stderr)[-800:] + detail)

leads = report['commands']
for x in leads:
    x['file'] = x['file'].replace('/work/source/', '')
    for read in x['reads']:
        read['file'] = read['file'].replace('/work/source/', '')
print(f"{report['files']} files, {report['allCommands']} commands, {report['playerCommands']} that any player can run, {report['compileErrors']} compile errors in the analysis")
if report['compileErrors'] > 25:
    print('Many compile errors means missing references, so some calls were not followed. Treat this as incomplete.')
for e in report.get('errorSample', [])[:5]:
    print('  ' + e[:150])


def typed(lead):
    """A command as a player runs it. Optional arguments only add noise to a warning."""
    return re.sub(r' \[[^\]]+\]', '', lead['command'])


def together(words):
    return ' and '.join(words) if len(words) < 3 else ', '.join(words[:-1]) + ' and ' + words[-1]


def sentence(commands, where, verbs, data):
    verb = 'lets' if len(commands) == 1 else 'let'
    return f"{together(commands)} ({where}) {verb} any player {together(verbs)} every {data}."


# One sentence per file and kind of data, so commands with the same problem read together, as a person would report them.
grouped = {}
named_only = {}
for x in leads:
    name = os.path.basename(x['file'])
    if not x['reads']:
        named_only.setdefault(name, []).append(x)
    kinds = {}
    for read in x['reads']:
        kinds.setdefault(read['data'], set()).add(read['verb'])
    for data, verbs in kinds.items():
        grouped.setdefault((name, tuple(sorted(verbs, reverse=True)), data), []).append(x)

warnings = []
for (name, verbs, data), same in sorted(grouped.items()):
    same = sorted({typed(x): x for x in same}.values(), key=lambda x: x['line'])
    where = f"{name}#L{min(x['line'] for x in same)}-L{max(x['endLine'] for x in same)}"
    text = sentence([typed(x) for x in same], where, list(verbs), data)
    warnings.append(text)
    print('WARN  ' + text)
    calls = sorted({f"{r['member']} at {r['file']}:{r['line']}" for x in same for r in x['reads'] if r['data'] == data})
    for call in calls[:6]:
        print(f'      {call}')
for name, same in sorted(named_only.items()):
    same = sorted(same, key=lambda x: x['line'])
    for x in same:
        text = f"{typed(x)} ({name}#L{x['line']}-L{x['endLine']}) takes a player, but no data was traced from it. Read what it shows them."
        warnings.append(text)
        print('CHECK ' + text)
# Torch finds a command's arguments with IndexOf over the whole line, so a value that is also in the command's own words starts too early.
for x in report.get('torchArguments', []):
    x['file'] = x['file'].replace('/work/source/', '')
    line = f"{x['command'][1:]} {x['value']}"
    text = (f"{x['command']} {x['value']} ({os.path.basename(x['file'])}#L{x['line']}): Torch finds arguments by searching the whole line, so '{x['value']}' is found in the "
            f"command's own words and the handler gets '{line[line.lower().find(x['value'].lower()):]}'. A handler that reads its first word never sees '{x['value']}'.")
    warnings.append(text)
    print('WARN  ' + text)
if report.get('torchFreeText'):
    text = (f"These Torch commands take free text, and any value that also appears in their own words reaches them with those words in front of it. Check what each does "
            f"with that: {', '.join(sorted(report['torchFreeText']))}")
    warnings.append(text)
    print('CHECK ' + text)
report['warnings'] = warnings
for x in report['everyCommand']:
    x['file'] = x['file'].replace('/work/source/', '')
classified = classification.load(target['name'], repo)
if classified is None:
    print(f"CLASSIFY  No current reading of these commands, so each one gets its own server. Read every handler below, then write "
          f"{classification.path_for(target['name'])} with source {classification.source_hash(repo)}, with real examples for every command "
          f"that takes a value. See stages/classification.py.")
    for x in sorted(report['everyCommand'], key=lambda v: v['command'].lower()):
        print(f"CLASSIFY  {x['command']}  ({x['file']}:{x['line']})")
else:
    print(f"{len(classified)} commands classified, so those that do not overlap share a server")
print('These are warnings. Some are intended, such as a public leaderboard, and having none proves nothing.')
finish('review', **report, covered=folders)
