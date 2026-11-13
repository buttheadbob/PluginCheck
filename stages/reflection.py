"""Checks every fixed reflection lookup in a plugin against the real game assemblies.

usage: reflection.py <target.json> <run dir> [results.json]

A wrong member name or BindingFlags compiles fine and only fails at runtime, often silently, which is how the review's
MOTD bug shipped. Only lookups with a literal type and name can be checked. Everything else, such as a name in a variable,
is listed as unchecked, so a reader knows to trace it by hand.
"""
import json, os, re, shutil, subprocess, sys

SDK = 'mcr.microsoft.com/dotnet/sdk:10.0'
SKIP = {'.claude', '.git', 'bin', 'obj', 'packages', 'GameBinaries', 'TorchBinaries', 'Build', '.vs', 'node_modules'}
LOOKUP = re.compile(r'typeof\(\s*([\w.]+(?:<[^>]*>)?)\s*\)\s*\.\s*Get(Method|Field|Property|NestedType)\(\s*(?:"(\w+)"|nameof\(\s*([\w.]+)\s*\))\s*(?:,\s*([^;]*?))?\)', re.S)
HARMONY = re.compile(r'\[HarmonyPatch\(\s*typeof\(\s*([\w.]+)\s*\)\s*,\s*"(\w+)"')
ANY = re.compile(r'\.Get(Method|Field|Property|NestedType)\(')
ALIASES = {'object': 'Object', 'string': 'String', 'int': 'Int32', 'long': 'Int64', 'bool': 'Boolean', 'float': 'Single', 'double': 'Double'}

target = json.load(open(sys.argv[1], encoding='utf-8'))
run_dir = os.path.abspath(sys.argv[2])
result_path = sys.argv[3] if len(sys.argv) > 3 else None
work = os.path.join(run_dir, 'reflection')
here = os.path.dirname(os.path.abspath(__file__))


def docker(*args):
    return subprocess.run(['docker', *args], capture_output=True, text=True, encoding='utf-8', errors='replace')


def finish(status, **fields):
    print(status, fields.get('reason', ''))
    if result_path:
        json.dump({'status': status, **fields}, open(result_path, 'w', encoding='utf-8'), indent=1)
    sys.exit(0 if status != 'fail' else 1)


lookups, unchecked, own_types = [], [], set()
for folder, dirs, files in os.walk(target['repo']):
    dirs[:] = [d for d in dirs if d not in SKIP]
    for f in files:
        if not f.endswith('.cs'):
            continue
        path = os.path.join(folder, f)
        rel = os.path.relpath(path, target['repo']).replace(os.sep, '/')
        text = open(path, encoding='utf-8', errors='replace').read()
        own_types.update(re.findall(r'\b(?:class|struct|enum|interface)\s+(\w+)', text))
        matched = set()
        for m in LOOKUP.finditer(text):
            line = text.count('\n', 0, m.start()) + 1
            matched.add(line)
            name = m.group(3) or (m.group(4) or '').split('.')[-1]
            flags = re.findall(r'BindingFlags\.(\w+)', m.group(5) or '')
            rest = re.sub(r'BindingFlags\.\w+|[|\s]', '', m.group(5) or '')
            if rest:
                flags.append(rest)  # A named constant such as AnyStatic: existence is checked, flags are not.
            lookups.append({'file': rel, 'line': line, 'type': ALIASES.get(m.group(1), m.group(1)), 'kind': m.group(2), 'member': name, 'flags': flags})
        for m in HARMONY.finditer(text):
            line = text.count('\n', 0, m.start()) + 1
            matched.add(line)
            # Harmony searches every member of the type, so a private method needs no flags here, unlike Type.GetMethod.
            lookups.append({'file': rel, 'line': line, 'type': m.group(1), 'kind': 'Method', 'member': m.group(2),
                            'flags': ['Public', 'NonPublic', 'Static', 'Instance']})
        for m in ANY.finditer(text):
            line = text.count('\n', 0, m.start()) + 1
            if line not in matched:
                unchecked.append(f'{rel}:{line}: {text.splitlines()[line - 1].strip()[:110]}')

# Lookups on the plugin's own types cannot be wrong for a game update, and are not in the game assemblies.
own = [l for l in lookups if l['type'].split('.')[-1].split('<')[0] in own_types]
lookups = [l for l in lookups if l not in own]
print(f'{len(lookups)} lookups on game types, {len(own)} on the plugin\'s own types, {len(unchecked)} not literal')
if not lookups:
    finish('pass', checked=0, unchecked=unchecked)

volume = next((v for v in ('plugincheck-ds-magnetar', 'plugincheck-ds-torch', 'plugincheck-ds-quasar')
               if docker('run', '--rm', '-v', f'{v}:/ds:ro', '--entrypoint', 'test', SDK, '-f', '/ds/DedicatedServer64/Sandbox.Game.dll').returncode == 0), None)
if not volume:
    finish('not run', reason='no dedicated server volume yet. Run a loader stage first.', unchecked=unchecked)

shutil.copytree(os.path.join(here, 'reflectcheck'), os.path.join(work, 'src'), dirs_exist_ok=True)
json.dump(lookups, open(os.path.join(work, 'lookups.json'), 'w', encoding='utf-8'))
command = ('set -e; cd /work/src; dotnet build -c Release -v q -nologo -o /work/out >/work/build.log 2>&1; '
           'dotnet /work/out/reflectcheck.dll /work/lookups.json /ds/DedicatedServer64')
r = docker('run', '--rm', '--cpus', '4', '--memory', '4g', '-v', f'{work}:/work', '-v', f'{volume}:/ds:ro', '--entrypoint', 'bash', SDK, '-c', command)
try:
    results = json.loads(r.stdout.strip().splitlines()[-1])
except (IndexError, ValueError):
    finish('not run', reason='the check did not finish', output=(r.stdout + r.stderr)[-1500:])

bad = [x for x in results if x['status'] in ('missing', 'wrong flags')]
unknown = [x for x in results if x['status'] == 'unknown type']
for x in bad + unknown:
    print(f"{x['status'].upper()}  {x['file']}:{x['line']}  {x['type']}.{x['member']} ({x['kind']})")
print(f"{len(results)} checked: {sum(x['status'] == 'found' for x in results)} found, "
      f"{sum(x['status'].startswith('found,') for x in results)} found with flags not checked, {len(bad)} bad, {len(unknown)} unknown type")
for u in unchecked[:20]:
    print('NOT LITERAL ' + u)
finish('fail' if bad else 'pass', checked=len(results), bad=bad, unknownTypes=unknown, unchecked=unchecked, gameFrom=volume)
