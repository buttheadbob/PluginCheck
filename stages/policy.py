"""Checks a plugin against the written rules of the hubs and the loaders.

usage: policy.py <target.json> [results.json]

Each rule has an id, a severity and the reason behind it, so a report can cite it. `error` fails the stage. `warn` needs a
person to judge it. Rules are line based, so a hit is a lead to read in context.
"""
import json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # stages run with -I
import repocopy

SKIP = {'.claude', '.git', 'bin', 'obj', 'packages', 'GameBinaries', 'TorchBinaries', 'Build', '.vs', 'node_modules'}

# id, severity, applies to (all, magnetar, shared, torch, quasar), pattern, why
RULES = [
    # UnpatchAll(id) undoes only that Harmony id's patches. With no id, or null, it undoes every plugin's.
    ('PC001', 'error', 'all', r'\.UnpatchAll\(\s*(null\s*)?\)','UnpatchAll removes other plugins\' patches too. Undo only your own.'),
    ('PC002', 'error', 'all', r'SpecialFolder\.(Local)?ApplicationData', 'The game supports -appdata. Use MyFileSystem.UserDataPath.'),
    ('PC003', 'warn', 'all', r'MyFileSystem\.(ContentPath|ModsPath)\b.*(Write|Delete|Create|Move|Copy)|(Write|Delete|Create|Move)\w*\(.*MyFileSystem\.(ContentPath|ModsPath)', 'Never change game or mod files. Removing a plugin must undo its effects.'),
    ('PC004', 'error', 'all', r'\bEnvironment\.(Exit|FailFast)\(', 'A plugin must not kill the server process. Use ServerControl or the loader.'),
    ('PC005', 'error', 'all', r'Process\.GetCurrentProcess\(\)\.Kill|\bProcess\.Kill\(', 'A plugin must not kill the server process.'),
    ('PC006', 'warn', 'all', r'\b(Assembly\.(Load|LoadFile|LoadFrom)|AppDomain\.CreateDomain)\(', 'Dynamic loading hides code from review. Reject unless explained.'),
    ('PC007', 'error', 'all', r'\bBinaryFormatter\b|TypeNameHandling\.(All|Auto|Objects|Arrays)', 'Unsafe deserialization can execute code from data.'),
    ('PC008', 'warn', 'all', r'(?i)\b(self[- ]?update|auto[- ]?update|download.{0,30}\.(dll|exe|zip))', 'Self-updating code is banned: the registry pin and the loader own updates.'),
    ('PC009', 'warn', 'all', r'new Harmony\(\s*"', 'Harmony ids must be unique. Use your plugin id, not a generic name.'),
    ('PC010', 'warn', 'all', r'\bcatch\s*(\(\s*(System\.)?Exception\s*\w*\s*\))?\s*\{\s*\}', 'An empty catch hides failures. Log it, or explain why it is optional.'),
    ('PC011', 'warn', 'all', r'\bThread\.Sleep\(\s*[1-9]', 'Sleeping on the game thread freezes the server, and on the pool it starves it. Check which this is.'),
    ('PC012', 'warn', 'all', r'\.(Wait|GetAwaiter\(\)\.GetResult)\(\)|\.Result\b(?!\s*=)', 'Blocking on a task on the game thread can deadlock the server.'),
    ('PC013', 'warn', 'all', r'"[^"\n]*\w\\\\\w[^"\n]*\.(cs|cfg|xml|json|log|txt|dll)"', 'A hard-coded backslash path breaks on Linux. Use Path.Combine.'),
    ('PC014', 'warn', 'magnetar', r'\bMicrosoft\.Win32\b|\bRegistry\.|\bSystem\.Windows\.|\bSystem\.Drawing\b|\bWindowsIdentity\b', 'Windows only. Magnetar Interim runs on Linux, and this code compiles there.'),
    ('PC015', 'error', 'magnetar', r'\[\s*Command\(\s*"help(\s[^"]*)?"', '`help` is reserved as the first word under every root, and stops registration.'),
    ('PC016', 'error', 'magnetar', r'\[\s*CommandRoot\(\s*"help"', '`help` is a reserved root prefix.'),
    ('PC017', 'warn', 'magnetar', r'\bConsole\.(Write|WriteLine)\(', 'Use PluginSdk Logger, so output reaches the log when managed by Quasar.'),
    ('PC018', 'warn', 'shared', r'\b(NLog|Torch\.API|Torch\.Commands|HarmonyLib|PluginSdk)\b', 'Shared code must not depend on one loader.'),
    ('PC019', 'warn', 'quasar', r'\(MarkupString\)|new MarkupString\(', 'Raw HTML: check that nothing user controlled reaches it.'),
    ('PC020', 'warn', 'quasar', r'\bJSRuntime\.|\bIJSRuntime\b|\beval\(', 'JS interop: trace what is passed through it.'),
    ('PC021', 'warn', 'quasar', r'@page\s+"', 'Check this page requires the right permission, and is not open to viewers by default.'),
    ('PC022', 'warn', 'quasar', r'\bMap(Get|Post|Put|Delete)\(', 'Check this endpoint has an authorization policy.'),
    ('PC023', 'warn', 'torch', r'\bIsUnderTest\b|\bTorchBase\.Instance\.(Destroy|Stop)\(', 'Check Torch lifecycle use: a plugin must not stop Torch itself.'),
    ('PC024', 'warn', 'all', r'(?i)\b(telemetry|analytics|phone.?home)\b', 'Hidden telemetry is rejected. Check it is disclosed and opt-in.'),
]
RULES = [(i, s, a, re.compile(p), w) for i, s, a, p, w in RULES]

