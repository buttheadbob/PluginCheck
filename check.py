"""Runs the plugin checks in containers and writes one results file. See README.md.

usage: python check.py <target> [--only manifest,scan,...] [--skip torch,...] [--keep] [--hold] [--refresh]
       python check.py <target> --pre                    the pre-container lane's half of a run (README, Pre-container lane)
       python check.py <target> --continue <run folder>  the rest of that run: behaviour and logcheck
"""
import argparse, atexit, contextlib, datetime, glob, hashlib, json, os, shutil, signal, subprocess, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, 'stages'))
from display import Display, STATUS_MARK, clock
import classification, githubtoken, leases, mountdepth, prerun
import pin

STAGES = {
    # Need no server at all.
    'manifest': 'stages/manifest.py',
    'scan': 'stages/scan.py',
    'policy': 'stages/policy.py',
    'similar': 'stages/similar.py',
    'copies': 'stages/copies.py',
    'dependencies': 'stages/dependencies.py',
    'inherited': 'stages/inherited.py',
    # Read the code against the game's assemblies, with no server running.
    'lint': 'stages/lint.py',
    'reflection': 'stages/reflection.py',
    'threads': 'stages/threads.py',
    'commands': 'stages/commands.py',
    'nulls': 'stages/nulls.py',
    'storage': 'stages/storage.py',
    'hostpackages': 'stages/hostpackages.py',
    # Run a real server. Magnetar alone, Quasar and Torch must never share data or a server install.
    'magnetar': 'stages/magnetar.py',
    'legacy': 'stages/legacy.py',
    'regcheck': 'stages/regcheck.py',
    'quasar': 'stages/quasar.py',
    'quasar_managed': 'stages/quasar_managed.py',
    'torch': 'stages/torch.py',
    'behaviour': 'stages/behaviour.py',
    # Reads the logs of everything above. Its data folders are still there at this point.
    'logcheck': 'stages/logcheck.py',
}
# What each stage needs finished first. Anything else runs side by side, so stages are only ordered where one really uses another's work:
# a plugin the loader built, or a dedicated server the stage installs into its own volume.
DEPENDS = {
    'regcheck': ['magnetar'],
    'quasar_managed': ['quasar'],
}
# Anything that can be checked without a running server goes first, so a problem they find costs no server time.
HOST_STAGES = ('manifest', 'scan', 'policy', 'similar', 'copies', 'dependencies', 'inherited')
# These read the game's assemblies, so they need a server installed, and that is all they need.
CODE_STAGES = ('lint', 'reflection', 'threads', 'commands', 'nulls', 'storage', 'hostpackages')
# Answer only from the plugin's files, the game and PluginCheck's own code, so the same inputs give the same result. dependencies is not here,
# because new advisories appear with no change to the plugin.
REUSABLE = ('manifest', 'scan', 'policy', 'lint', 'reflection', 'threads', 'commands', 'nulls', 'storage', 'hostpackages')
# The tool folder behind each stage, which counts as part of what it is.
STAGE_TOOLS = {'reflection': ['reflectcheck'], 'threads': ['threadcheck', 'threadcatalog'], 'commands': ['commandcheck'], 'nulls': ['nullcheck'], 'storage': ['storagecheck']}
SERVER_STAGES = ('magnetar', 'legacy', 'quasar', 'quasar_managed', 'torch')
# Not worth starting once lint has found a compile error.
STOPPED_BY_LINT = (*SERVER_STAGES, 'regcheck', 'behaviour')
# Takes every core, pinned, so anything beside it would disturb the timing it measures.
ALONE = 'behaviour'
# Reads every log the others wrote.
LAST = 'logcheck'
# The stages that never start a container. Every other stage waits at its start while an exclusive request waits or holds the machine
# (leases.RunHold), and counts as running until it ends. tests/test_hold.py fails when a stage listed here mentions docker.
NO_DOCKER = ('manifest', 'scan', 'policy', 'similar', 'copies', 'inherited', LAST)
# Each stage runs in a process group of its own, so a Ctrl-C typed at the console reaches this script alone. It sends the stage INTERRUPT and
# gives it STOP_SECONDS to clean up, such as behaviour removing its servers, before it ends it. A typed Ctrl-Break reaches every process on the
# console whatever its group, so the stage gets that one at once too.
GROUP = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
INTERRUPT = signal.CTRL_BREAK_EVENT if os.name == 'nt' else signal.SIGINT
STOP_SECONDS = 60
INTERRUPTED = 'the run was interrupted while this stage ran'
BEFORE_START = 'the run was interrupted before this stage started.'
# A stage that ended with no result while the run was being interrupted waits this long for the main thread to set stopping (run_stage).
NOTICE_SECONDS = 2
NEEDS_DIR = {'similar', 'copies', 'magnetar', 'legacy', 'regcheck', 'quasar', 'quasar_managed', 'torch', 'behaviour', 'inherited', 'reflection', 'dependencies', 'threads', 'commands', 'nulls', 'storage', 'hostpackages', 'lint', 'logcheck'}

