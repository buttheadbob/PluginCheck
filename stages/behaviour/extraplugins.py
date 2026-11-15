"""Other plugins loaded beside the one under test, for a target that names them, and the build variants that load them.

A target names plugins under behaviour.plugins, by a label:

    "plugins": {"Performance": {
        "magnetar": {"repo": "owner/name", "commit": "<sha>", "manifest": "PluginServer.xml",
                     "configFile": "Performance.cfg", "configRoot": "PerformanceConfig"},
        "torch": {"repo": "owner/name", "ref": "<tag or sha>", "folder": "TorchPlugin",
                  "configFile": "PerformanceImprovements.cfg", "configRoot": "PluginConfig"}}}

and build variants under behaviour.variants. Each makes one more build from each working build on its hosts:

    "variants": [{"name": "perf-counter", "hosts": ["magnetar"], "with": ["Performance"],
                  "config": {"Performance": {"FixScriptCounter": true}}, "scenarios": ["getreport [settle 170"]},
                 {"name": "before-skip", "hosts": ["magnetar", "legacy"], "overlay": "<folder of files laid over the working source>"}]

Both are built from source, as Magnetar and Torch would: Magnetar compiles the manifest's source directories, and a Torch plugin
is compiled here against Torch's and the game's own assemblies. Nothing is downloaded from torchapi.com. A plugin's config is
written before the server starts, so a setting its preloader reads at start, such as Performance's FixScriptCounter, takes
effect with no restart. "scenarios" limits a variant to the scenarios whose names contain one of those texts, or are exactly the text after a leading "=". "files" maps a path
under the server's seed folder (such as "Instance/Advanced_PB_LimiterConfig.cfg") to text written there before start, such as an
older config file of the plugin under test.
"""
import io, os, re, shutil, urllib.request, zipfile
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr
import pin


def fetch(repo, ref, cache):
    """The repository's files at a commit or tag, kept in the cache."""
    folder = os.path.join(cache, f'{repo.replace("/", "-")}-{ref[:12]}')
    if not os.path.isdir(folder):
        print(f'downloading {repo} at {ref} from GitHub', flush=True)
        archive = zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(f'https://codeload.github.com/{repo}/zip/{ref}', timeout=300).read()))
        for info in archive.infolist():
            relative = info.filename.split('/', 1)[-1]
            path = os.path.join(folder, relative)
            # A member such as C:/x or /x makes the join absolute: only paths that stay in the folder are written.
            if relative and not info.is_dir() and '..' not in relative and inside(folder, path):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                open(path, 'wb').write(archive.read(info))
    return folder


def inside(folder, path):
    folder = os.path.abspath(folder)
    return os.path.commonpath([folder, os.path.abspath(path)]) == folder and os.path.abspath(path) != folder


def config_xml(root, values):
    """A config file holding only the values set, which PluginSdk's ConfigStorage and Torch's XmlSerializer both read."""
    def text(value):
        return str(value).lower() if isinstance(value, bool) else str(value)
    body = ''.join(f'  <{name}>{escape(text(value))}</{name}>\n' for name, value in values.items())
    return f'<?xml version="1.0" encoding="utf-8"?>\n<{root}>\n{body}</{root}>\n'


def magnetar_plugin(spec, cache):
    """(source folder, manifest file name, plugin id, label) for a dev folder entry."""
    folder = fetch(spec['repo'], spec['commit'], cache)
    root = ET.parse(os.path.join(folder, spec['manifest'])).getroot()
    return folder, spec['manifest'], root.findtext('Id').strip(), root.findtext('FriendlyName').strip()


XAML_ROOTS = {'UserControl': 'System.Windows.Controls.UserControl', 'Window': 'System.Windows.Window', 'Page': 'System.Windows.Controls.Page'}


