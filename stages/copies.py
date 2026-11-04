"""Compares a plugin's code with every published plugin whose source can be reached, in both directions.

usage: copies.py <target.json> <run dir> [results.json]

A renamed copy with a new description reads as an original, but its code does not change, and a copy of a copy looks the same
as a direct one. So this compares code, and ignores names, descriptions and fork links.

  shared    the same line, wherever it sits
  renamed   the same line with its names and literal text masked out, so renaming everything does not hide it
  commands  the same command paths, with any added root such as !ess taken off
  messages  the same user-facing sentences, word for word, with their placeholders ignored
  calls     the same runs of method calls, in order, whatever the syntax around them

A rewrite in newer C# changes every line, with pattern matching, switch expressions and the like, but it must still call the same
game methods in much the same order, so `calls` survives what lines do not.

Lines alone miss a rewrite: two plugins can share almost no lines and still have the same commands and messages. Nobody writes
the same sentence twice by chance, so messages are the strongest sign. Only rare lines and messages count: one found in several
plugins is a template or game boilerplate, and proves nothing.

Credit is checked in both directions, in a readme, license, notice or changelog, and names the other project exactly. Credit
puts a match in NOTICE however large it is, unless the original is clearly marked as not allowing reuse.

The original is the older project. A newer project's no-reuse license says nothing about code an older project wrote first,
so it never raises a match. A missing license is not a clear marking. The age of a repository is evidence, not proof: code
can predate its repository.

  NOTICE   a small overlap, or a credited one
  WARN     a real overlap with no credit, or credited but the original forbids reuse
  EXTREME  mostly the same code with no credit, or with credit but the original forbids reuse

This stage finds candidates, and an AI judges them, following REVIEW.md, so it always ends as `review`. The tiers say what to
read first. Similar code is not proof of wrongdoing, and low numbers are not proof of originality: a thorough rewrite passes
a line comparison. A person must read the matches. Sources are cached in cache/sources: hub plugins by their pinned
commit, which never changes, and others for a day. Pass --refresh to check.py to download again.
"""
import collections, concurrent.futures, hashlib, io, json, os, re, shutil, sys, time, urllib.request, zipfile
import xml.etree.ElementTree as ET

HUBS = {'MagnetarHub': 'https://codeload.github.com/CometWorks/magnetar-hub/zip/refs/heads/main',
        'QuasarHub': 'https://codeload.github.com/CometWorks/quasar-hub/zip/refs/heads/main'}
TORCH = 'https://torchapi.com/api/plugins'
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, 'cache', 'sources')
MAX_AGE = 24 * 3600
MAX_DOWNLOAD = 60 * 1024 * 1024
SKIP = {'.git', 'bin', 'obj', 'packages', 'GameBinaries', 'TorchBinaries', 'Build', '.vs', 'node_modules', 'Tests'}
EXTENSIONS = ('.cs', '.razor', '.xaml')
LICENSES = ('LICENSE', 'LICENSE.md', 'LICENSE.txt', 'COPYING', 'NOTICE')
CREDIT_FILES = LICENSES + ('README.md', 'README.txt', 'README', 'CHANGELOG.md', 'CREDITS.md')
NOT_REPOS = {'TorchAPI/Torch', 'keenswh/SpaceEngineers', 'KeenSoftwareHouse/SpaceEngineers'}
COMMON_IN = 3      # A line in this many other plugins is boilerplate.
MIN_LINES = 15     # Below this many shared rare lines, a percentage means nothing.
WARN_AT, EXTREME_AT = 0.15, 0.60
MIN_MESSAGES = 10  # Fewer shared messages than this could be chance.
MIN_COMMANDS = 15  # Short command names such as tp or kick are common.
SAME_MESSAGES_WARN, SAME_MESSAGES_EXTREME = 0.10, 0.40
SAME_COMMANDS_WARN, SAME_COMMANDS_EXTREME = 0.30, 0.60
CALL_RUN = 6       # Calls in a row that make one fingerprint.
MIN_CALLS = 25
SAME_CALLS_WARN, SAME_CALLS_EXTREME = 0.15, 0.50
NOT_CALLS = set('''if for foreach while switch catch using lock return nameof typeof sizeof default when new await throw base this fixed
Log Info Debug Warn Warning Error Respond ToString Add Remove Contains Count Any First FirstOrDefault Where Select ToList ToArray Format
Equals Join Trim Split Substring Replace StartsWith EndsWith IsNullOrEmpty IsNullOrWhiteSpace TryGetValue ContainsKey Clear Length Max Min
AppendLine Append Dispose Invoke GetType'''.split())
# Clear statements that reuse is not allowed. Silence is not one of them.
NO_REUSE = re.compile(r'all rights reserved|not free to use|proprietary|no (re-?use|redistribution)|may not (be )?(copy|copied|redistribut|re-?use|modif)|'
                      r'do not (copy|redistribute|re-?use)|without (the )?(prior )?(written )?permission|non-?commercial.{0,40}no-?deriv|cc[- ]by[- ]nc[- ]nd', re.I)
