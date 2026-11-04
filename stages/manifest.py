"""Checks hub manifests against the rules hub reviewers apply.

usage: manifest.py <target.json> [results.json]
"""
import json, os, re, sys
import xml.etree.ElementTree as ET

LIMIT = 1000
target = json.load(open(sys.argv[1], encoding='utf-8'))
repo = target['repo']
items = []


def add(level, file, text):
    items.append({'level': level, 'file': file, 'text': text})


def check(section):
    conf = target.get(section)
    if not conf or 'manifest' not in conf:
        return
    name = conf['manifest']
    raw = open(os.path.join(repo, name), encoding='utf-8-sig').read()
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        add('fail', name, f'not valid XML: {e}')
        return

    def text(tag):
        return (root.findtext(tag) or '').strip()

    for tag in ('Id', 'RepoId', 'FriendlyName', 'Author', 'Tooltip', 'Commit'):
        if not text(tag):
            add('fail', name, f'<{tag}> is missing')
    if 'TODO' in raw:
        add('fail', name, 'contains a leftover TODO')
    commit = text('Commit')
    if commit and not re.fullmatch(r'[0-9a-f]{40}', commit):
        add('fail', name, f'<Commit> is not a full SHA: {commit}')

    length = len(root.findtext('Description') or '')
    add('fail' if length > LIMIT else 'pass', name, f'description is {length} characters, the limit is {LIMIT}')

    for d in root.findall('SourceDirectories/Directory'):
        if not os.path.isdir(os.path.join(repo, (d.text or '').strip())):
            add('fail', name, f'source directory does not exist: {d.text}')
    if section == 'magnetar' and root.find('SourceDirectories') is None:
        add('warn', name, 'no <SourceDirectories>: the whole repository gets compiled')

    if section == 'quasar':
        if text('PluginKind') != 'QuasarUiPlugin':
            add('fail', name, '<PluginKind> must be QuasarUiPlugin')
        for tag in ('ProjectPath', 'PackageManifest'):
            value = text(tag)
            if not value or not os.path.exists(os.path.join(repo, value)):
                add('fail', name, f'<{tag}> does not point at a file: {value}')


for section in ('magnetar', 'quasar'):
    check(section)

guid = target.get('torch', {}).get('guid')
if guid:
    path = os.path.join(repo, os.path.dirname(target['torch']['project']), 'manifest.xml')
    found = (ET.parse(path).getroot().findtext('Guid') or '').strip() if os.path.exists(path) else ''
    add('pass' if found.lower() == guid.lower() else 'fail', 'manifest.xml', f'Torch GUID is {found or "missing"}')

failed = any(i['level'] == 'fail' for i in items)
for i in items:
    print(f"{i['level'].upper():5} {i['file']}: {i['text']}")
if len(sys.argv) > 2:
    json.dump({'status': 'fail' if failed else 'pass', 'items': items}, open(sys.argv[2], 'w', encoding='utf-8'), indent=1)
sys.exit(1 if failed else 0)
