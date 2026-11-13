"""Checks every package a plugin uses against the NuGet security advisories.

usage: dependencies.py <target.json> <run dir> [results.json]

Reads project files, packages.config and the hub manifests' NuGetReferences, since hub loaders ignore project files. All
of them go into one throwaway project, so a package is checked even when its plugin project cannot be restored here.
Clean means no advisory is published today. It does not mean a package is safe.
"""
import json, os, re, subprocess, sys
import xml.etree.ElementTree as ET
from xml.sax.saxutils import quoteattr

SDK = 'mcr.microsoft.com/dotnet/sdk:10.0'
SKIP = {'.claude', '.git', 'bin', 'obj', 'packages', 'GameBinaries', 'TorchBinaries', 'Build', '.vs', 'node_modules'}

target = json.load(open(sys.argv[1], encoding='utf-8'))
run_dir = os.path.abspath(sys.argv[2])
result_path = sys.argv[3] if len(sys.argv) > 3 else None
repo = target['repo']
work = os.path.join(run_dir, 'dependencies')
os.makedirs(work, exist_ok=True)


def finish(status, **fields):
    print(status, fields.get('reason', ''))
    if result_path:
        json.dump({'status': status, **fields}, open(result_path, 'w', encoding='utf-8'), indent=1)
    sys.exit(0 if status != 'fail' else 1)


packages = {}
floating = []


def add(name, version, source):
    if not name or not version or not re.fullmatch(r'\d+(\.\d+){1,3}([-.+]\w+)*', version):
        floating.append(f'{source}: {name} {version or "(no version)"}')
        return
    packages.setdefault((name, version), []).append(source)


for folder, dirs, files in os.walk(repo):
    dirs[:] = [d for d in dirs if d not in SKIP]
    for f in files:
        path = os.path.join(folder, f)
        rel = os.path.relpath(path, repo).replace(os.sep, '/')
        if f.endswith(('.csproj', '.props', '.targets')) or f == 'packages.config':
            try:
                root = ET.parse(path).getroot()
            except ET.ParseError:
                continue
            for e in root.iter():
                tag = e.tag.split('}')[-1]
                if tag == 'PackageReference':
                    add(e.get('Include') or e.get('Update'), e.get('Version') or e.findtext('Version'), rel)
                elif tag == 'package':
                    add(e.get('id'), e.get('version'), rel)

for section in ('magnetar', 'quasar'):
    manifest = target.get(section, {}).get('manifest')
    if manifest:
        root = ET.parse(os.path.join(repo, manifest)).getroot()
        for e in root.iter():
            if e.tag.split('}')[-1] == 'NuGetPackage' or e.tag.split('}')[-1] == 'PackageReference':
                add(e.get('Name') or e.findtext('Name') or e.get('Include'), e.get('Version') or e.findtext('Version'), manifest)

print(f'{len(packages)} packages: ' + ', '.join(f'{n} {v}' for n, v in sorted(packages)))
for f in floating:
    print('NOT CHECKED ' + f)
if not packages:
    finish('pass', packages=[], notChecked=floating)



def audit():
    items = '\n'.join(f'    <PackageReference Include={quoteattr(n)} Version={quoteattr(v)} />' for n, v in sorted(packages))
    open(os.path.join(work, 'audit.csproj'), 'w', encoding='utf-8').write(f'''<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net10.0</TargetFramework>
    <NoWarn>NU1701;NU1608;NU1605;NU1901;NU1902;NU1903;NU1904</NoWarn>
  </PropertyGroup>
  <ItemGroup>
{items}
  </ItemGroup>
</Project>
''')
    command = ('rm -rf /work/obj; cd /work; dotnet restore -v q >/work/restore.log 2>&1 || exit 7; '
               'dotnet list package --vulnerable --include-transitive --format json')
    return subprocess.run(['docker', 'run', '--rm', '--cpus', '4', '--memory', '4g', '-v', f'{work}:/work', '--entrypoint', 'bash', SDK, '-c', command],
                          capture_output=True, text=True, encoding='utf-8', errors='replace')


# A package that is not on nuget.org, such as a host's own SDK, cannot be audited. It must not stop the others.
for attempt in range(len(packages) + 1):
    if not packages:
        finish('pass', packages=[], notChecked=floating)
    r = audit()
    if r.returncode != 7:
        break
    log = open(os.path.join(work, 'restore.log'), encoding='utf-8', errors='replace').read()
    missing = set(re.findall(r'NU110[12]: Unable to find package (\S+?)[. ]', log)) | set(re.findall(r"NU110[12]: Unable to find package '?([\w.]+?)'?\.? ", log))
    removed = [key for key in packages if key[0] in missing or any(key[0].startswith(m) for m in missing)]
    if not removed:
        finish('not run', reason='the audit did not finish', output=log[-800:])
    for key in removed:
        floating.append(f'{packages.pop(key)[0]}: {key[0]} {key[1]} is not on nuget.org')
        print(f'NOT CHECKED {key[0]} {key[1]} is not on nuget.org')

try:
    report = json.loads(r.stdout[r.stdout.index('{'):])
except ValueError:
    finish('not run', reason='the audit did not finish', output=(r.stdout + r.stderr)[-800:])

found = []
for project in report.get('projects', []):
    for framework in project.get('frameworks', []):
        for package in framework.get('topLevelPackages', []) + framework.get('transitivePackages', []):
            for v in package.get('vulnerabilities', []):
                found.append({'package': package['id'], 'version': package.get('resolvedVersion'), 'severity': v.get('severity'), 'advisory': v.get('advisoryurl')})
for f in found:
    print(f"{f['severity'].upper()}  {f['package']} {f['version']}  {f['advisory']}")
print(f'{len(found)} advisories')
finish('fail' if found else 'pass', packages=[f'{n} {v}' for n, v in sorted(packages)], advisories=found, notChecked=floating)