parser = argparse.ArgumentParser()
parser.add_argument('target')
parser.add_argument('--only', default='')
parser.add_argument('--skip', default='')
parser.add_argument('--keep', action='store_true', help='keep run data such as worlds and source copies')
parser.add_argument('--refresh', action='store_true', help='ignore what earlier runs kept: results, the thread catalog and the source of other plugins')
parser.add_argument('--hold', action='store_true', help='leave the managed Quasar running afterwards, on 127.0.0.1:18080')
parser.add_argument('--pre', action='store_true', help="every stage before behaviour, and behaviour's discovery, on one slot of PluginCheck's own")
parser.add_argument('--continue', dest='resume', metavar='RUN_DIR', help='behaviour and logcheck, in the folder of a run started with --pre')
args = parser.parse_args()
if (args.pre or args.resume) and (args.pre and args.resume or args.only or args.skip or args.hold):
    parser.error('--pre and --continue are the two halves of one whole run, so they take no --only, --skip or --hold, nor each other')

target = json.load(open(os.path.join(HERE, 'targets', args.target + '.json'), encoding='utf-8'))
# Targets name their repo relative to this folder, so they work wherever the folders are cloned side by side.
target['repo'] = os.path.normpath(os.path.join(HERE, target['repo']))
if not os.path.isdir(target['repo']):
    sys.exit(f"{target['repo']} does not exist. Clone the plugin next to PluginCheck.")
if args.resume:
    run_dir = os.path.abspath(args.resume)
    if not os.path.isfile(os.path.join(run_dir, 'results.json')):
        sys.exit(f'{run_dir} is not a run folder.')
