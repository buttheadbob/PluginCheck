"""Load test: builds a Quasar UI plugin the way Quasar's installer does and loads it into a real Quasar web worker.

usage: quasar.py <target.json> <run dir> [results.json]

Runs in its own containers with its own data, separate from the plain Magnetar stage, since not every server uses
Quasar. The plugin is built against each running Quasar's own Quasar.Plugin.Abstractions.dll. Quasar serves a plugin's
static files only after loading it, so requesting its stylesheet is the proof that it loaded.

Quasar 1.5 refuses to start without the receipt a 1.4.1 start writes, and fresh installs are not exempt, so the versions
run in order on the same data, which is the documented path. The 1.4.1.1 image has a packaging bug: release.json says
1.4.1-1 while the program reports 1.4.1.1, so writing the receipt throws and 1.5 cannot start. For that step only, a
corrected release.json is mounted, so Quasar's own code writes the real receipt. Results record this as a workaround.
Never write the receipt by hand: a test that skips the real path proves nothing.

The admin Steam ID below is a placeholder that is never signed in with. It only satisfies Quasar's first-run requirement.
"""
import json, os, re, shutil, subprocess, sys, time

VERSIONS = ['1.4.1.1', 'latest']
TEST_ADMIN = '76561190000000000'
TIMEOUT = 10 * 60
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # stages run with -I
import repocopy
import pin

target = json.load(open(sys.argv[1], encoding='utf-8'))
run_dir = os.path.abspath(sys.argv[2])
result_path = sys.argv[3] if len(sys.argv) > 3 else None
name = target['name']
conf = target['quasar']
source = os.path.join(run_dir, 'quasar-source', name)
data = os.path.join(run_dir, 'quasar-data')

shutil.copytree(target['repo'], source, ignore=repocopy.ignore(target['repo']))
os.makedirs(data, exist_ok=True)
package = json.load(open(os.path.join(source, conf['packageManifest']), encoding='utf-8'))
project = package['projectPath']
abstractions = '/opt/quasar/Quasar.Plugin.Abstractions.dll'
assets = [f"/_quasar/plugins/{package['id']}/{s}" for s in package.get('stylesheets', [])]


def docker(*args):
    return subprocess.run(['docker', *args], capture_output=True, text=True, encoding='utf-8', errors='replace')


def test(version):
    container = f'plugincheck-quasar-{version.replace(".", "-")}-{int(time.time())}'
    image = f'ghcr.io/cometworks/quasar:{version}'
    pull = docker('pull', image)
    if pull.returncode:
        return {'version': version, 'status': 'fail', 'problems': [pull.stderr.strip()[:300]]}
    resolved = docker('image', 'inspect', image, '--format', '{{index .Config.Labels "org.opencontainers.image.version"}}').stdout.strip()

    # Earlier versions leave their build output behind, and this build must be against this version's assembly.
    for sub in ('bin', 'obj'):
        shutil.rmtree(os.path.join(source, os.path.dirname(project), sub), ignore_errors=True)
    script = (f'set -e; cd "/plugins/{name}"; '
              f'dotnet build "{project}" -c Release -p:QuasarPluginAbstractionsAssembly={abstractions} -p:DeployToQuasar=false; '
              'echo PLUGINCHECK_BUILD_OK; cd /opt/quasar; exec /opt/quasar/Quasar')
    extra = []
    packaged = docker('run', *pin.args(), '--rm', '--entrypoint', 'cat', image, '/opt/quasar/release.json').stdout
    workaround = version.startswith('1.4.1') and json.loads(packaged).get('version') != resolved
    if workaround:
        fixed = os.path.join(run_dir, f'release-{resolved}.json')
        json.dump({**json.loads(packaged), 'version': resolved}, open(fixed, 'w', encoding='utf-8'))
        extra = ['-v', f'{fixed}:/opt/quasar/release.json:ro']

    subprocess.run(['docker', 'run', *pin.args(), '-d', '--name', container, '--cpus', '4', '--memory', '6g', '--entrypoint', 'bash', *extra,
                    '-e', 'QUASAR_UI_PLUGIN_DIRS=/plugins', '-e', f'QUASAR_ADMIN_STEAM_ID={TEST_ADMIN}',
                    '-e', 'QUASAR_HOST_ID=plugincheck', '-e', 'QUASAR_LOG_MIN_LEVEL=Info',
                    '-v', f'{os.path.join(run_dir, "quasar-source")}:/plugins',
                    '-v', f'{data}:/data',
                    image, '-c', script], check=True, stdout=subprocess.DEVNULL)

    def status(path):
        return docker('exec', container, 'curl', '-s', '-o', '/dev/null', '-w', '%{http_code}',
                      f'http://127.0.0.1:8080{path}').stdout.strip()

    start, healthy = time.time(), False
    while time.time() - start < TIMEOUT:
        if docker('inspect', '-f', '{{.State.Running}}', container).stdout.strip() != 'true':
            break
        if status('/api/health') == '200':
            healthy = True
            break
        time.sleep(5)
    time.sleep(10)

    served = {a: status(a) for a in assets} if healthy else {}
    logs = docker('logs', container)
    text = logs.stdout + logs.stderr
    open(os.path.join(run_dir, f'quasar-{version}.log'), 'w', encoding='utf-8').write(text)
    docker('stop', '-t', '30', container)
    docker('rm', '-f', '-v', container)

    problems = [l.strip()[:300] for l in text.splitlines()
                if re.search(r'error CS\d{4}|error MSB\d{4}|ui plugin.*(error|fail)|Unhandled exception|require a completed', l, re.I)]
    built = 'PLUGINCHECK_BUILD_OK' in text
    warnings = re.search(r'(\d+) Warning\(s\)', text)
    loaded = bool(served) and all(v == '200' for v in served.values())
    ok = built and healthy and loaded and not problems
    # The image has no installer to write the 1.4.1 receipt. That stops Quasar itself, so it says nothing about the plugin.
    blocked = built and any('require a completed' in p for p in problems)
    state = 'unable to verify' if blocked else 'pass' if ok else 'fail'
    return {'version': version, 'resolved': resolved, 'status': state, 'built': built,
            'warnings': int(warnings.group(1)) if warnings else None, 'healthy': healthy, 'served': served,
            'seconds': int(time.time() - start), 'problems': problems, 'releaseJsonWorkaround': workaround}


results = [test(v) for v in VERSIONS]
receipt = os.path.join(data, 'Updates', 'migration-1.4.1.json')
for r in results:
    print({k: v for k, v in r.items() if k != 'problems'})
    for p in r['problems'][:15]:
        print('  ' + p)
print('1.4.1 receipt written:', os.path.exists(receipt))
ok = all(r['status'] == 'pass' for r in results) or (any(r['status'] == 'pass' for r in results) and not any(r['status'] == 'fail' for r in results)) and not any(r['status'] == 'fail' for r in results)
if result_path:
    json.dump({'status': 'pass' if ok else 'fail', 'versions': results, 'notRun': ['managed server with the companion plugin']},
              open(result_path, 'w', encoding='utf-8'), indent=1)
sys.exit(0 if ok else 1)
