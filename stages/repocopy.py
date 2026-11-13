"""What a stage leaves out when it copies a plugin's repository to build it."""
import os, shutil, stat

ALWAYS = ('.claude', 'bin', 'obj', '.git', '.vs', 'Build', 'GameBinaries', 'TorchBinaries', 'node_modules', '*.user')


def linked(path):
    """A symlink, or on Windows a junction. Either points outside the repository, such as at a game or Torch install, and copying or
    walking it would take in that install's files, logs included. os.walk and shutil.copytree both follow junctions. A junction is
    told by its reparse tag, as os.path.isjunction does from Python 3.12."""
    if os.path.islink(path):
        return True
    try:
        return os.lstat(path).st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
    except (OSError, AttributeError):
        return False


def unlinked(patterns):
    """An ignore for shutil.copytree that also leaves out linked folders and files, whatever their name."""
    def ignore(folder, names):
        return set(patterns(folder, names)) | {n for n in names if linked(os.path.join(folder, n))}
    return ignore


def walk(top):
    """os.walk that does not go into linked folders. A caller's own pruning of the folder list still works."""
    for folder, subfolders, files in os.walk(top):
        subfolders[:] = [s for s in subfolders if not linked(os.path.join(folder, s))]
        yield folder, subfolders, files


def ignore(repo):
    """Directory.Build.props is left out only where the repository ignores it, as a machine's own copy of a template with its
    local paths. A committed one holds real build settings, such as ImplicitUsings, and the build fails without it."""
    try:
        with open(os.path.join(repo, '.gitignore'), encoding='utf-8', errors='replace') as listed:
            ignored = {line.strip().lstrip('/') for line in listed}
    except OSError:
        ignored = set()
    return unlinked(shutil.ignore_patterns(*ALWAYS, *(['Directory.Build.props'] if 'Directory.Build.props' in ignored else [])))


def companion_folders(repo, target):
    """The source of a Quasar UI plugin's owned companions, the Magnetar plugins Quasar builds with it, which have no hub manifest
    of their own. Their project folders, and any folder a project compiles in from elsewhere, such as shared contracts. Only folders
    inside the repository count, and a project that cannot be read adds only its own folder."""
    import json
    import xml.etree.ElementTree as ET
    manifest = target.get('quasar', {}).get('packageManifest')
    if not manifest:
        return []
    try:
        package = json.load(open(os.path.join(repo, manifest), encoding='utf-8'))
    except (OSError, ValueError):
        return []
    folders = []
    for companion in package.get('companionPlugins', []):
        project = companion.get('projectPath') if isinstance(companion, dict) else None
        if not project or not inside(project) or not os.path.isfile(os.path.join(repo, project)):
            continue
        folder = os.path.dirname(project) or '.'
        folders.append(folder)
        try:
            items = list(ET.parse(os.path.join(repo, project)).getroot().iter())
        except ET.ParseError:
            continue
        for item in items:
            include = item.get('Include') or ''
            if item.tag.endswith('Compile') and '..' in include:
                linked = os.path.relpath(os.path.normpath(os.path.join(repo, folder, include.replace('\\', '/').split('*')[0])), repo).replace(os.sep, '/')
                if inside(linked) and os.path.isdir(os.path.join(repo, linked)) and linked not in folders:
                    folders.append(linked)
    return folders


def inside(path):
    """Whether a path a plugin's files name stays inside its repository. The stages copy and mount these folders, so one that
    leads elsewhere on this machine is left out."""
    path = os.path.normpath(path.replace('\\', '/'))
    return not os.path.isabs(path) and not os.path.splitdrive(path)[0] and path != os.pardir and not path.startswith(os.pardir + os.sep)


# What the .NET SDK adds to every file of a project built with ImplicitUsings.
SDK_USINGS = ('System', 'System.Collections.Generic', 'System.IO', 'System.Linq', 'System.Net.Http', 'System.Threading', 'System.Threading.Tasks')


def implicit_usings(repo, folders):
    """True when a project in these folders builds with ImplicitUsings, set in its csproj or the repository's Directory.Build.props.
    The code stages compile source files without the project, so they must add those usings themselves."""
    import glob, re

    def setting(path):
        try:
            found = re.search(r'<ImplicitUsings>\s*(\w+)\s*</ImplicitUsings>', open(path, encoding='utf-8', errors='replace').read())
        except OSError:
            return None
        return None if found is None else found.group(1).lower() in ('enable', 'true')

    default = setting(os.path.join(repo, 'Directory.Build.props'))
    for folder in folders:
        for project in glob.glob(os.path.join(repo, folder, '*.csproj')):
            own = setting(project)
            if own if own is not None else default:
                return True
    return False


def add_implicit_usings(repo, folders, source):
    """Writes the SDK's implicit usings into the copied source when the plugin's projects use them."""
    if implicit_usings(repo, folders):
        with open(os.path.join(source, 'PluginCheck.ImplicitUsings.cs'), 'w', encoding='utf-8', newline='\n') as out:
            out.write(''.join(f'global using {name};\n' for name in SDK_USINGS))
