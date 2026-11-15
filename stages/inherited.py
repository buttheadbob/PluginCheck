"""Fair measure of how much code came from an upstream project. Position never matters, and small edits still count.

usage: inherited.py <target.json> <run dir> [results.json]

Do not use git statistics for this: a moved line or a one-character edit does not become new work.
Each code line gets the first tier it reaches:
  exact    same text after trimming and collapsing whitespace
  near     at least 75% of tokens match some upstream line, in order
  moved    not near, but its tokens sit inside an 8-token run that exists upstream

The target's knownNew folders have no upstream equivalent, so whatever matches there is chance, and that rate is
subtracted to give the adjusted figure. Report the range. Code rewritten from the original can still derive from it
without any line matching, so even the raw figure is not a ceiling.
"""
import collections, io, json, os, re, sys, urllib.request, zipfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # stages run with -I
import repocopy
from difflib import SequenceMatcher

target = json.load(open(sys.argv[1], encoding='utf-8'))
run_dir = sys.argv[2]
result_path = sys.argv[3] if len(sys.argv) > 3 else None
repo = target['repo']
unpacked = os.path.join(run_dir, 'upstream')
zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(target['upstream'], timeout=120).read())).extractall(unpacked)
upstream = os.path.join(unpacked, os.listdir(unpacked)[0], target.get('upstreamSubfolder', ''))
EXCLUDE = tuple(target.get('exclude', []))
KNOWN_NEW = tuple(target.get('knownNew', []))
EXT = ('.cs', '.xaml')
SKIP = {'.claude', 'bin', 'obj', '.git', 'Build', 'GameBinaries', 'TorchBinaries', 'node_modules', 'Essentials.Tests', 'Jenkins'}
KW = set('''if else for foreach while do return var new public private protected internal static void bool int long
double float string class struct enum namespace try catch finally throw null true false this base in out ref is as
continue break switch case default using readonly const override virtual abstract sealed async await params typeof'''.split())
TOK = re.compile(r'"(?:[^"\\]|\\.)*"|[A-Za-z_]\w*|\d+\.?\d*|\S')
N = 8

def toks(s): return TOK.findall(s)
def mask(ts):
    out = []
    for t in ts:
        if t[0] == '"': out.append('L')
        elif t[0].isdigit(): out.append('L')
        elif t[0].isalpha() or t[0] == '_': out.append(t if t in KW else 'I')
        else: out.append(t)
    return out

def code_lines(path):
    out = []
    for raw in open(path, encoding='utf-8-sig', errors='replace'):
        l = raw.strip()
        if len(l) < 12 or l.startswith(('//', '///', '*', '/*', 'using ', '#')):
            continue
        if not re.search(r'[A-Za-z]{3}', l):
            continue
        out.append(re.sub(r'\s+', ' ', l))
    return out

def walk(root):
    for d, ds, fs in repocopy.walk(root):
        ds[:] = [x for x in ds if x not in SKIP]
        for f in fs:
            if f.endswith(EXT) and f != 'AssemblyVersion.cs':
                yield os.path.join(d, f)

def runs(files_tokens, f):
    s = set()
    for ts in files_tokens:
        t = f(ts)
        for i in range(len(t) - N + 1):
            s.add(tuple(t[i:i + N]))
    return s

up = {p: code_lines(p) for p in walk(upstream)}
up_lines = sorted(set(l for v in up.values() for l in v))
up_set = set(up_lines)
up_toks = [toks(l) for l in up_lines]
index = collections.defaultdict(set)
for i, ts in enumerate(up_toks):
    for t in set(ts):
        if len(t) > 2 and t not in KW:
            index[t].add(i)
# Whole upstream files as one token stream, so a run can cross line breaks.
streams = [[t for l in v for t in toks(l)] for v in up.values()]
exact_runs = runs(streams, lambda t: t)

def near(ts):
    votes = collections.Counter()
    for t in set(ts):
        if t in index and len(index[t]) < 400:
            votes.update(index[t])
    best = 0
    for i, _ in votes.most_common(25):
        best = max(best, SequenceMatcher(None, ts, up_toks[i], autojunk=False).ratio())
    return best >= .75

def covered(tokens, per_line, runset, f):
    """Marks tokens that fall inside any N-token run present upstream."""
    t = f(tokens)
    hit = [False] * len(t)
    for i in range(len(t) - N + 1):
        if tuple(t[i:i + N]) in runset:
            for j in range(i, i + N):
                hit[j] = True
    res, pos = [], 0
    for c in per_line:
        res.append(c > 0 and sum(hit[pos:pos + c]) / c >= .6)
        pos += c
    return res

groups = collections.OrderedDict()
rows = []
for p in walk(repo):
    rel = os.path.relpath(p, repo).replace(os.sep, '/')
    if rel.startswith(EXCLUDE) if EXCLUDE else False:
        continue
    lines = code_lines(p)
    if not lines:
        continue
    lt = [toks(l) for l in lines]
    counts = [len(x) for x in lt]
    moved = covered([t for x in lt for t in x], counts, exact_runs, lambda t: t)
    c = collections.Counter()
    for i, l in enumerate(lines):
        c['exact' if l in up_set else 'near' if near(lt[i]) else 'moved' if moved[i] else 'new'] += 1
    top = rel.split('/')
    g = '/'.join(top[:2]) if top[0] == 'Shared' else top[0]
    g = next((k for k in KNOWN_NEW if rel.startswith(k)), g)
    groups.setdefault(g, collections.Counter()).update(c)
    groups[g]['lines'] += len(lines)
    rows.append((rel, len(lines), c))


def share(c):
    return (c['exact'] + c['near'] + c['moved']) / c['lines'] if c['lines'] else 0


control = collections.Counter()
total = collections.Counter()
for g, c in groups.items():
    total.update(c)
    if g in KNOWN_NEW:
        control.update(c)
noise = share(control)


def adjusted(c):
    return max(0, (share(c) - noise) / (1 - noise))


print(f'{"area":28}{"lines":>7}{"exact":>7}{"near":>6}{"moved":>7}{"new":>6}{"raw":>7}{"adjusted":>10}')
for g, c in list(groups.items()) + [('TOTAL', total)]:
    print(f'{g:28}{c["lines"]:>7}{c["exact"]:>7}{c["near"]:>6}{c["moved"]:>7}{c["new"]:>6}{share(c):>7.0%}{adjusted(c):>10.0%}')
print(f'chance match rate, from {control["lines"]} lines of known-new code: {noise:.0%}')
print()
print('most inherited files (30 lines or more, raw)')
for rel, n, c in sorted([r for r in rows if r[1] >= 30], key=lambda r: -(r[2]['exact'] + r[2]['near'] + r[2]['moved']) / r[1])[:12]:
    print(f"{(c['exact'] + c['near'] + c['moved']) / n:>5.0%} {n:>5}  {rel}")
if result_path:
    json.dump({'status': 'review', 'raw': round(share(total), 3), 'adjusted': round(adjusted(total), 3), 'chanceRate': round(noise, 3),
               'lines': total['lines'], 'excluded': list(EXCLUDE),
               'areas': {g: {'lines': c['lines'], 'raw': round(share(c), 3), 'adjusted': round(adjusted(c), 3)} for g, c in groups.items()}},
              open(result_path, 'w', encoding='utf-8'), indent=1)
