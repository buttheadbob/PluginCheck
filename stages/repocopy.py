"""What a stage leaves out when it copies a plugin's repository to build it."""
import os, shutil

ALWAYS = ('.claude', 'bin', 'obj', '.git', '.vs', 'Build', 'GameBinaries', 'TorchBinaries', 'node_modules', '*.user')


def ignore(repo):
    """Directory.Build.props is left out only where the repository ignores it, as a machine's own copy of a template with its
    local paths. A committed one holds real build settings, such as ImplicitUsings, and the build fails without it."""
    try:
        with open(os.path.join(repo, '.gitignore'), encoding='utf-8', errors='replace') as listed:
            ignored = {line.strip().lstrip('/') for line in listed}
    except OSError:
        ignored = set()
    return shutil.ignore_patterns(*ALWAYS, *(['Directory.Build.props'] if 'Directory.Build.props' in ignored else []))
