"""Builds the plugin with the .NET analyzers on, and summarizes what they find.

usage: lint.py <target.json> <run dir> [results.json]

Uses the code quality rules of the .NET SDK (CA) and the code style rules (IDE), through a real build, so results know the
actual types. A compile error fails the stage. Warnings end as `review`: they are advice, not defects, and many are matters
of taste, so report only the ones that matter.

Covers the Magnetar, Quasar and Torch projects. Torch's own build needs Windows only for its WPF pages, so a generated project
compiles its C# on Linux against the real Torch DLLs, with a stand-in for each page. A compile error there says the stand-in
fell short, so it is reported as a gap in coverage, and does not fail the stage.
"""
import collections, json, os, re, shutil, subprocess, sys
import xml.etree.ElementTree as ET
from xml.sax.saxutils import quoteattr

SDK = 'mcr.microsoft.com/dotnet/sdk:10.0'
MAGNETAR = 'ghcr.io/cometworks/magnetar:latest'
QUASAR = 'ghcr.io/cometworks/quasar:latest'
TORCH = 'plugincheck-torch-proton:latest'
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # stages run with -I
import repocopy
# Style rules that only say the code differs from a default, not that anything is wrong.
ANALYZERS = ['-p:EnableNETAnalyzers=true', '-p:AnalysisMode=Recommended', '-p:AnalysisLevel=latest', '-p:EnforceCodeStyleInBuild=true',
             '-p:GenerateDocumentationFile=false', '-p:TreatWarningsAsErrors=false', '--no-incremental', '-v:quiet', '-nologo']
WARNING = re.compile(r'^(.+?)\((\d+),\d+\): (warning|error) ([A-Z]+\d+): (.+?)(?: \[.*\])?$', re.M)

target = json.load(open(sys.argv[1], encoding='utf-8'))
run_dir = os.path.abspath(sys.argv[2])
result_path = sys.argv[3] if len(sys.argv) > 3 else None
work = os.path.join(run_dir, 'lint')
source = os.path.join(work, 'source')
os.makedirs(work, exist_ok=True)
shutil.copytree(target['repo'], source, ignore=repocopy.ignore(target['repo']))


def docker(*args):
    return subprocess.run(['docker', *args], capture_output=True, text=True, encoding='utf-8', errors='replace')


def finish(status, **fields):
    print(status, fields.get('reason', ''))
    if result_path:
        json.dump({'status': status, **fields}, open(result_path, 'w', encoding='utf-8'), indent=1)
    sys.exit(1 if status == 'fail' else 0)


builds = {}
if 'magnetar' in target and target['magnetar'].get('project'):
    name = f'plugincheck-lintsdk-{os.getpid()}'
    libs = os.path.join(work, 'libs')
    os.makedirs(libs, exist_ok=True)
    docker('create', '--name', name, MAGNETAR)
    docker('cp', f'{name}:/opt/magnetar/Libraries/MagnetarInterim/.', libs)
    docker('rm', '-v', name)
    args = ' '.join(ANALYZERS + ['-f net10.0', '-c Release', '-p:Dedicated64=/ds/DedicatedServer64', '-p:MagnetarBin=/work/libs', '-p:DeployToMagnetar=false'])
    builds['magnetar'] = docker('run', '--rm', '--cpus', '4', '--memory', '6g', '-v', f'{work}:/work', '-v', 'plugincheck-ds-magnetar:/ds:ro',
                                '--entrypoint', 'bash', SDK, '-c', f'cd /work/source; dotnet build "{target["magnetar"]["project"]}" {args} 2>&1')
if 'quasar' in target and target['quasar'].get('project'):
    args = ' '.join(ANALYZERS + ['-c Release', '-p:QuasarPluginAbstractionsAssembly=/opt/quasar/Quasar.Plugin.Abstractions.dll', '-p:DeployToQuasar=false'])
    builds['quasar'] = docker('run', '--rm', '--cpus', '4', '--memory', '6g', '-v', f'{work}:/work',
                              '--entrypoint', 'bash', QUASAR, '-c', f'cd /work/source; dotnet build "{target["quasar"]["project"]}" {args} 2>&1')