def torch_project(source, folder, out, torch_libs=None):
    """An SDK project for a Torch plugin's folder, compiled on Linux: its own .cs files and any shared project they import,
    its references mapped to Torch's (/work/torch) and the game's (/ds/DedicatedServer64) assemblies, and a stand-in for each
    XAML page, which a server with no UI never opens. With torch_libs, a reference goes to Torch's copy when Torch has that file
    and to the game's otherwise, whatever folder its HintPath names."""
    project = next(f for f in os.listdir(os.path.join(source, folder)) if f.endswith('.csproj'))
    root = ET.parse(os.path.join(source, folder, project)).getroot()
    tag = lambda e: e.tag.split('}')[-1]
    files, references = [], []
    for d, _, fs in os.walk(os.path.join(source, folder)):
        if os.sep + 'obj' not in d and os.sep + 'bin' not in d:
            files += [os.path.join(d, f) for f in fs if f.endswith('.cs')]
    for e in root.iter():
        if tag(e) == 'Import' and e.get('Project', '').endswith('.projitems'):
            items = os.path.normpath(os.path.join(source, folder, e.get('Project').replace('\\', '/')))
            base = os.path.dirname(items)
            for c in ET.parse(items).getroot().iter():
                include = c.get('Include', '')
                if tag(c) == 'Compile' and include.endswith('.cs'):
                    files.append(os.path.normpath(os.path.join(base, include.replace('$(MSBuildThisFileDirectory)', '').replace('\\', '/'))))
        elif tag(e) == 'PackageReference' and e.get('Include'):
            # Shipped in the zip, as the plugin's release does: Torch provides its own assemblies and the game's, not these.
            version = e.get('Version') or next((c.text for c in e if tag(c) == 'Version'), '')
            references.append(f'    <PackageReference Include={quoteattr(e.get("Include"))} Version={quoteattr(version or "*")} />')
        elif tag(e) == 'Reference':
            name = e.get('Include').split(',')[0]
            hint = next((c.text for c in e if tag(c) == 'HintPath'), None)
            if hint:
                parts = hint.replace('\\', '/').split('/')
                if torch_libs:
                    where = '/work/torch' if os.path.exists(os.path.join(torch_libs, parts[-1])) else '/ds/DedicatedServer64'
                else:
                    where = '/ds/DedicatedServer64' if 'DedicatedServer64' in parts else '/work/torch'
                references.append(f'    <Reference Include={quoteattr(name)}><HintPath>{where}/{parts[-1]}</HintPath><Private>false</Private></Reference>')
            else:
                references.append(f'    <Reference Include={quoteattr(name)} />')
    stubs = []
    for d, _, fs in os.walk(os.path.join(source, folder)):
        for f in fs:
            if f.endswith('.xaml'):
                text = open(os.path.join(d, f), encoding='utf-8-sig', errors='replace').read()
                match = re.search(r'x:Class\s*=\s*"([\w.]+)"', text)
                if match:
                    ns, cls = match.group(1).rsplit('.', 1)
                    named = dict((n, t) for t, n in re.findall(r'<(\w+)\b[^<>]*?\s(?:x:)?Name\s*=\s*"(\w+)"', text))
                    fields = ''.join(f'internal System.Windows.Controls.{t} {n} = null; ' for n, t in sorted(named.items()) if t != 'UserControl')
                    # The base type is the XAML's root element, which code-behind often leaves out because the XAML compiler supplies it.
                    top = re.search(r'<(?![?!])([\w:]+)', text)
                    base = XAML_ROOTS.get(top.group(1)) if top else None
                    stubs.append(f'namespace {ns} {{ partial class {cls}{" : " + base if base else ""} {{ {fields}private void InitializeComponent() {{ }} }} }}')
    os.makedirs(out, exist_ok=True)
    open(os.path.join(out, 'Pages.g.cs'), 'w', encoding='utf-8').write('\n'.join(stubs))
    assembly = (root.findtext('.//{*}AssemblyName') or root.findtext('.//AssemblyName') or os.path.splitext(project)[0]).strip()
    compile_items = '\n'.join(f'    <Compile Include={quoteattr("/work/source/" + os.path.relpath(f, source).replace(os.sep, "/"))} />' for f in sorted(set(files)))
    open(os.path.join(out, 'Extra.csproj'), 'w', encoding='utf-8').write(f'''<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net48</TargetFramework>
    <PlatformTarget>x64</PlatformTarget>
    <LangVersion>latest</LangVersion>
    <EnableDefaultCompileItems>false</EnableDefaultCompileItems>
    <GenerateAssemblyInfo>false</GenerateAssemblyInfo>
    <AllowUnsafeBlocks>true</AllowUnsafeBlocks>
    <AssemblyName>{escape(assembly)}</AssemblyName>
    <NoWarn>$(NoWarn);NU1701;MSB3277;MSB3243;CS0618</NoWarn>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Microsoft.NETFramework.ReferenceAssemblies.net48" Version="1.0.3" PrivateAssets="all" />
{compile_items}
    <Compile Include="Pages.g.cs" />
{chr(10).join(references)}
  </ItemGroup>
</Project>
''')
    return assembly


