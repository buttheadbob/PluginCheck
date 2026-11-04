"""Reviews every log a run produced, since a run can pass while its logs show a real problem.

usage: logcheck.py <target.json> <run dir> [results.json]

Runs last and reads the server, loader, Torch, Keen and Quasar logs. Lines that differ only by numbers, ids or times are
grouped, so one error repeated 500 times reads as one line with a count. Lines naming the plugin are separated from the
rest. A stack trace through the plugin's own code fails the stage. Everything else ends as `review`, because a person or AI
must read it: this stage finds the lines, and does not know which ones matter.
"""
import collections, json, os, re, sys

SKIP_DIRS = {'source', 'quasar-source', 'plugins', 'upstream', 'lint', 'threads', 'reflection', 'dependencies', 'regcheck', 'torch-plugins', 'Content', 'content', 'Mods', 'cache'}
STAGE_LOGS = {'legacy', 'manifest', 'scan', 'policy', 'dependencies', 'inherited', 'magnetar', 'regcheck', 'quasar', 'quasar_managed', 'torch', 'reflection',
              'threads', 'nulls', 'similar', 'copies', 'hostpackages', 'lint', 'logcheck'}
PROBLEM = re.compile(r'\[(WARN|WARNING|ERROR|FATAL|CRITICAL)\]|\b(Exception|Unhandled|Traceback|NullReference|StackOverflow|OutOfMemory|deadlock|Assert|fatal error|crash)\w*\b|\bFailed to\b|\bCould not\b|\bUnable to\b|\berror:', re.I)
TRACE = re.compile(r'^\s+at (.+?)(?:\(| in )')
QUIET = re.compile(r'Hosting\.Diagnostics|EndpointMiddleware|LogicalHandler|ClientHandler')
NOISE = [re.compile(p) for p in (
    r'^\s+at ', r'Failed to parse texture header', r'ntsync unavailable', r'Consent: no interactive terminal', r'Overriding HTTP_PORTS',
    r'Missing definition MyObjectBuilder_DemoComponentDefinition', r'possible \d+ extra bytes', r'Using fallback search for project files',
    r'Could not resolve keysym', r'Errors from xkbcomp', r'start process .git.', r'An error occurred while checking git')]

target = json.load(open(sys.argv[1], encoding='utf-8'))
run_dir = os.path.abspath(sys.argv[2])
result_path = sys.argv[3] if len(sys.argv) > 3 else None
name = target['name']
marks = {name.lower(), target.get('namespace', name).lower()}
words = re.findall(r'[A-Z][a-z]+', name)
if len(words) > 1:
    marks.add(' '.join(words).lower())


def normal(line):
    line = re.sub(r'^\s*\d{4}-\d\d-\d\d[ T][\d:.]+(?:\s*[-:])?\s*', '', line)
    line = re.sub(r'^\s*\d\d:\d\d:\d\d\.\d+\s*', '', line)
    line = re.sub(r'\[thread \d+\]|\(\d+\)\s|Thread:\s*\d+\s*->', '', line)
    line = re.sub(r'[0-9a-f]{8}-[0-9a-f-]{27}|\b[0-9a-f]{16,}\b', '<id>', line)
    line = re.sub(r'_[a-z0-9]{8}\.[a-z0-9]{3}\b', '', line)
    line = re.sub(r'\d+', '#', line)
    return line.strip()[:220]


groups = collections.OrderedDict()
traces = collections.Counter()
files = 0
for d, ds, fs in os.walk(run_dir):
    ds[:] = [x for x in ds if x not in SKIP_DIRS]
    for f in fs:
        if not f.endswith('.log') or os.path.splitext(f)[0] in STAGE_LOGS:
            continue
        path = os.path.join(d, f)
        rel = os.path.relpath(path, run_dir).replace(os.sep, '/')
        files += 1
        previous = ''
        for line in open(path, encoding='utf-8', errors='replace'):
            trace = TRACE.match(line)
            if trace and any(m.replace(' ', '') in trace.group(1).lower() for m in marks):
                traces[(trace.group(1).strip()[:150], normal(previous)[:150])] += 1
            if not trace:
                previous = line
            if not PROBLEM.search(line) or QUIET.search(line) or any(n.search(line) for n in NOISE):
                continue
            key = normal(line)
            group = groups.setdefault(key, {'text': line.strip()[:240], 'count': 0, 'files': set()})
            group['count'] += 1
            group['files'].add(rel)

items = [{'text': g['text'], 'count': g['count'], 'files': sorted(g['files'])} for g in groups.values()]
for item in items:
    item['plugin'] = any(m in item['text'].lower() for m in marks)
items.sort(key=lambda x: (not x['plugin'], -x['count']))
plugin = [x for x in items if x['plugin']]
stack = [{'frame': k[0], 'after': k[1], 'count': v} for k, v in traces.most_common()]

print(f'{files} logs read, {len(items)} distinct lines to read, {len(plugin)} naming the plugin, {len(stack)} stack frames in plugin code')
for s in stack[:20]:
    print(f"TRACE x{s['count']}  {s['frame']}\n      after: {s['after']}")
for x in items[:45]:
    tag = 'PLUGIN' if x['plugin'] else 'other '
    print(f"{tag} x{x['count']:<4} {x['text'][:175]}  [{x['files'][0]}]")
if len(items) > 45:
    print(f'... and {len(items) - 45} more in logcheck.json')
status = 'fail' if stack else 'review' if items else 'pass'
if result_path:
    json.dump({'status': status, 'logs': files, 'stackFrames': stack, 'lines': items}, open(result_path, 'w', encoding='utf-8'), indent=1)
sys.exit(1 if stack else 0)