target = json.load(open(sys.argv[1], encoding='utf-8'))
result_path = sys.argv[2] if len(sys.argv) > 2 else None
repo = target['repo']


def scopes(rel):
    # Which rule groups apply to a file, from where it sits in this target's layout.
    top = rel.split('/')[0]
    out = {'all'}
    if top == 'Shared':
        out |= {'shared', 'magnetar'}
    if 'quasar' in target and (rel.lower().startswith('quasarplugin') or rel.endswith(('.razor', '.cshtml'))):
        out.add('quasar')
    if 'torch' in target and rel.startswith(os.path.dirname(target['torch'].get('project', '')) + '/'):
        out.add('torch')
    if 'magnetar' in target and rel.startswith(os.path.dirname(target['magnetar'].get('project', '')) + '/'):
        out.add('magnetar')
    return out


found = []
for d, ds, fs in repocopy.walk(repo):
    ds[:] = [x for x in ds if x not in SKIP]
    for f in fs:
        if not f.endswith(('.cs', '.razor', '.cshtml')):
            continue
        path = os.path.join(d, f)
        rel = os.path.relpath(path, repo).replace(os.sep, '/')
        groups = scopes(rel)
        for n, line in enumerate(open(path, encoding='utf-8', errors='replace'), 1):
            code = line.split('//')[0] if '"' not in line else line
            if code.strip().startswith(('//', '*', '///')):
                continue
            for rule, severity, group, pattern, why in RULES:
                if group in groups and pattern.search(code):
                    found.append({'rule': rule, 'severity': severity, 'file': rel, 'line': n, 'text': line.strip()[:130], 'why': why})

errors = [x for x in found if x['severity'] == 'error']
for x in sorted(found, key=lambda v: (v['severity'], v['rule'])):
    print(f"{x['severity'].upper():5} {x['rule']} {x['file']}:{x['line']}  {x['text']}")
print(f'{len(errors)} errors, {len(found) - len(errors)} warnings from {len(RULES)} rules')
status = 'fail' if errors else 'review' if found else 'pass'
if result_path:
    json.dump({'status': status, 'errors': len(errors), 'warnings': len(found) - len(errors), 'findings': found}, open(result_path, 'w', encoding='utf-8'), indent=1)
sys.exit(1 if errors else 0)