def torch_project(project):
    # Writes an SDK project with the Torch project's sources and references, and a stand-in for each XAML page.
    folder = os.path.dirname(project)
    source_dir = os.path.join(source, folder)
    root = ET.parse(os.path.join(source, project)).getroot()
    tag = lambda e: e.tag.split('}')[-1]
    files, references = [], []

    def items(tree, base):
        for e in tree.iter():
            if tag(e) == 'Compile' and e.get('Include', '').endswith('.cs'):
                path = e.get('Include').replace('$(MSBuildThisFileDirectory)', '').replace('\\', '/')
                files.append(os.path.normpath(os.path.join(base, path)).replace(os.sep, '/'))
            elif tag(e) == 'Import' and e.get('Project', '').endswith('.projitems'):
                item = os.path.normpath(os.path.join(base, e.get('Project').replace('\\', '/')))
                items(ET.parse(os.path.join(source, item)).getroot(), os.path.dirname(item).replace(os.sep, '/'))

    items(root, folder)
    for e in root.iter():
        if tag(e) != 'Reference':
            continue
        name = e.get('Include').split(',')[0]
        hint = next((c.text for c in e if tag(c) == 'HintPath'), None)
        if hint:
            dll = hint.replace('\\', '/').split('/')[-1]
            base = '/ds/DedicatedServer64' if 'GameBinaries' in hint else '/work/torch'
            references.append(f'    <Reference Include={quoteattr(name)}><HintPath>{base}/{dll}</HintPath></Reference>')
        else:
            references.append(f'    <Reference Include={quoteattr(name)} />')

    stubs = []
    for d, _, fs in os.walk(source_dir):
        for f in fs:
            if not f.endswith('.xaml'):
                continue
            text = open(os.path.join(d, f), encoding='utf-8-sig', errors='replace').read()
            match = re.search(r'x:Class\s*=\s*"([\w.]+)"', text)
            if not match:
                continue
            ns, cls = match.group(1).rsplit('.', 1)
            named = dict((n, t) for t, n in re.findall(r'<(\w+)\b[^<>]*?\s(?:x:)?Name\s*=\s*"(\w+)"', text))
            # Only Root style names on the page itself are left out: the base class already is that object.
            fields = ''.join(f'internal System.Windows.Controls.{t} {n} = null!; ' for n, t in sorted(named.items()) if t != 'UserControl')
            stubs.append(f'namespace {ns} {{ partial class {cls} {{ {fields}private void InitializeComponent() {{ }} }} }}')
    os.makedirs(os.path.join(work, 'torchlint'), exist_ok=True)
    open(os.path.join(work, 'torchlint', 'Pages.g.cs'), 'w', encoding='utf-8').write('#nullable enable\n' + '\n'.join(stubs))
    compile_items = '\n'.join(f'    <Compile Include={quoteattr("/work/source/" + f)} />' for f in sorted(set(files)))
    open(os.path.join(work, 'torchlint', 'TorchLint.csproj'), 'w', encoding='utf-8').write(f'''<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net48</TargetFramework>
    <PlatformTarget>x64</PlatformTarget>
    <LangVersion>latest</LangVersion>
    <Nullable>enable</Nullable>
    <EnableDefaultCompileItems>false</EnableDefaultCompileItems>
    <GenerateAssemblyInfo>false</GenerateAssemblyInfo>
    <AssemblyName>TorchLint</AssemblyName>
    <NoWarn>$(NoWarn);NU1701;MSB3277;MSB3243</NoWarn>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Microsoft.NETFramework.ReferenceAssemblies.net48" Version="1.0.3" PrivateAssets="all" />
{compile_items}
    <Compile Include="Pages.g.cs" />
{chr(10).join(references)}
  </ItemGroup>
</Project>
''')


torch_gap = None
if 'torch' in target and target['torch'].get('project'):
    if docker('image', 'inspect', TORCH).returncode:
        torch_gap = f'the {TORCH} image is not built'
    else:
        name = f'plugincheck-linttorch-{os.getpid()}'
        os.makedirs(os.path.join(work, 'torch'), exist_ok=True)
        docker('create', '--name', name, TORCH)
        docker('cp', f'{name}:/opt/torch/.', os.path.join(work, 'torch'))
        docker('rm', '-v', name)
        torch_project(target['torch']['project'])
        args = ' '.join(ANALYZERS + ['-c Release'])
        builds['torch'] = docker('run', '--rm', '--cpus', '4', '--memory', '6g', '-v', f'{work}:/work', '-v', 'plugincheck-ds-magnetar:/ds:ro',
                                 '--entrypoint', 'bash', SDK, '-c', f'cd /work/torchlint; dotnet build TorchLint.csproj {args} 2>&1')
if not builds:
    finish('not run', reason='no project to lint')

items, seen = [], set()
for label, build in builds.items():
    output = build.stdout + build.stderr
    if label == 'torch' and build.returncode:
        problems = [m for m in WARNING.findall(output) if m[2] == 'error']
        torch_gap = f'the generated Torch project did not compile, {len(problems)} errors. First: ' + (problems[0][4][:120] if problems else output[-200:])
        print('GAP   ' + torch_gap)
        for p in problems[:10]:
            print(f'      {p[0].split("/")[-1]}:{p[1]} {p[3]} {p[4][:110]}')
        continue
    for path, line, level, rule, text in WARNING.findall(output):
        rel = path.split('/work/source/')[-1]
        key = (rel, line, rule)
        if key not in seen:
            seen.add(key)
            items.append({'file': rel, 'line': int(line), 'level': level, 'rule': rule, 'text': text[:140]})

errors = [x for x in items if x['level'] == 'error']
counts = collections.Counter(x['rule'] for x in items if x['level'] == 'warning')
examples = {}
for x in items:
    examples.setdefault(x['rule'], x)
for x in errors[:15]:
    print(f"ERROR {x['rule']} {x['file']}:{x['line']}  {x['text']}")
for rule, n in counts.most_common(25):
    x = examples[rule]
    print(f"{n:>5} {rule}  {x['text'][:95]}  ({x['file']}:{x['line']})")
print(f'{len(errors)} errors, {sum(counts.values())} warnings across {len(counts)} rules, from {", ".join(builds)}')
failed = [k for k, b in builds.items() if b.returncode and k != 'torch']
if failed and not errors:
    print('\n'.join((builds[failed[0]].stdout + builds[failed[0]].stderr).splitlines()[-12:]))
    finish('not run', reason=f'{failed[0]} did not build')
covered = [k for k in builds if not (k == 'torch' and torch_gap)]
finish('fail' if errors else 'review' if counts or torch_gap else 'pass', errors=errors, rules=dict(counts.most_common()), items=items[:400], covered=covered, gap=torch_gap)