PERMISSIVE = ('MIT License', 'Apache License', 'GNU GENERAL', 'GNU AFFERO', 'GNU LESSER', 'public domain', 'Redistribution and use')

KEYWORDS = set('''if else for foreach while do return var new public private protected internal static void bool int long double
float string class struct enum namespace try catch finally throw null true false this base in out ref is as continue break
switch case default readonly const override virtual abstract sealed async await params typeof'''.split())
TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[A-Za-z_]\w*|\d+\.?\d*|\S')

target = json.load(open(sys.argv[1], encoding='utf-8'))
run_dir = os.path.abspath(sys.argv[2])
result_path = sys.argv[3] if len(sys.argv) > 3 else None
refresh = os.environ.get('PLUGINCHECK_REFRESH') == '1'
repo = target['repo']


def get(url, limit=None):
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'PluginCheck'}), timeout=60) as r:
        data = r.read(limit + 1 if limit else -1)
    if limit and len(data) > limit:
        raise ValueError('too large')
    return data


def key_for(name, ref):
    safe = re.sub(r'[^\w.-]+', '-', name)
    return f'{safe}-{hashlib.sha1(ref.encode()).hexdigest()[:10]}'


def fetch(item):
    """Keeps only source and credit files, so the cache stays small and nothing in it can be run by accident."""
    folder = os.path.join(CACHE, key_for(item['repo'], item['ref']))
    stamp = os.path.join(folder, '.fetched')
    if os.path.exists(stamp) and not refresh and (item['pinned'] or time.time() - os.path.getmtime(stamp) < MAX_AGE):
        item['folder'] = folder
        return item
    try:
        archive = zipfile.ZipFile(io.BytesIO(get(f"https://codeload.github.com/{item['repo']}/zip/{item['ref']}", MAX_DOWNLOAD)))
    except Exception as e:
        item['error'] = str(e)[:90]
        return item
    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(folder)
    for info in archive.infolist():
        parts = info.filename.split('/')[1:]
        name = parts[-1] if parts else ''
        if info.is_dir() or not name or any(p in SKIP for p in parts) or '..' in parts:
            continue
        if name.endswith(EXTENSIONS) or name in CREDIT_FILES:
            out = os.path.join(folder, *parts)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, 'wb') as f:
                f.write(archive.read(info))
    open(stamp, 'w').write(item['ref'])
    item['folder'] = folder
    return item


def lines_of(folder):
    """Maps each normalized rare-looking line to the file it came from, plain and with names masked."""
    plain, masked = {}, {}
    for d, ds, fs in os.walk(folder):
        ds[:] = [x for x in ds if x not in SKIP]
        for f in fs:
            if not f.endswith(EXTENSIONS):
                continue
            path = os.path.join(d, f)
            rel = os.path.relpath(path, folder).replace(os.sep, '/')
            for raw in open(path, encoding='utf-8', errors='replace'):
                line = raw.strip()
                if len(line) < 24 or line.startswith(('//', '///', '*', '/*', 'using ', '#', '[', '<')):
                    continue
                tokens = TOKEN.findall(line)
                if len(tokens) < 6:
                    continue
                plain.setdefault(' '.join(tokens), rel)
                if len(tokens) >= 12:
                    mask = ['L' if t[0] == '"' or t[0].isdigit() else t if t in KEYWORDS or not (t[0].isalpha() or t[0] == '_') else 'I' for t in tokens]
                    masked.setdefault(' '.join(mask), rel)
    return plain, masked


def behaviour_of(folder):
    """The command paths and user-facing sentences a project contains."""
    commands, messages = set(), set()
    for d, ds, fs in os.walk(folder):
        ds[:] = [x for x in ds if x not in SKIP]
        for f in fs:
            if not f.endswith('.cs'):
                continue
            text = open(os.path.join(d, f), encoding='utf-8', errors='replace').read()
            prefix = (re.findall(r'\[Category\(\s*"([\w ]+)"', text) or re.findall(r'\[CommandRoot\(\s*"(\w+)"', text) or [''])[0]
            for name in re.findall(r'\[Command\(\s*"([^"]*)"', text):
                path = ' '.join((prefix + ' ' + name).lower().split())
                if path:
                    commands.add(path)
            for s in re.findall(r'\$?"((?:[^"\\\n]|\\.){18,})"', text):
                if s.count(' ') >= 2 and not s.startswith(('http', '/', '[')):
                    messages.add(re.sub(r'\{[^}]*\}', '{}', s).lower())
    return commands, messages