else:
    run_dir = os.path.join(HERE, 'runs', target['name'], datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
# A folder too deep for Docker Desktop's mounts makes every Legacy and Torch server exit at start (stages/mountdepth.py).
too_deep = mountdepth.problem(run_dir)
if too_deep:
    sys.exit(too_deep)
if not args.resume:
    os.makedirs(run_dir)
target_file = os.path.join(run_dir, 'target.json')
if not args.resume:
    json.dump(target, open(target_file, 'w', encoding='utf-8'), indent=1)

# The pre-container lane runs on one slot of PluginCheck's own, held as a lease, and every container it starts is pinned to it (stages/pin.py).
lane = None
if args.pre:
    lane = leases.Lane(leases.machine_cores(), say=lambda text: print(text, flush=True))
    print(f'waiting for the pre-container lane\'s slot ({leases.LANE})...', flush=True)
    os.environ['PLUGINCHECK_CPUSET'] = lane.acquire()
    # Also on an early exit, so the slot never waits out the 5-minute lease limit.
    atexit.register(lane.release)
    print(f"the lane holds cores {os.environ['PLUGINCHECK_CPUSET']}", flush=True)
# A full run (and the --continue half) registers for its whole life, and holds still at each stage boundary while an exclusive request
# (a measurement that needs a quiet machine) waits or holds the machine. The lane's half is a lease of its own and steps aside itself.
hold = None
if not args.pre:
    hold = leases.RunHold(target['name'], say=lambda text: print(text, flush=True))
    hold.start()
    atexit.register(hold.close)
if args.hold:
    os.environ['PLUGINCHECK_HOLD'] = '1'
if args.refresh:
    os.environ['PLUGINCHECK_REFRESH'] = '1'

only = {s for s in args.only.split(',') if s}
skip = {s for s in args.skip.split(',') if s}
results = {'target': target['name'], 'started': datetime.datetime.now().isoformat(timespec='seconds'), 'stages': {}}
if args.resume:
    # The first half's stages are this run's own, done already. Behaviour's discovery-only result gives way to the whole stage.
    results = json.load(open(os.path.join(run_dir, 'results.json'), encoding='utf-8'))
    results['stages'] = {s: o for s, o in results['stages'].items() if s not in (ALONE, LAST)}
    results['continued'] = datetime.datetime.now().isoformat(timespec='seconds')


def needed(stage):
    # Quasar renders a Magnetar plugin's settings with no UI plugin at all, so a Magnetar build is enough to run under it.
    if stage == 'quasar_managed':
        return 'quasar' in target or 'magnetar' in target
    if stage in ('magnetar', 'legacy', 'regcheck', 'quasar', 'torch') and stage.replace('regcheck', 'magnetar').replace('legacy', 'magnetar') not in target:
        return False
    if stage == 'inherited' and 'upstream' not in target:
        return False
    if stage == 'behaviour' and 'magnetar' not in target and 'torch' not in target:
        return False
    return True


def past_seconds():
    """How long each stage took the last time it ran for this plugin, which is the best estimate there is."""
    seconds = {}
    for result in sorted(glob.glob(os.path.join(HERE, 'runs', target['name'], '*', 'results.json')), reverse=True):
        try:
            stages = json.load(open(result, encoding='utf-8'))['stages']
        except (OSError, ValueError, KeyError):
            continue
        for name, outcome in stages.items():
            # A run cut down while working on a stage says nothing about how long the whole stage takes.
            if 'seconds' in outcome and not outcome.get('filtered'):
                seconds.setdefault(name, outcome['seconds'])
    return seconds


past = past_seconds()
planned = [s for s in STAGES if needed(s) and not ((only and s not in only) or s in skip)]
if args.pre:
    planned = [s for s in planned if s != LAST]
elif args.resume:
    planned = [s for s in planned if s in (ALONE, LAST)]
results_path = os.path.join(run_dir, 'results.json')
run_started = time.time()
lock = threading.Lock()
begun, finished_stages = {}, set(results['stages'])
stopping, processes = threading.Event(), {}
# Ctrl-Break, like Ctrl-C, ends the run through KeyboardInterrupt below.
if hasattr(signal, 'SIGBREAK'):
    signal.signal(signal.SIGBREAK, signal.default_int_handler)


def game_build():
    """A hash of the game's main assembly, or None before anything has installed it. On a first run the code stages have nothing to read."""
    with hold.stage('game build') if hold else contextlib.nullcontext():
        found = subprocess.run(['docker', 'run', *pin.args(), '--rm', '-v', 'plugincheck-ds-magnetar:/ds:ro', '--entrypoint', 'sha256sum', 'mcr.microsoft.com/dotnet/sdk:10.0',
                                '/ds/DedicatedServer64/Sandbox.Game.dll'], capture_output=True, text=True).stdout.split()
    return found[0][:16] if found else None


game = game_build()
code_first = game is not None
plugin_files = classification.folder_hash(target['repo'])
inputs = {'source': plugin_files, 'tools': prerun.tools_hash(HERE), 'game': game, 'target': prerun.target_hash(target)}
if args.resume:
    pre_path = os.path.join(run_dir, 'pre.json')
    pre = json.load(open(pre_path, encoding='utf-8')) if os.path.isfile(pre_path) else None
    refused = prerun.problems(pre, inputs)
    if refused:
        sys.exit(f'{run_dir} cannot be continued: ' + ' '.join(refused))


def needs(stage):
    """The stages that must finish before this one, among those in this run."""
    if stage == LAST:
        return [s for s in planned if s != LAST]
    if stage == ALONE:
        return [s for s in planned if s not in (ALONE, LAST)]
    wanted = list(DEPENDS.get(stage, []))
    if stage in CODE_STAGES and not code_first:
        # A first run has no game assemblies yet, so these wait for a server stage to install them.
        wanted += ['magnetar', 'torch', 'quasar_managed'] if stage == 'reflection' else ['magnetar']
    if stage in SERVER_STAGES:
        wanted += HOST_STAGES
        if code_first:
            wanted += CODE_STAGES
    return [s for s in wanted if s in planned]


after = {s: [c for c in planned if s in needs(c)] for s in planned}


def seconds_from(stage):
    """How long until everything that hangs off this stage is done. Side by side stages count once, by the longest path."""
    if stage in finished_stages:
        own = 0
    else:
        own = max(past.get(stage, 0) - (time.time() - begun.get(stage, time.time())), 0)
    return own + max((seconds_from(c) for c in after[stage]), default=0)


def time_left():
    return max((seconds_from(s) for s in planned if s not in finished_stages), default=0)


display = Display(target['name'], planned, past, time_left, run_dir)
refused = githubtoken.check()
if refused:
    display.line('WARN  ' + refused)


def left_text():
    unknown = sum(1 for s in planned if s not in past and s not in finished_stages)
    return f'{clock(time_left())} left' + (f', plus {unknown} stages with no past timing' if unknown else '')


def heartbeat(stage, started, last_line, finished):
    """Says a stage is alive when it has gone quiet, so silence never has to be guessed at."""
    while not finished.wait(30):
        if time.time() - last_line[0] >= 60:
            display.heartbeat(f'  ... {stage} still working, {clock(time.time() - started)} so far, about {left_text()}')
            last_line[0] = time.time()


def stage_key(stage):
    """Everything a stage's answer depends on. The commands stage also reports whether a current reading of the commands exists."""
    if stage not in REUSABLE or not game or args.refresh:
        return None
    parts = [stage, plugin_files, game]
    script = open(os.path.join(HERE, STAGES[stage]), 'rb').read()
    parts.append(hashlib.sha256(script).hexdigest())
    parts += [classification.folder_hash(os.path.join(HERE, 'stages', tool)) for tool in STAGE_TOOLS.get(stage, [])]
    parts.append(open(os.path.join(HERE, 'stages', 'classification.py'), encoding='utf-8').read() if stage == 'commands' else '')
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:24]


