"""Registers every command module of the loader-built plugin on its own.

usage: regcheck.py <target.json> <run dir> [results.json]

Magnetar's RegisterAssembly stops at the first exception, so its log hides every failure after the first, and it
overwrites a repeated path without saying so. This runs after the Magnetar stage and tests the plugin.dll that the loader
compiled there, with the PluginSdk from the same image and the dedicated server from Magnetar's own volume.
"""
import glob, json, os, re, shutil, subprocess, sys

MAGNETAR = 'ghcr.io/cometworks/magnetar:latest'
SDK = 'mcr.microsoft.com/dotnet/sdk:10.0'

target = json.load(open(sys.argv[1], encoding='utf-8'))
run_dir = os.path.abspath(sys.argv[2])
result_path = sys.argv[3] if len(sys.argv) > 3 else None
work = os.path.join(run_dir, 'regcheck')
here = os.path.dirname(os.path.abspath(__file__))


def docker(*args):
    return subprocess.run(['docker', *args], capture_output=True, text=True, encoding='utf-8', errors='replace')


def finish(status, **fields):
    if result_path:
        json.dump({'status': status, **fields}, open(result_path, 'w', encoding='utf-8'), indent=1)
    sys.exit(0 if status == 'pass' else 1)


def why_no_plugin():
    """Why the magnetar stage left no plugin.dll to test, in its own words where it gave any."""
    path = os.path.join(run_dir, 'magnetar.json')
    if not os.path.exists(path):
        return 'the magnetar stage did not run, and this stage tests the plugin it builds'
    stage = json.load(open(path, encoding='utf-8'))
    if stage['status'] == 'pass':
        return 'the magnetar stage passed but left no plugin.dll in its DevFolder'
    detail = stage.get('reason') or '; '.join(p['text'] for p in stage.get('problems', [])[:3])
    return f"the magnetar stage ended as {stage['status']}, so there is no plugin.dll to test" + (f': {detail}' if detail else '')


built = glob.glob(os.path.join(run_dir, 'data', 'Magnetar', 'DevFolder', '*', 'Bin', 'plugin.dll'))
if not built:
    reason = why_no_plugin()
    print(reason)
    finish('not run', reason=reason)

os.makedirs(work, exist_ok=True)
shutil.copytree(os.path.join(here, 'regcheck'), os.path.join(work, 'src'), dirs_exist_ok=True)
shutil.copy(built[0], os.path.join(work, 'plugin.dll'))

name = f'plugincheck-sdkcopy-{os.getpid()}'
docker('create', '--name', name, MAGNETAR)
copied = docker('cp', f'{name}:/opt/magnetar/Libraries/MagnetarInterim/PluginSdk.dll', os.path.join(work, 'PluginSdk.dll'))
# Reading a type's attributes loads every attribute on it, and a [HarmonyPatch] class needs Harmony for that. The host provides it to a plugin.
harmony = docker('cp', f'{name}:/opt/magnetar/Libraries/MagnetarInterim/0Harmony.dll', os.path.join(work, '0Harmony.dll'))
docker('rm', '-v', name)
if copied.returncode or harmony.returncode:
    print(copied.stderr + harmony.stderr)
    finish('not run', reason='could not copy PluginSdk.dll and 0Harmony.dll from the Magnetar image')

command = ('set -e; cd /work/src; '
           'dotnet build -c Release -v q -nologo -o /work/out -p:PluginSdkPath=/work/PluginSdk.dll >/work/build.log 2>&1; '
           'dotnet /work/out/regcheck.dll /work/plugin.dll /ds/DedicatedServer64')
r = docker('run', '--rm', '--cpus', '4', '--memory', '4g', '--network', 'none' if os.path.isdir(os.path.join(work, 'out')) else 'bridge',
           '-v', f'{work}:/work', '-v', 'plugincheck-ds-magnetar:/ds:ro', '--entrypoint', 'bash', SDK, '-c', command)
output = r.stdout + r.stderr
print(output.strip())
summary = re.search(r'(\d+) ok, (\d+) failed', output)
if not summary:
    finish('not run', reason='the check did not finish', output=output[-1500:])
failed = re.findall(r'^FAIL (.+)$', output, re.M)
dupes = re.findall(r'^DUP (.+)$', output, re.M)
commands = re.search(r'commands: (\d+)', output)
finish('fail' if failed or dupes else 'pass', modules=int(summary.group(1)) + int(summary.group(2)), failed=failed, duplicates=dupes,
       commands=int(commands.group(1)) if commands else None)