def calls_of(folder):
    """Runs of method calls in source order, with comments and text removed, so syntax and formatting do not matter."""
    runs = set()
    for d, ds, fs in os.walk(folder):
        ds[:] = [x for x in ds if x not in SKIP]
        for f in fs:
            if not f.endswith('.cs'):
                continue
            text = open(os.path.join(d, f), encoding='utf-8', errors='replace').read()
            text = re.sub(r'//[^\n]*|/\*.*?\*/', ' ', text, flags=re.S)
            text = re.sub(r'\$?@?"(?:[^"\\\n]|\\.)*"', '""', text)
            calls = [c for c in re.findall(r'\b([A-Za-z_]\w*)\s*(?:<[\w<>,.\s\[\]?]*>)?\(', text) if c not in NOT_CALLS]
            for i in range(len(calls) - CALL_RUN + 1):
                runs.add(tuple(calls[i:i + CALL_RUN]))
    return runs


def same_commands(mine, theirs):
    """Compares command paths, also with an added root taken off either side."""
    strip = lambda paths: {p.split(' ', 1)[1] if ' ' in p else p for p in paths}
    return max(len(mine & theirs), len(strip(mine) & theirs), len(mine & strip(theirs)))


def needles_for(repo_name, plugin, own_name):
    """Names precise enough to count as credit. A common word in both names, such as Essentials, must not."""
    names = [repo_name, 'github.com/' + repo_name]
    if plugin and ' ' in plugin.strip() and plugin.lower() not in own_name.lower().replace('sens', ''):
        names.append(plugin)
    return names


def forbids_reuse(folder):
    for name in LICENSES:
        path = os.path.join(folder, name)
        if os.path.exists(path):
            text = open(path, encoding='utf-8', errors='replace').read()
            if not any(p.lower() in text[:900].lower() for p in PERMISSIVE) and NO_REUSE.search(text):
                return True
    return False


def created(repo_name):
    """When a repository was created, or None. Uses GitHub's public API, which allows 60 requests an hour."""
    try:
        return json.loads(get(f'https://api.github.com/repos/{repo_name}')).get('created_at')
    except Exception:
        return None


def credits(folder, needles):
    """Whether any of these names appears in the project's own readme, license or changelog."""
    for d, ds, fs in os.walk(folder):
        ds[:] = [x for x in ds if x not in SKIP]
        for f in fs:
            if f in CREDIT_FILES:
                text = open(os.path.join(d, f), encoding='utf-8', errors='replace').read().lower()
                if any(n.lower() in text for n in needles):
                    return True
    return False


def license_of(folder):
    for name in LICENSES:
        path = os.path.join(folder, name)
        if os.path.exists(path):
            head = open(path, encoding='utf-8', errors='replace').read(600)
            for label, mark in (('MIT', 'MIT License'), ('Apache-2.0', 'Apache License'), ('GPL', 'GNU GENERAL'), ('AGPL', 'GNU AFFERO'), ('LGPL', 'GNU LESSER'),
                                ('Unlicense', 'public domain'), ('BSD', 'Redistribution and use')):
                if mark.lower() in head.lower():
                    return label
            return 'other'
    return 'none found'


# Everything with reachable source.
own_repos, own_ids = set(), set()
for section in ('magnetar', 'quasar'):
    manifest = target.get(section, {}).get('manifest')
    if manifest:
        root = ET.parse(os.path.join(repo, manifest)).getroot()
        own_repos.add((root.findtext('RepoId') or '').strip().lower())
        own_ids.add((root.findtext('Id') or '').strip().lower())
if target.get('torch', {}).get('guid'):
    own_ids.add(target['torch']['guid'].lower())

wanted, skipped = {}, []
for hub, url in HUBS.items():
    archive = zipfile.ZipFile(io.BytesIO(get(url)))
    for item in archive.namelist():
        if '/Plugins/' not in item or not item.endswith('.xml'):
            continue
        try:
            root = ET.fromstring(archive.read(item))
        except ET.ParseError:
            continue
        name, commit = (root.findtext('RepoId') or '').strip(), (root.findtext('Commit') or '').strip()
        if name.lower() in own_repos or (root.findtext('Id') or '').strip().lower() in own_ids:
            continue
        if name and commit:
            wanted.setdefault(name.lower(), {'repo': name, 'ref': commit, 'pinned': True, 'plugin': (root.findtext('FriendlyName') or name).strip(),
                                             'author': (root.findtext('Author') or '').strip(), 'source': hub})