def stage_files(stage, key):
    folder = os.path.join(HERE, 'cache', 'stages', target['name'])
    return os.path.join(folder, f'{stage}-{key}.json'), os.path.join(folder, f'{stage}-{key}.log')


def reuse_stage(stage, key):
    """Same inputs, same answer. Its lines are shown again, so a reused result reads like any other, and the operator still judges every lead."""
    kept_result, kept_log = stage_files(stage, key)
    if not (key and os.path.exists(kept_result) and os.path.exists(kept_log)):
        return None
    shutil.copy(kept_result, os.path.join(run_dir, stage + '.json'))
    shutil.copy(kept_log, os.path.join(run_dir, stage + '.log'))
    for line in open(kept_log, encoding='utf-8'):
        display.line(line, stage)
    outcome = json.load(open(kept_result, encoding='utf-8'))
    outcome['seconds'], outcome['reused'] = 0, True
    return outcome


def remember_stage(stage, key, outcome):
    if key and outcome['status'] != 'not run':
        kept_result, kept_log = stage_files(stage, key)
        os.makedirs(os.path.dirname(kept_result), exist_ok=True)
        shutil.copy(os.path.join(run_dir, stage + '.json'), kept_result)
        shutil.copy(os.path.join(run_dir, stage + '.log'), kept_log)


