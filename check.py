"""Runs the plugin checks in containers and writes one results file. See README.md.

usage: python check.py <target> [--only manifest,scan,...] [--skip torch,...] [--keep] [--hold] [--refresh]
"""
import argparse, datetime, json, os, shutil, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
# Each stage is separate on purpose: Magnetar alone, Quasar and Torch must never share data or a server install.
STAGES = {
    'manifest': 'stages/manifest.py',
    'scan': 'stages/scan.py',
    'policy': 'stages/policy.py',
    'similar': 'stages/similar.py',
    'copies': 'stages/copies.py',
    'dependencies': 'stages/dependencies.py',
    'inherited': 'stages/inherited.py',
    'magnetar': 'stages/magnetar.py',
    'legacy': 'stages/legacy.py',
    'regcheck': 'stages/regcheck.py',
    'quasar': 'stages/quasar.py',
    'quasar_managed': 'stages/quasar_managed.py',
    'torch': 'stages/torch.py',
    # Last, so a first run has a dedicated server to check against.
    'reflection': 'stages/reflection.py',
    'threads': 'stages/threads.py',
    'nulls': 'stages/nulls.py',
    'hostpackages': 'stages/hostpackages.py',
    'lint': 'stages/lint.py',
    # Very last, so it reads the logs of everything above. Its data folders are still there at this point.
    'logcheck': 'stages/logcheck.py',
}
NEEDS_DIR = {'similar', 'copies', 'magnetar', 'legacy', 'regcheck', 'quasar', 'quasar_managed', 'torch', 'inherited', 'reflection', 'dependencies', 'threads', 'nulls', 'hostpackages', 'lint', 'logcheck'}

parser = argparse.ArgumentParser()
parser.add_argument('target')
parser.add_argument('--only', default='')
parser.add_argument('--skip', default='')
parser.add_argument('--keep', action='store_true', help='keep run data such as worlds and source copies')
parser.add_argument('--refresh', action='store_true', help='download the sources in cache/sources again')
parser.add_argument('--hold', action='store_true', help='leave the managed Quasar running afterwards, on 127.0.0.1:18080')
args = parser.parse_args()

target = json.load(open(os.path.join(HERE, 'targets', args.target + '.json'), encoding='utf-8'))
# Targets name their repo relative to this folder, so they work wherever the folders are cloned side by side.
target['repo'] = os.path.normpath(os.path.join(HERE, target['repo']))
if not os.path.isdir(target['repo']):
    sys.exit(f"{target['repo']} does not exist. Clone the plugin next to PluginCheck.")
run_dir = os.path.join(HERE, 'runs', target['name'], datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
os.makedirs(run_dir)
target_file = os.path.join(run_dir, 'target.json')
json.dump(target, open(target_file, 'w', encoding='utf-8'), indent=1)
if args.hold:
    os.environ['PLUGINCHECK_HOLD'] = '1'
if args.refresh:
    os.environ['PLUGINCHECK_REFRESH'] = '1'

only = {s for s in args.only.split(',') if s}
skip = {s for s in args.skip.split(',') if s}
results = {'target': target['name'], 'started': datetime.datetime.now().isoformat(timespec='seconds'), 'stages': {}}


def needed(stage):
    if stage in ('magnetar', 'legacy', 'regcheck', 'quasar', 'quasar_managed', 'torch') and stage.replace('regcheck', 'magnetar').replace('legacy', 'magnetar').replace('_managed', '') not in target:
        return False
    if stage == 'inherited' and 'upstream' not in target:
        return False
    return True


for stage, script in STAGES.items():
    if not needed(stage):
        results['stages'][stage] = {'status': 'not present'}
        continue
    if (only and stage not in only) or stage in skip:
        results['stages'][stage] = {'status': 'not run'}
        continue

    print(f'\n===== {stage} =====', flush=True)
    result_file = os.path.join(run_dir, stage + '.json')
    command = [sys.executable, '-I', os.path.join(HERE, script), target_file]
    if stage in NEEDS_DIR:
        command.append(run_dir)
    command.append(result_file)

    start = time.time()
    with open(os.path.join(run_dir, stage + '.log'), 'w', encoding='utf-8') as log:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
        for line in process.stdout:
            print(line, end='', flush=True)
            log.write(line)
        process.wait()

    if os.path.exists(result_file):
        outcome = json.load(open(result_file, encoding='utf-8'))
    else:
        outcome = {'status': 'not run', 'reason': f'the stage stopped with exit code {process.returncode} and wrote no result'}
    outcome['seconds'] = int(time.time() - start)
    results['stages'][stage] = outcome

path = os.path.join(run_dir, 'results.json')
json.dump(results, open(path, 'w', encoding='utf-8'), indent=1)

# Each stage keeps its own logs. These folders are server installs and source copies, and add up to gigabytes per run.
if not args.keep and not args.hold:
    for folder in ('data', 'source', 'quasar-data', 'quasar-source', 'upstream', 'regcheck', 'torch-data', 'torch-plugins', 'managed', 'legacy', 'reflection', 'dependencies', 'threads', 'nulls', 'lint'):
        shutil.rmtree(os.path.join(run_dir, folder), ignore_errors=True)

print('\n===== summary =====')
for stage, outcome in results['stages'].items():
    print(f"{stage:10} {outcome['status']}")
print(path)
sys.exit(1 if any(o['status'] == 'fail' for o in results['stages'].values()) else 0)
