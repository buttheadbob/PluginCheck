"""Collects every published plugin from torchapi.com, MagnetarHub and QuasarHub, for an AI to compare with this one.

usage: similar.py <target.json> <run dir> [results.json]

This stage only gathers. It writes similar-catalog.txt, one plugin per line, with this plugin's own details first. Whether
two plugins overlap is a matter of meaning, so an AI reads the file and judges it. See REVIEW.md. It always ends as `review`.
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import githubtoken, repocopy
import io, json, os, re, sys, urllib.request, zipfile
import xml.etree.ElementTree as ET

SOURCES = {'MagnetarHub': 'https://codeload.github.com/CometWorks/magnetar-hub/zip/refs/heads/main',
           'QuasarHub': 'https://codeload.github.com/CometWorks/quasar-hub/zip/refs/heads/main'}
TORCH = 'https://torchapi.com/api/plugins'
SKIP = {'.claude', '.git', 'bin', 'obj', 'packages', 'GameBinaries', 'TorchBinaries', 'Build', '.vs'}
LENGTH = 320

target = json.load(open(sys.argv[1], encoding='utf-8'))
run_dir = os.path.abspath(sys.argv[2])
result_path = sys.argv[3] if len(sys.argv) > 3 else None
repo = target['repo']


def fetch(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'PluginCheck', **githubtoken.headers(url)}), timeout=60).read()


def tidy(text):
    text = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', text or '')
    text = re.sub(r'<[^>]+>|https?://\S+|[#*`_|]', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()[:LENGTH]


own_ids = set()
for section in ('magnetar', 'quasar'):
    manifest = target.get(section, {}).get('manifest')
    if manifest:
        own_ids.add((ET.parse(os.path.join(repo, manifest)).getroot().findtext('Id') or '').strip().lower())
if target.get('torch', {}).get('guid'):
    own_ids.add(target['torch']['guid'].lower())

plugins, failed = [], []
try:
    for p in json.loads(fetch(TORCH))['plugins']:
        if p.get('private'):
            continue
        plugins.append({'source': 'torchapi.com', 'id': p.get('guid') or p.get('id'), 'name': p.get('name'), 'author': p.get('author'),
                        'text': tidy(p.get('description') or p.get('short_description')), 'archived': bool(p.get('archived')), 'downloads': p.get('downloads')})
except Exception as e:
    failed.append(f'torchapi.com: {e}')

for source, url in SOURCES.items():
    try:
        archive = zipfile.ZipFile(io.BytesIO(fetch(url)))
        for item in archive.namelist():
            if '/Plugins/' not in item or not item.endswith('.xml'):
                continue
            try:
                root = ET.fromstring(archive.read(item))
            except ET.ParseError:
                continue
            plugins.append({'source': source, 'id': (root.findtext('Id') or '').strip(), 'name': (root.findtext('FriendlyName') or os.path.basename(item)).strip(),
                            'author': (root.findtext('Author') or '').strip(), 'archived': False, 'downloads': None,
                            'text': tidy((root.findtext('Tooltip') or '') + '. ' + (root.findtext('Description') or ''))})
    except Exception as e:
        failed.append(f'{source}: {e}')

others = [p for p in plugins if (p['id'] or '').lower() not in own_ids]

# What this plugin claims, so the comparison has something to start from.
roots = set()
for d, ds, fs in repocopy.walk(repo):
    ds[:] = [x for x in ds if x not in SKIP]
    for f in fs:
        if f.endswith('.cs'):
            text = open(os.path.join(d, f), encoding='utf-8', errors='replace').read()
            roots.update(re.findall(r'\[CommandRoot\(\s*"(\w+)"', text))
            roots.update(re.findall(r'\[Category\(\s*"(\w+)"', text))
summary = ''
for section in ('magnetar', 'quasar'):
    manifest = target.get(section, {}).get('manifest')
    if manifest:
        summary += tidy(ET.parse(os.path.join(repo, manifest)).getroot().findtext('Description')) + ' '

lines = [f'THIS PLUGIN: {target["name"]}', f'Command roots: {" ".join(sorted(roots))}', f'Description: {summary.strip()}', '',
         'PUBLISHED PLUGINS: source | name | author | archived or downloads | description']
for p in sorted(others, key=lambda v: (v['source'], (v['name'] or '').lower())):
    state = 'archived' if p['archived'] else p['downloads'] if p['downloads'] is not None else ''
    lines.append(f"{p['source']} | {p['name']} | {p['author']} | {state} | {p['text']}")
open(os.path.join(run_dir, 'similar-catalog.txt'), 'w', encoding='utf-8').write('\n'.join(lines))

counts = {s: sum(p['source'] == s for p in others) for s in ('torchapi.com', *SOURCES)}
print(f'{len(others)} published plugins: ' + ', '.join(f'{k} {v}' for k, v in counts.items()))
print(f'{len(roots)} command roots from this plugin. Wrote similar-catalog.txt for an AI to read.')
for f in failed:
    print('COULD NOT READ ' + f)
if result_path:
    json.dump({'status': 'review' if not failed else 'not run' if not others else 'review', 'counts': counts, 'failed': failed, 'roots': sorted(roots)},
              open(result_path, 'w', encoding='utf-8'), indent=1)