def run_stage(stage):
    script = STAGES[stage]
    begun[stage] = time.time()
    key = stage_key(stage)
    display.stage_started(stage, f"===== {stage} ===== {clock(time.time() - run_started)} so far, about {left_text()}")
    reused = reuse_stage(stage, key)
    if reused:
        with lock:
            results['stages'][stage] = reused
            finished_stages.add(stage)
        display.stage_finished(stage, reused['status'], f"{stage}: {reused['status']}, reused because nothing it reads has changed. About {left_text()}.")
        return
    result_file = os.path.join(run_dir, stage + '.json')
    command = [sys.executable, '-I', '-X', 'utf8', os.path.join(HERE, script), target_file]
    if stage in NEEDS_DIR:
        command.append(run_dir)
    command.append(result_file)

    env = {**os.environ, 'PYTHONUNBUFFERED': '1', 'PLUGINCHECK_SECONDS_AFTER': str(int(max((seconds_from(c) for c in after[stage]), default=0)))}
    # A Docker stage of a full run waits here while an exclusive request waits or holds, and counts as running until it ends. In the
    # lane, one stage at a time on its slot, and behaviour only discovers.
    gate = hold.stage(stage) if hold and stage not in NO_DOCKER else contextlib.nullcontext()
    with gate, (lane.stage() if lane else contextlib.nullcontext()) as cores, open(os.path.join(run_dir, stage + '.log'), 'w', encoding='utf-8') as log:
        # Timed from here, so a stage that waited for the lane's slot does not count the wait as its own time.
        start = time.time()
        if lane:
            env['PLUGINCHECK_CPUSET'] = cores
            if stage == ALONE:
                env['PLUGINCHECK_BEHAVIOUR_DISCOVER_ONLY'] = '1'
        # Started under the lock stop_stages sets stopping under, so a stage either starts before that and is interrupted with the others,
        # or does not start at all, such as one let through the hold's gate by the run's end.
        with lock:
            late = stopping.is_set()
            if not late:
                # Unbuffered, so a stage's lines arrive as they happen and its log can be followed while it runs.
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', env=env,
                                           **GROUP)
                processes[stage] = process
        if late:
            with lock:
                results['stages'][stage] = {'status': 'not run', 'reason': BEFORE_START}
                finished_stages.add(stage)
            display.stage_finished(stage, 'not run', f'{stage}: not run. {BEFORE_START}')
            return
        last_line, finished = [time.time()], threading.Event()
        threading.Thread(target=heartbeat, args=(stage, start, last_line, finished), daemon=True).start()
        for line in process.stdout:
            last_line[0] = time.time()
            if line.startswith(STATUS_MARK):
                # A stage that counts its own work gives a better estimate than the last run did.
                event = display.status(line)
                if event and event.get('kind') == 'progress':
                    past[stage] = int(time.time() - start) + event['left']
                continue
            display.line(line, stage)
            log.write(line)
            log.flush()
        process.wait()
        finished.set()

    seconds = int(time.time() - start)
    if not stopping.is_set() and not os.path.exists(result_file):
        # A typed Ctrl-Break reaches the stage as well as this script, and a stage can end on it before the main thread, which sees it
        # within one 0.5 s join step (wait_for), has set stopping.
        stopping.wait(NOTICE_SECONDS)
    if stopping.is_set():
        # No seconds: a stage cut short says nothing about how long it takes (past_seconds).
        outcome = {'status': 'not run', 'reason': INTERRUPTED}
    elif os.path.exists(result_file):
        outcome = json.load(open(result_file, encoding='utf-8'))
        outcome['seconds'] = seconds
    else:
        outcome = {'status': 'not run', 'reason': f'the stage stopped with exit code {process.returncode} and wrote no result', 'seconds': seconds}
    remember_stage(stage, key, outcome)
    with lock:
        results['stages'][stage] = outcome
        finished_stages.add(stage)
        # Written after every stage, so finished results can be read while the rest runs.
        json.dump(results, open(results_path, 'w', encoding='utf-8'), indent=1)
    display.stage_finished(stage, outcome['status'], f"{stage}: {outcome['status']} in {clock(seconds)}. About {left_text()}.")


def interrupt(process):
    try:
        process.send_signal(INTERRUPT)
    except OSError:
        # No console to send it through.
        process.kill()


def stop_stages():
    """Ctrl-C or Ctrl-Break: each running stage is interrupted and given STOP_SECONDS to clean up, then ended. No stage starts after this."""
    # A further Ctrl-C or Ctrl-Break is ignored from here on: it would cut short the stages' clean-up, such as behaviour removing its servers,
    # and the writing of the results.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, signal.SIG_IGN)
    with lock:
        stopping.set()
        running = [p for p in processes.values() if p.poll() is None]
    display.line(f'interrupted: stopping {len(running)} running stages, giving them up to {STOP_SECONDS}s to clean up')
    for process in running:
        interrupt(process)
    deadline = time.time() + STOP_SECONDS
    while time.time() < deadline and any(p.poll() is None for p in running):
        time.sleep(0.5)
    for process in running:
        if process.poll() is None:
            process.kill()


