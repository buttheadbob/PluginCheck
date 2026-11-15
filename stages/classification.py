"""The AI operator's reading of a plugin's commands, which decides what can share a server.

What a command touches is a judgement about its code, so the AI that runs PluginCheck reads every handler and records it in
cache/behaviour/commands-<target>.json. No person writes this file and it is not kept in git. It holds the hash of the source it came from,
so changed code needs reading again. With no current classification, every command gets its own server, which is slower and always safe.

  {"source": "<hash>", "commands": {
    "!as about":    {"reads": [], "writes": [], "settings": [], "why": "AutoStationCommands.cs:13 returns fixed text"},
    "!as forcerun": {"reads": ["grids", "players"], "writes": ["grids"], "settings": ["Enable"], "why": "AutoBOT.cs:72 converts grids"}
  }}

settings lists every on/off setting the command's code can reach, so only those get flipped. Leave it out to flip all of them.

examples lists real ways to run a command, because a placeholder argument only reaches its first check. Each is a line, or
{"line": "...", "caller": "player" or "admin", "online": true}. A caller runs it as an online player of that rank instead of the console. online
adds the online players without changing who runs it. The fixture's names are the only ones that exist: FixturePlayer, FixtureRecent, the grids
in fixture.py, and, once a scenario has a caller or online, ProbeCaller and ProbeTarget, who are online with characters. With examples, only they run.

A line can also say settle (seconds after the world is ready before it runs), min (seconds to keep watching afterwards), settings (values for this line
only, of any type), flip false (do not repeat this line with each on/off setting) and node true (run on Magnetar as a cluster node with a shared folder).
Lines joined with " && " run one after the other on one server, so one command can set up the next. The probe has lines of its own for such a chain:
probe:wait <seconds>, probe:sit <cockpit name> (seats the caller), probe:off <block name>, probe:file <path under the user data folder> (its text), probe:storage <programmable block name> (the Storage the block holds), probe:recompile <block name>, probe:save, probe:empty (empties every block named watch-*), probe:fill <block> <ore> <amount>,
probe:sharedwrite <path under the plugin's shared folder> <text>, probe:sharedread <path> and probe:sharedremove <path>. The last three stand in for another node.

  "!economy give": {"reads": ["economy"], "writes": ["economy"], "settings": [], "why": "...", "examples": ["!economy give FixturePlayer 100"]},
  "!economy top":  {"reads": ["economy"], "writes": [], "settings": ["PublicBalances"], "why": "...", "examples": [
    "!economy top", {"line": "!economy top", "caller": "player"}, {"line": "!economy top", "caller": "admin"}]}

Changing the world is not a reason to run alone. Commands share a server unless they overlap: one writes something another reads or writes.
reads and writes name what the command depends on or changes, in any useful words: grids, players, factions, economy, weather, voxels,
plugin-config, plugin-files. The names only have to match between commands, so use the same word for the same thing. "*" means it could
reach anything, such as a command that restarts the server, so it always gets its own.
"""
import hashlib, json, os
import repocopy

HERE = os.path.dirname(os.path.abspath(__file__))
SKIP = {'.claude', 'bin', 'obj', '.git', '.vs', 'GameBinaries', 'TorchBinaries', 'Build', 'node_modules'}
EVERYTHING = {'*'}


def path_for(name):
    return os.path.normpath(os.path.join(HERE, '..', 'cache', 'behaviour', f'commands-{name}.json'))


def source_hash(repo):
    """Of every C# file, so any change to the code asks for a fresh reading."""
    digest = hashlib.sha256()
    for folder, subfolders, files in repocopy.walk(repo):
        # Pruned here, as the walk goes. Sorting the walk would read every folder first, game binaries included.
        subfolders[:] = sorted(s for s in subfolders if s not in SKIP)
        for name in sorted(files):
            if name.endswith('.cs'):
                digest.update(name.encode())
                digest.update(open(os.path.join(folder, name), 'rb').read())
    return digest.hexdigest()[:16]


def load(name, repo):
    """What each command touches, as {line: (reads, writes)}, or None when there is no reading for this source."""
    try:
        data = json.load(open(path_for(name), encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if data.get('source') != source_hash(repo):
        return None
    return {line.lower(): (set(entry.get('reads', [])), set(entry.get('writes', ['*']))) for line, entry in data.get('commands', {}).items()}


def settings_read(name, repo):
    """The settings each command depends on, as {line: names}. A command missing here is flipped against every setting."""
    try:
        data = json.load(open(path_for(name), encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if data.get('source') != source_hash(repo):
        return {}
    return {line.lower(): set(entry['settings']) for line, entry in data.get('commands', {}).items() if 'settings' in entry}


def examples(name, repo):
    """The real lines each command was given, as {command: [{line, caller, online}]}. Empty when there is no reading for this source."""
    try:
        data = json.load(open(path_for(name), encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if data.get('source') != source_hash(repo):
        return {}
    found = {}
    for command, entry in data.get('commands', {}).items():
        lines = [{'line': e} if isinstance(e, str) else e for e in entry.get('examples', [])]
        if lines:
            found[command.lower()] = lines
    return found


def best_match(command, known):
    """The longest known command a line starts with, since arguments do not change what it is."""
    line = (command or '').lower()
    matches = [k for k in known if line == k or line.startswith(k + ' ')]
    return max(matches, key=len) if matches else None


def folder_hash(folder):
    """Of every file in a folder, for anything that decides a result: a plugin's project files as well as its code."""
    digest = hashlib.sha256()
    for root, subfolders, files in repocopy.walk(folder):
        subfolders[:] = sorted(s for s in subfolders if s not in SKIP)
        for name in sorted(files):
            digest.update(os.path.relpath(os.path.join(root, name), folder).replace(os.sep, '/').encode())
            digest.update(open(os.path.join(root, name), 'rb').read())
    return digest.hexdigest()[:16]


def footprint(command, known):
    """What a command line touches. Arguments do not change it, so the longest known command it starts with decides. Unknown means anything."""
    match = best_match(command, known)
    return known[match] if match else (set(), EVERYTHING)


def overlap(first, second):
    """Whether one writes something the other reads or writes."""
    first_reads, first_writes = first
    second_reads, second_writes = second
    if '*' in first_writes or '*' in second_writes:
        return True
    return bool(first_writes & (second_reads | second_writes) or second_writes & first_reads)


def batches(plans, known):
    """Which scenarios share a server. Settings differ between servers, so only scenarios with the same settings can share one.

    Commands that write go first, so the first in each server starts on a clean world, and a scenario joins the first server it does not
    overlap with. Scenarios with no command wait out a timer, so each keeps a server to itself.
    """
    groups = {}
    for plan in plans:
        # Online players change what every command in a server sees, so scenarios with them do not share with those without.
        groups.setdefault((tuple(sorted(plan.get('settings', {}).items())), bool(plan.get('players')), plan.get('settle'), plan.get('node')), []).append(plan)
    out = []
    for group in groups.values():
        # So does a scenario that removes things, because that would reach anything sharing its server.
        out += [[p] for p in group if not p.get('command') or p.get('removals')]
        commands = [p for p in group if p.get('command') and not p.get('removals')]
        commands.sort(key=lambda p: not footprint(p['command'], known)[1])
        shared = []
        for plan in commands:
            mine = footprint(plan['command'], known)
            home = next((b for b in shared if not any(overlap(mine, footprint(p['command'], known)) for p in b)), None)
            if home is None:
                shared.append([plan])
            else:
                home.append(plan)
        out += shared
    return out