listing = json.loads(get(TORCH))['plugins']
for p in listing:
    if (p.get('guid') or '').lower() in own_ids:
        continue
    text = json.dumps({k: p.get(k) for k in ('description', 'short_description')}).replace('\\/', '/')
    links = [f'{a}/{b.removesuffix(".git")}' for a, b in re.findall(r'github\.com/([\w.-]+)/([\w.-]+)', text)]
    links = [l for l in links if l not in NOT_REPOS and l.lower() not in own_repos]
    if links:
        wanted.setdefault(links[0].lower(), {'repo': links[0], 'ref': 'HEAD', 'pinned': False, 'plugin': p.get('name'), 'author': p.get('author'), 'source': 'torchapi.com'})
    else:
        skipped.append(p.get('name'))

upstream = target.get('upstreamRepo')
if not upstream and 'upstream' in target:
    match = re.search(r'github\.com/([\w.-]+/[\w.-]+)/', target['upstream'])
    upstream = match.group(1) if match else None
if upstream:
    # A declared upstream is always compared. Its listing may not link to its source at all.
    entry = wanted.setdefault(upstream.lower(), {'repo': upstream, 'ref': 'HEAD', 'pinned': False, 'plugin': upstream.split('/')[-1], 'author': upstream.split('/')[0], 'source': 'declared upstream'})
    entry['upstream'] = True

os.makedirs(CACHE, exist_ok=True)
start = time.time()
with concurrent.futures.ThreadPoolExecutor(8) as pool:
    results = list(pool.map(fetch, wanted.values()))
others = [r for r in results if 'folder' in r]
failed = [f"{r['repo']}: {r['error']}" for r in results if 'error' in r]
print(f'{len(others)} repositories compared, in {int(time.time() - start)}s. {len(failed)} could not be downloaded. {len(skipped)} Torch listings name no source.')

# Rarity comes from how many plugins hold a line, so templates and game boilerplate drop out.
mine_plain, mine_masked = lines_of(repo)
my_commands, my_messages = behaviour_of(repo)
my_calls = calls_of(repo)
held, behaviour, call_runs = {}, {}, {}
counts_plain, counts_masked, counts_messages, counts_calls = collections.Counter(), collections.Counter(), collections.Counter(), collections.Counter()
for r in others:
    held[r['repo']] = lines_of(r['folder'])
    behaviour[r['repo']] = behaviour_of(r['folder'])
    counts_plain.update(held[r['repo']][0].keys())
    counts_masked.update(held[r['repo']][1].keys())
    counts_messages.update(behaviour[r['repo']][1])
    call_runs[r['repo']] = calls_of(r['folder'])
    counts_calls.update(call_runs[r['repo']])

rare_plain = {l for l in mine_plain if counts_plain[l] < COMMON_IN}
rare_masked = {l for l in mine_masked if counts_masked[l] < COMMON_IN}

