"""Lists places a reviewer must read. A hit is a lead, not a finding, and a clean scan proves nothing.

usage: scan.py <target.json> [results.json]
"""
import collections, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # stages run with -I
import repocopy

target = json.load(open(sys.argv[1], encoding='utf-8'))
repo = target['repo']
SKIP = {'.claude', '.git', 'bin', 'obj', 'node_modules', 'packages', 'GameBinaries', 'TorchBinaries', 'Build', '.vs'}
TEXT = ('.cs', '.razor', '.cshtml', '.xaml', '.csproj', '.projitems', '.props', '.targets', '.ps1', '.bat', '.cmd',
        '.sh', '.js', '.json', '.xml', '.yml', '.yaml', '.md', '.config')
BINARY = ('.dll', '.exe', '.so', '.dylib', '.pdb', '.zip', '.7z', '.rar', '.nupkg', '.bin')
SHOWN = ('prompt injection', 'process', 'dynamic code', 'deserialization', 'null forgiven lookup')
RULES = {k: re.compile(v) for k, v in {
    'process': r'Process\.Start|ProcessStartInfo|cmd\.exe|powershell|/bin/(ba)?sh',
    'dynamic code': r'Assembly\.Load|LoadFrom\(|Activator\.CreateInstance|CSharpCompilation|DynamicMethod|Reflection\.Emit',
    'native': r'DllImport|LibraryImport|Marshal\.|unsafe\b',
    'deserialization': r'BinaryFormatter|TypeNameHandling|NetDataContractSerializer|LosFormatter',
    'network': r'HttpClient|WebClient|WebRequest|TcpClient|UdpClient|Socket\b|https?://',
    'file access': r'File\.(Write|Delete|Move|Copy|Create|Open|Append)|Directory\.(Delete|Move|Create)|SpecialFolder\.',
    'encoded data': r'FromBase64String|GZipStream|DeflateStream|Rijndael|Aes\.Create',
    'credentials': r'(?i)\b(password|passwd|api[_-]?key|secret|token)\b\s*[=:]',
    'reflection': r'GetNestedType|GetMethod\(|GetField\(|GetProperty\(|BindingFlags\.NonPublic|\.Invoke\(null',
    'null forgiven lookup': r'(GetNestedType|GetMethod|GetField|GetProperty)\([^;]*\)!',
    'patching': r'UnpatchAll|\[HarmonyPatch|new Harmony\(',
    'blocking': r'Thread\.Sleep|\.Wait\(\)|\.Result\b|\.GetAwaiter\(\)\.GetResult',
    'raw html': r'MarkupString|IJSRuntime|eval\(',
    'license text': r'(?i)not free|proprietary|all rights reserved|apache license|\bGPL\b|copyright',
    'prompt injection': r'(?i)(ignore|disregard) (all |any )?(previous|prior|above) (instructions|prompts)|you are an? (ai|assistant)|(ai|llm) reviewers?',
}.items()}

hits, binaries, files = collections.defaultdict(list), [], 0
# Things that bring in code a file search here would not see.
outside = []
for d, ds, fs in os.walk(repo):
    if '.git' in ds and os.path.abspath(d) != os.path.abspath(repo):
        outside.append(f'nested repository: {os.path.relpath(d, repo)}')
    for x in ds:
        if repocopy.linked(os.path.join(d, x)) and x not in SKIP:
            outside.append(f'linked folder: {os.path.relpath(os.path.join(d, x), repo)}')
    ds[:] = [x for x in ds if x not in SKIP and not repocopy.linked(os.path.join(d, x))]
    for f in fs:
        path = os.path.join(d, f)
        if f == '.gitmodules':
            outside += [f'submodule: {u.strip()}' for u in open(path, encoding='utf-8', errors='replace') if u.strip().startswith('url')]
        elif f == '.gitattributes' and 'filter=lfs' in open(path, encoding='utf-8', errors='replace').read():
            outside.append('Git LFS is in use: tracked content may be stored outside the repository')
        elif f.lower() == 'nuget.config':
            outside.append(f'custom NuGet sources: {os.path.relpath(path, repo)}')
        elif os.path.islink(path):
            outside.append(f'linked file: {os.path.relpath(path, repo)}')
        rel = os.path.relpath(path, repo).replace(os.sep, '/')
        files += 1
        low = f.lower()
        if low.endswith(BINARY):
            binaries.append({'file': rel, 'bytes': os.path.getsize(path)})
        elif low.endswith(TEXT):
            for n, line in enumerate(open(path, encoding='utf-8', errors='replace'), 1):
                for name, rule in RULES.items():
                    if rule.search(line):
                        hits[name].append({'file': rel, 'line': n, 'text': line.strip()[:160]})

print(f'{files} files walked, {len(binaries)} binary or archive files, {len(outside)} sources of outside code')
for o in outside:
    print(f'OUTSIDE {o}')
for b in binaries:
    print(f"BINARY {b['file']} ({b['bytes']} bytes)")
for name in RULES:
    found = hits.get(name, [])
    print(f'{len(found):>5}  {name}')
    if name in SHOWN:
        for h in found[:12]:
            print(f"       {h['file']}:{h['line']}  {h['text']}")
if len(sys.argv) > 2:
    json.dump({'status': 'review', 'files': files, 'binaries': binaries, 'outside': outside, 'hits': hits},
              open(sys.argv[2], 'w', encoding='utf-8'), indent=1)