def wait_for(threads, seconds=None):
    """Thread.join with no timeout cannot be interrupted on Windows: Ctrl-C and Ctrl-Break would wait for the whole run. So it joins in short steps."""
    end = time.time() + seconds if seconds else None
    for thread in threads:
        while thread.is_alive() and (end is None or time.time() < end):
            thread.join(0.5)


gates = {s: threading.Event() for s in planned}


def when_ready(stage):
    for needed_first in needs(stage):
        gates[needed_first].wait()
    try:
        lint = results['stages'].get('lint', {})
        reason = None
        if stopping.is_set():
            reason = BEFORE_START
        elif stage in STOPPED_BY_LINT and lint.get('status') == 'fail':
            reason = 'lint found compile errors, so no server was started. Fix those first.'
        if reason:
            begun[stage] = time.time()
            display.stage_started(stage, f'===== {stage} ===== skipped')
            with lock:
                results['stages'][stage] = {'status': 'not run', 'reason': reason}
                finished_stages.add(stage)
            display.stage_finished(stage, 'not run', f'{stage}: not run. {reason}')
        else:
            run_stage(stage)
    finally:
        gates[stage].set()


for stage in STAGES:
    if stage in results['stages']:
        continue
    if not needed(stage):
        results['stages'][stage] = {'status': 'not present'}
    elif stage not in planned:
        results['stages'][stage] = {'status': 'not run'}

try:
    workers = [threading.Thread(target=when_ready, args=(s,), daemon=True) for s in planned]
    for worker in workers:
        worker.start()
    try:
        wait_for(workers)
    except KeyboardInterrupt:
        stop_stages()
        # Each stopped stage records itself, and each one not started yet that it did not run. A stage whose output is still held open
        # by something it started is recorded here.
        wait_for(workers, STOP_SECONDS)
        with lock:
            for stage in planned:
                results['stages'].setdefault(stage, {'status': 'not run', 'reason': INTERRUPTED})
finally:
    # The lane's slot goes back to the run that lent it, or to the machine, as soon as its half is done. A waiting exclusive request is
    # granted as soon as the run's last stage is over, not after the clean-up.
    if lane:
        lane.release()
    if hold:
        hold.close()
results['stages'] = {s: results['stages'][s] for s in STAGES if s in results['stages']}

path = results_path
json.dump(results, open(path, 'w', encoding='utf-8'), indent=1)
if args.pre:
    pre = prerun.record(results['stages'], classification.load(target['name'], target['repo']) is not None, inputs)
    json.dump(pre, open(os.path.join(run_dir, 'pre.json'), 'w', encoding='utf-8'), indent=1)
    display.line(f"pre-container lane: {pre['status']}" + (': ' + '; '.join(pre['reasons']) if pre['reasons'] else
                 f'. Continue it with: python check.py {args.target} --continue "{run_dir}"'))

# Each stage keeps its own logs. These folders are server installs and source copies, and add up to gigabytes per run.
# The lane's half keeps them, since behaviour and logcheck read them when the run continues.
if not args.keep and not args.hold and not args.pre:
    for folder in ('data', 'source', 'quasar-data', 'quasar-source', 'upstream', 'regcheck', 'torch-data', 'torch-plugins', 'managed', 'legacy', 'behaviour', 'reflection', 'dependencies', 'threads', 'commands', 'nulls', 'storage', 'lint'):
        shutil.rmtree(os.path.join(run_dir, folder), ignore_errors=True)

display.close()
print('\n===== summary =====')
display.summary(results['stages'], path)
print('AI operator: follow Agent.md. Read the code behind every lead and give each one a verdict. Never report a guess.')
sys.exit(1 if stopping.is_set() or any(o['status'] == 'fail' for o in results['stages'].values()) else 0)