def torch_plugin(spec, cache, work, docker, sdk, torch_libs, ds_volume):
    """(zip path, guid, name) of a Torch plugin built from source, or raises with the build's own error."""
    source = fetch(spec['repo'], spec['ref'], cache)
    name = ET.parse(os.path.join(source, spec['folder'], 'manifest.xml')).getroot().findtext('Name').strip()
    return torch_zip(source, spec['folder'], os.path.join(work, f'extra-{name}'), docker, sdk, torch_libs, ds_volume)


def torch_zip(source, folder, out, docker, sdk, torch_libs, ds_volume):
    """(zip path, guid, name) of the Torch plugin in source/folder, built on Linux with its own manifest.xml, or raises with the build's
    own error. Kept in out, and built once."""
    manifest = os.path.join(source, folder, 'manifest.xml')
    root = ET.parse(manifest).getroot()
    guid, name = root.findtext('Guid').strip(), root.findtext('Name').strip()
    zip_path = os.path.join(out, f'{name}.zip')
    if os.path.exists(zip_path):
        return zip_path, guid, name
    build = os.path.join(out, 'build')
    assembly = torch_project(source, folder, build, torch_libs)
    made = docker('run', *pin.args(), '--rm', '--cpus', '4', '--memory', '6g', '-v', f'{source}:/work/source:ro', '-v', f'{build}:/work/build', '-v', f'{torch_libs}:/work/torch:ro',
                  '-v', f'{ds_volume}:/ds:ro', '--entrypoint', 'bash', sdk, '-c', 'cd /work/build; dotnet build -c Release -v q -nologo -o /work/build/out > /work/build/build.log 2>&1')
    dll = os.path.join(build, 'out', assembly + '.dll')
    if made.returncode or not os.path.exists(dll):
        log = open(os.path.join(build, 'build.log'), encoding='utf-8', errors='replace').read()
        raise RuntimeError(f'{name} did not build: ' + '\n'.join(l for l in log.splitlines() if ' error ' in l)[:600])
    with zipfile.ZipFile(zip_path, 'w') as z:
        # The plugin and the packages it takes, such as its own Harmony. Torch's and the game's assemblies stay out (Private=false).
        for name_in_out in sorted(os.listdir(os.path.join(build, 'out'))):
            if name_in_out.endswith('.dll'):
                z.write(os.path.join(build, 'out', name_in_out), name_in_out)
        z.write(manifest, 'manifest.xml')
    return zip_path, guid, name


def variant_builds(builds, options, work, overlay_copy):
    """One more build per variant from each working build on its hosts."""
    made = []
    for variant in options.get('variants', []):
        for base in builds:
            if base['published'] or base['host'] not in variant.get('hosts', ['magnetar', 'legacy', 'torch']):
                continue
            build = {**base, 'name': f"{base['name']}~{variant['name']}", 'variant': variant['name'], 'with': variant.get('with', []),
                     'config': variant.get('config', {}), 'files': variant.get('files', {}), 'only': variant.get('scenarios')}
            if variant.get('overlay'):
                if base['host'] == 'torch':
                    # A Torch build is a zip, so its zip is built from the overlaid source once the Torch libraries are in place.
                    build['overlay'] = variant['overlay']
                elif 'source' not in base:
                    continue
                else:
                    build['source'] = overlay_copy(base, variant['overlay'], build['name'])
            made.append(build)
    return made
