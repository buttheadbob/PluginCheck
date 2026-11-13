"""Finds packages a plugin ships that its host already provides.

usage: hostpackages.py <target.json> <run dir> [results.json]

A plugin must not bring its own copy of something the game, Torch, Magnetar or Quasar already loads. Two versions of one
assembly in a process give load failures and method-not-found errors that only some servers see. The host lists come from
the real installs, not a hand-kept list.

`error` means a copy really ships: a hub NuGet reference or a DLL in the Torch zip. `note` means only the project file names
it, which is fine for IDE use, since hub loaders ignore project files.
"""
import json, os, re, subprocess, sys, zipfile
import xml.etree.ElementTree as ET

SKIP = {'.claude', '.git', 'bin', 'obj', 'packages', 'GameBinaries', 'TorchBinaries', 'Build', '.vs', 'node_modules'}
# Packages whose DLL is not named after them.
DLL_NAMES = {'lib.harmony': '0harmony', 'harmony': '0harmony', 'protobuf-net': 'protobuf.net', 'protobuf-net.core': 'protobuf.net.core'}

target = json.load(open(sys.argv[1], encoding='utf-8'))
run_dir = os.path.abspath(sys.argv[2])
result_path = sys.argv[3] if len(sys.argv) > 3 else None
repo = target['repo']


def docker(*args):
    return subprocess.run(['docker', *args], capture_output=True, text=True, encoding='utf-8', errors='replace')


def listing(image, folder, volume=None):
    mount = ['-v', f'{volume}:/ds:ro'] if volume else []
    r = docker('run', '--rm', *mount, '--entrypoint', 'sh', image, '-c', f'ls {folder} 2>/dev/null')
    names = {os.path.splitext(n.strip().lower())[0] for n in r.stdout.splitlines() if n.strip().lower().endswith('.dll')}
    return names


magnetar = 'ghcr.io/cometworks/magnetar:latest'
quasar = 'ghcr.io/cometworks/quasar:latest'
torch = 'plugincheck-torch-proton:latest'
game = listing(magnetar, '/ds/DedicatedServer64', 'plugincheck-ds-magnetar')
hosts = {'game': game, 'Magnetar': listing(magnetar, '/opt/magnetar/Libraries/MagnetarInterim')}
hosts['Quasar'] = listing(quasar, '/opt/quasar /opt/quasar/Agent')
if docker('image', 'inspect', torch).returncode == 0:
    hosts['Torch'] = listing(torch, '/opt/torch')

missing = [name for name, dlls in hosts.items() if not dlls]
if not game:
    result = {'status': 'not run', 'reason': 'no dedicated server yet. Run the magnetar stage first.'}
    print(result['reason'])
    if result_path:
        json.dump(result, open(result_path, 'w', encoding='utf-8'), indent=1)
    sys.exit(0)

applies = {'magnetar': ['game', 'Magnetar'], 'quasar': ['Quasar'], 'torch': ['game', 'Torch']}


def providers(dll, loaders):
    dll = DLL_NAMES.get(dll.lower(), dll.lower())
    return sorted({h for loader in loaders for h in applies[loader] if dll in hosts.get(h, ())})


found = []


def report(level, source, name, by, why):
    found.append({'level': level, 'source': source, 'package': name, 'providedBy': by, 'why': why})


# 1. Hub manifests: the loader really restores these.
for section in ('magnetar', 'quasar'):
    manifest = target.get(section, {}).get('manifest')
    if not manifest:
        continue
    root = ET.parse(os.path.join(repo, manifest)).getroot()
    for e in root.iter():
        if e.tag.split('}')[-1] in ('NuGetPackage', 'PackageReference'):
            name = e.get('Name') or e.findtext('Name') or e.get('Include')
            by = providers(name or '', [section])
            if by:
                report('error', manifest, name, by, 'The hub restores this, so a second copy loads next to the host\'s.')

# 2. The Torch zip: whatever is in it gets loaded.
zip_path = os.path.join(repo, target.get('torch', {}).get('zip', ''))
if 'torch' in target and os.path.isfile(zip_path):
    own = os.path.splitext(os.path.basename(target['torch']['zip']))[0].lower()
    for entry in zipfile.ZipFile(zip_path).namelist():
        base, ext = os.path.splitext(os.path.basename(entry))
        if ext.lower() == '.dll' and base.lower() != own:
            by = providers(base, ['torch'])
            report('error' if by else 'warn', target['torch']['zip'], base, by,
                   'Shipped in the zip, and the host already has it.' if by else 'Shipped in the zip. Check it is needed and its origin is known.')

# 3. Project files: not shipped by hub builds, so these are notes.
for d, ds, fs in os.walk(repo):
    ds[:] = [x for x in ds if x not in SKIP]
    for f in fs:
        if not f.endswith('.csproj'):
            continue
        rel = os.path.relpath(os.path.join(d, f), repo).replace(os.sep, '/')
        loaders = [s for s in ('magnetar', 'quasar', 'torch') if s in target and target[s].get('project') == rel]
        if not loaders:
            loaders = ['quasar'] if rel.lower().startswith('quasarplugin') else [s for s in ('magnetar', 'torch') if s in target]
        for e in ET.parse(os.path.join(d, f)).getroot().iter():
            tag = e.tag.split('}')[-1]
            if tag == 'PackageReference' and e.get('Include'):
                by = providers(e.get('Include'), loaders)
                if by:
                    report('note', rel, e.get('Include'), by, 'Only used for building, unless the host version differs from this one.')
            elif tag == 'Reference' and e.get('Include'):
                name = e.get('Include').split(',')[0]
                private = (e.get('Private') or e.findtext('{*}Private') or e.findtext('Private') or '').lower()
                by = providers(name, loaders)
                if by and private != 'false' and 'Private' not in ''.join(c.tag for c in e):
                    report('warn', rel, name, by, 'Copied to the output by default. Set <Private>False</Private>, as the host provides it.')

errors = [x for x in found if x['level'] == 'error']
for x in sorted(found, key=lambda v: v['level']):
    print(f"{x['level'].upper():5} {x['source']}: {x['package']} is provided by {', '.join(x['providedBy']) or 'nothing known'}. {x['why']}")
print(f"Compared against {', '.join(f'{k} ({len(v)})' for k, v in hosts.items())}. {len(errors)} errors, {len(found) - len(errors)} others")
status = 'fail' if errors else 'review' if any(x['level'] == 'warn' for x in found) else 'pass'
if result_path:
    json.dump({'status': status, 'hosts': {k: len(v) for k, v in hosts.items()}, 'notListed': missing, 'findings': found},
              open(result_path, 'w', encoding='utf-8'), indent=1)
sys.exit(1 if errors else 0)