my_date = next((d for d in map(created, own_repos) if d), None)
matches = []
for r in others:
    plain, masked = held[r['repo']]
    theirs_rare = {l for l in plain if counts_plain[l] < COMMON_IN + 1}
    shared = rare_plain & set(plain)
    renamed = (rare_masked & set(masked))
    of_mine = max(len(shared) / max(len(rare_plain), 1), len(renamed) / max(len(rare_masked), 1))
    of_theirs = len(set(mine_plain) & theirs_rare) / max(len(theirs_rare), 1)
    their_commands, their_messages = behaviour[r['repo']]
    messages = {m for m in my_messages & their_messages if counts_messages[m] < COMMON_IN}
    commands = same_commands(my_commands, their_commands)
    smaller_messages = max(min(len(my_messages), len(their_messages)), 1)
    smaller_commands = max(min(len(my_commands), len(their_commands)), 1)
    of_messages = len(messages) / smaller_messages if len(messages) >= MIN_MESSAGES else 0
    of_commands = commands / smaller_commands if commands >= MIN_COMMANDS else 0
    calls = {c for c in my_calls & call_runs[r['repo']] if counts_calls[c] < COMMON_IN}
    smaller_calls = max(min(len(my_calls), len(call_runs[r['repo']])), 1)
    of_calls = len(calls) / smaller_calls if len(calls) >= MIN_CALLS else 0
    if max(len(shared), len(renamed)) < MIN_LINES and not of_messages and not of_commands and not of_calls:
        continue
    credited_by_me = credits(repo, needles_for(r['repo'], r['plugin'], target['name']))
    credited_by_them = credits(r['folder'], [target['name'], *own_repos])
    score = max(of_mine, of_theirs)
    behaves_alike = of_messages >= SAME_MESSAGES_WARN or of_commands >= SAME_COMMANDS_WARN or of_calls >= SAME_CALLS_WARN
    their_date = created(r['repo']) if score >= WARN_AT or behaves_alike or forbids_reuse(r['folder']) else None
    they_are_older = bool(their_date) and (not my_date or their_date < my_date)
    # Only the original's terms bind the code. A later project's no-reuse notice is ignored here.
    original_forbids = they_are_older and forbids_reuse(r['folder'])
    size = 'EXTREME' if score >= EXTREME_AT or of_messages >= SAME_MESSAGES_EXTREME or of_commands >= SAME_COMMANDS_EXTREME or of_calls >= SAME_CALLS_EXTREME \
        else 'WARN' if score >= WARN_AT or of_messages >= SAME_MESSAGES_WARN or of_commands >= SAME_COMMANDS_WARN or of_calls >= SAME_CALLS_WARN else 'NOTICE'
    if credited_by_me or credited_by_them:
        level = size if original_forbids else 'NOTICE'
    else:
        level = size
    examples = sorted(shared, key=len, reverse=True)[:3]
    matches.append({'level': level, 'plugin': r['plugin'], 'repo': r['repo'], 'author': r['author'], 'source': r['source'], 'license': license_of(r['folder']),
                    'sharedLines': len(shared), 'renamedLines': len(renamed), 'sameCommands': commands, 'sameMessages': len(messages), 'ofCommands': round(of_commands, 3), 'ofMessages': round(of_messages, 3),
                    'sameCalls': len(calls), 'ofCalls': round(of_calls, 3), 'sampleCalls': [' > '.join(c) for c in sorted(calls)[:3]],
                    'sampleMessages': sorted(messages, key=len, reverse=True)[:4], 'ofThisPlugin': round(of_mine, 3), 'ofThatPlugin': round(of_theirs, 3),
                    'creditedHere': credited_by_me, 'creditedThere': credited_by_them, 'theirRepoCreated': their_date, 'thisRepoCreated': my_date,
                    'originalForbidsReuse': original_forbids, 'theirLicenseForbidsReuse': forbids_reuse(r['folder']), 'declaredUpstream': r.get('upstream', False), 'direction': 'this plugin holds their code' if of_mine >= of_theirs else 'their plugin holds this code',
                    'examples': [{'line': l[:130], 'here': mine_plain[l], 'there': plain[l]} for l in examples]})

order = {'EXTREME': 0, 'WARN': 1, 'NOTICE': 2}
matches.sort(key=lambda m: (order[m['level']], -max(m['ofThisPlugin'], m['ofThatPlugin'])))
for m in matches:
    credit = 'credited here' if m['creditedHere'] else 'credited there' if m['creditedThere'] else 'NO CREDIT FOUND'
    if m['originalForbidsReuse']:
        credit += ', and the original forbids reuse'
    elif m['theirLicenseForbidsReuse']:
        credit += ', their no-reuse notice is ignored because they are not the older project'
    print(f"{m['level']:8}{m['plugin']} ({m['repo']}, license: {m['license']}): {m['ofThisPlugin']:.0%} of this plugin's rare lines, "
          f"{m['ofThatPlugin']:.0%} of theirs, {m['sharedLines']} lines shared, {m['renamedLines']} matching once renamed, "
          f"{m['sameCommands']} same commands ({m['ofCommands']:.0%}), {m['sameMessages']} same messages ({m['ofMessages']:.0%}), "
          f"{m['sameCalls']} same call runs ({m['ofCalls']:.0%}). {credit}. {m['direction']}.")
    if m['level'] != 'NOTICE':
        for x in m['sampleMessages']:
            print(f'        message: {x[:120]}')
        for x in m['sampleCalls']:
            print(f'        calls: {x[:120]}')
        for x in m['examples']:
            print(f"        {x['here']}  =  {x['there']}\n        {x['line']}")
print(f"{len(rare_plain)} rare lines in this plugin. {sum(m['level'] == 'EXTREME' for m in matches)} EXTREME, {sum(m['level'] == 'WARN' for m in matches)} WARN, {sum(m['level'] == 'NOTICE' for m in matches)} NOTICE")
for f in failed[:10]:
    print('COULD NOT READ ' + f)

status = 'review'
if result_path:
    json.dump({'status': status, 'compared': len(others), 'matches': matches, 'failed': failed, 'noSource': skipped}, open(result_path, 'w', encoding='utf-8'), indent=1)
