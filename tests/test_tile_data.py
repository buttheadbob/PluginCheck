"""What a machine view reads while a run goes: python -I -m unittest discover -s tests

The behaviour stage's owner entry in leases.json names its run folder, stage and progress while the owner runs (owner.run, owner.stage,
owner.progress), and they go with it: at the normal end, on an interrupt, on a crash, and when a stale owner is found. Runs go to
<runs>\\<target>\\<stamp>, under PLUGINCHECK_RUNS_DIR when set, each in a new folder. The lease folder is PLUGINCHECK_MACHINE_DIR, else the
one machine-dir.txt names, else the checkout's own. No test here asks Docker: each Docker call is replaced, and the replacement records that it was called.
"""
import ast, datetime, io, json, os, re, shutil, subprocess, sys, tempfile, types, unittest
from unittest import mock
import scratchmachine  # noqa: F401  (before anything of PluginCheck's: keeps every test off the real lease folders)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
STAGES = os.path.join(ROOT, 'stages')
sys.path.insert(0, STAGES)
sys.path.insert(0, ROOT)
import leases, runfolders  # noqa: E402
import lease  # noqa: E402

NO_WINDOW = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
STAMP = re.compile(r'\A[0-9]{8}-[0-9]{6}\Z')
CORES = ['0-1', '2-3']
PROGRESS = {'done': 3, 'total': 40, 'left': 600, 'overall': 900, 'failed': 0}
# The fields a machine view reads from the owner entry.
FIELDS = ('run', 'stage', 'progress')
# Stands in for Docker in a child process: every docker command is recorded and fails, as with no Docker at all; nothing else changes.
DOCKER_SPY = r'''
import json, os, subprocess
_run, _popen = subprocess.run, subprocess.Popen


def _docker(args):
    return isinstance(args, (list, tuple)) and args and os.path.basename(str(args[0])).lower() in ('docker', 'docker.exe')


def run(args, *rest, **options):
    if _docker(args):
        with open(os.environ['DOCKER_SPY_CALLS'], 'a', encoding='utf-8') as f:
            f.write(json.dumps(list(args)) + '\n')
        return subprocess.CompletedProcess(args, 1, '', 'docker is replaced in this test')
    return _run(args, *rest, **options)


def popen(args, *rest, **options):
    if _docker(args):
        raise OSError('docker is replaced in this test')
    return _popen(args, *rest, **options)


subprocess.run, subprocess.Popen = run, popen
'''


def read(folder):
    with open(os.path.join(folder, 'leases.json'), encoding='utf-8') as f:
        return json.load(f)


def write(folder, state):
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, 'leases.json'), 'w', encoding='utf-8') as f:
        json.dump(state, f)


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class NoDocker:
    """names() for an Owner: the running containers, recorded each time it is asked."""

    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return set()


class Folder(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix='pc-tile-')
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.run = os.path.join(self.folder, 'runs', 'Demo', '20261115-101500')
        os.makedirs(self.run)
        self.clock, self.names = Clock(), NoDocker()

    def owner(self, run=None):
        return leases.Owner(CORES, folder=self.folder, names=self.names, clock=self.clock, run=self.run if run is None else run)

    def entry(self):
        return read(self.folder)['owner']


class OwnerEntryTests(Folder):
    def test_the_run_folder_and_stage_are_written_when_the_owner_starts(self):
        self.owner().start()
        entry = self.entry()
        self.assertEqual((entry['run'], entry['stage'], entry['running'], entry['pid']), (self.run, 'behaviour', True, os.getpid()))
        self.assertNotIn('progress', entry, 'no progress before the first step')
        self.assertGreater(self.names.calls, 0, 'the stand-in for Docker was asked')

    def test_a_progress_step_is_written_at_once_without_waiting_for_a_heartbeat(self):
        owner = self.owner()
        owner.start()
        self.clock.now = 1003
        self.assertTrue(owner.report(progress=PROGRESS))
        entry = self.entry()
        self.assertEqual(entry['progress'], PROGRESS)
        self.assertEqual(entry['heartbeat'], 1003, 'a step is also a heartbeat')
        self.assertEqual(entry['run'], self.run)

    def test_a_stage_change_is_written_at_once(self):
        owner = self.owner()
        owner.start()
        owner.report(progress=PROGRESS)
        self.assertTrue(owner.report(stage='behaviour: alone reruns'))
        entry = self.entry()
        self.assertEqual(entry['stage'], 'behaviour: alone reruns')
        self.assertEqual(entry['progress'], PROGRESS, 'a stage change keeps the progress so far')

    def test_every_heartbeat_carries_them(self):
        owner = self.owner()
        owner.start()
        owner.report(stage='behaviour: servers', progress=PROGRESS)
        # Something else rewrote the entry in between, such as an older copy: the next heartbeat writes them again.
        state = read(self.folder)
        for field in FIELDS:
            state['owner'].pop(field)
        write(self.folder, state)
        self.clock.now = 1005
        owner.beat(0)
        entry = self.entry()
        self.assertEqual((entry['run'], entry['stage'], entry['progress'], entry['heartbeat']), (self.run, 'behaviour: servers', PROGRESS, 1005))

    def test_the_shape_a_machine_view_reads(self):
        """owner.run a string, owner.stage a string, owner.progress an object with numbers done and total > 0; the run an absolute path to a
        <yyyyMMdd-HHmmss> folder whose runs folder is two up."""
        owner = self.owner()
        owner.start()
        owner.report(stage='behaviour: servers', progress=PROGRESS)
        entry = self.entry()
        self.assertEqual(leases.OWNER_RUN_FIELDS, FIELDS)
        self.assertIsInstance(entry['run'], str)
        self.assertTrue(os.path.isabs(entry['run']))
        self.assertRegex(os.path.basename(entry['run']), STAMP)
        self.assertEqual(os.path.dirname(os.path.dirname(entry['run'])), os.path.join(self.folder, 'runs'))
        self.assertIsInstance(entry['stage'], str)
        self.assertLessEqual(len(entry['stage']), 80)
        self.assertIsInstance(entry['progress']['done'], int)
        self.assertGreater(entry['progress']['total'], 0)

    def test_not_written_once_the_owner_has_stopped(self):
        owner = self.owner()
        owner.start()
        owner.stop()
        self.assertFalse(owner.report(stage='behaviour: servers', progress=PROGRESS))
        entry = self.entry()
        self.assertFalse(entry['running'])
        for field in FIELDS:
            self.assertNotIn(field, entry)

    def test_not_written_into_another_owners_entry(self):
        owner = self.owner()
        owner.start()
        state = read(self.folder)
        state['owner'] = {'name': 'plugincheck', 'pid': os.getpid() + 100000, 'heartbeat': 1000, 'running': True, 'servers': 3, 'cores': CORES}
        write(self.folder, state)
        self.assertFalse(owner.report(progress=PROGRESS))
        self.assertNotIn('progress', self.entry())


class StopTests(Folder):
    def test_stop_leaves_another_owners_entry_alone(self):
        owner = self.owner()
        owner.start()
        state = read(self.folder)
        other = {'name': 'plugincheck', 'pid': os.getpid() + 100000, 'heartbeat': 1000, 'running': True, 'servers': 3, 'cores': CORES,
                 'run': self.run, 'stage': 'behaviour', 'progress': PROGRESS}
        state['owner'] = dict(other)
        write(self.folder, state)
        owner.stop()
        self.assertEqual(self.entry(), other)

    def test_an_interrupt_on_the_reporting_thread_does_not_wait_for_ever(self):
        """A POSIX signal handler runs on the stage's main thread, between two steps of whatever it is doing, and behaviour.py's handler
        calls lending.stop(). Fired while that thread is in lending.report, the stop must still go through."""
        import threading
        lending = leases.Lending(lambda: self.owner())
        real = leases.Owner.report

        def handler_fires(owner, stage=None, progress=None):
            lending.stop()  # what interrupted() does, on this same thread
            return real(owner, stage, progress)

        done = threading.Event()

        def main_thread():
            with mock.patch.object(leases.Owner, 'report', handler_fires):
                lending.report(stage='behaviour: servers', progress=PROGRESS)
            done.set()

        thread = threading.Thread(target=main_thread, daemon=True)
        thread.start()
        thread.join(15)
        self.assertTrue(done.is_set(), 'the stop waited on the report under it')
        entry = self.entry()
        self.assertFalse(entry['running'])
        for field in FIELDS:
            self.assertNotIn(field, entry, 'nothing written after the stop')


class ClearedTests(Folder):
    def test_cleared_at_the_normal_end(self):
        lending = leases.Lending(lambda: self.owner())
        lending.report(stage='behaviour: servers', progress=PROGRESS)
        self.assertEqual(self.entry()['progress'], PROGRESS)
        lending.stop()
        entry = self.entry()
        self.assertFalse(entry['running'])
        for field in FIELDS:
            self.assertNotIn(field, entry)

    def test_nothing_written_after_the_end(self):
        lending = leases.Lending(lambda: self.owner())
        lending.stop()
        with mock.patch.object(leases.Owner, 'report', side_effect=AssertionError('reported after stop')):
            lending.report(progress=PROGRESS)
        self.assertNotIn('progress', self.entry())

    def test_a_report_that_fails_never_reaches_the_stage(self):
        said = []
        lending = leases.Lending(lambda: self.owner(), say=said.append)
        with mock.patch.object(leases.Owner, 'report', side_effect=TimeoutError('held')):
            lending.report(progress=PROGRESS)
            lending.report(progress=PROGRESS)
        self.assertEqual(said, ['lending: report failed (TimeoutError: held), carrying on'])

    def test_the_lane_never_writes_them(self):
        """In the pre-container lane there is no owner, so nothing is written."""
        lending = leases.Lending(None)
        lending.report(stage='behaviour: discovering builds', progress=PROGRESS)
        self.assertFalse(os.path.exists(os.path.join(self.folder, 'leases.json')))

    def test_dropped_when_a_stale_owner_is_found(self):
        owner = self.owner()
        owner.start()
        owner.report(stage='behaviour: servers', progress=PROGRESS)
        with leases.Store(self.folder) as state:
            leases.expire(state, 1000 + leases.OWNER_LIMIT + 1)
        entry = self.entry()
        self.assertFalse(entry['running'])
        for field in FIELDS:
            self.assertNotIn(field, entry)

    def test_dropped_at_once_when_the_owners_process_is_gone(self):
        """A hard kill: no clean-up and a heartbeat still fresh. The fields go at the next change to the file; the owner keeps counting
        as running until its heartbeat is OWNER_LIMIT old, as before."""
        gone = subprocess.run([sys.executable, '-I', '-c', 'import os; print(os.getpid())'], capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL, **NO_WINDOW)
        pid = int(gone.stdout)
        self.assertFalse(leases.process_alive(pid))
        write(self.folder, dict(leases.empty(), owner={'name': 'plugincheck', 'pid': pid, 'heartbeat': 1000, 'running': True, 'servers': 4,
                                                      'cores': CORES, 'run': self.run, 'stage': 'behaviour', 'progress': PROGRESS}))
        with leases.Store(self.folder) as state:
            leases.expire(state, 1001)
        entry = self.entry()
        self.assertTrue(entry['running'])
        for field in FIELDS:
            self.assertNotIn(field, entry)

    def live_run(self, pid):
        """A run folder as check.py leaves it when killed: live.json not finished, behaviour running, no results.json."""
        live = {'target': 'Demo', 'started': 900, 'updated': 950, 'finished': False, 'timeLeftSeconds': 300,
                'stages': [{'name': 'manifest', 'state': 'pass'}, {'name': 'behaviour', 'state': 'running'}, {'name': 'logcheck', 'state': 'planned'}],
                'progress': PROGRESS, 'servers': [{'cores': '0-1', 'build': 'b', 'scenario': 's', 'sinceSeconds': 5}]}
        with open(os.path.join(self.run, 'live.json'), 'w', encoding='utf-8') as f:
            json.dump(live, f)
        write(self.folder, dict(leases.empty(), owner={'name': 'plugincheck', 'pid': pid, 'heartbeat': 1000, 'running': True, 'servers': 4,
                                                      'cores': CORES, 'run': self.run, 'stage': 'behaviour: servers', 'progress': PROGRESS}))
        with leases.Store(self.folder) as state:
            leases.expire(state, 1001)
        with open(os.path.join(self.run, 'live.json'), encoding='utf-8') as f:
            return json.load(f)

    def test_a_killed_owners_run_folder_is_marked_ended(self):
        gone = subprocess.run([sys.executable, '-I', '-c', 'import os; print(os.getpid())'], capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL, **NO_WINDOW)
        live = self.live_run(int(gone.stdout))
        self.assertEqual((live['finished'], live['timeLeftSeconds'], live['servers']), (True, 0, []))
        self.assertEqual([s['state'] for s in live['stages']], ['pass', 'not run', 'planned'])
        with open(os.path.join(self.run, 'results.json'), encoding='utf-8') as f:
            results = json.load(f)
        self.assertEqual(results['stages'], {'manifest': {'status': 'not run'}, 'behaviour': {'status': 'not run', 'reason': 'ended without cleaning up'},
                                             'logcheck': {'status': 'not run'}})
        self.assertNotIn('run', self.entry())

    def test_a_live_owners_run_folder_is_left_alone(self):
        live = self.live_run(os.getpid())
        self.assertFalse(live['finished'])
        self.assertEqual(live['stages'][1]['state'], 'running')
        self.assertFalse(os.path.exists(os.path.join(self.run, 'results.json')))

    def test_kept_while_the_owner_beats(self):
        owner = self.owner()
        owner.start()
        owner.report(progress=PROGRESS)
        with leases.Store(self.folder) as state:
            leases.expire(state, 1000 + leases.OWNER_LIMIT)
        self.assertEqual(self.entry()['progress'], PROGRESS)

    def test_dropped_from_an_owner_saved_as_not_running(self):
        """A file left by an owner that ended without its clean-up, or by an older copy, loses them at the next change."""
        write(self.folder, dict(leases.empty(), owner={'name': 'plugincheck', 'pid': 38596, 'heartbeat': 1000, 'running': False, 'servers': 0,
                                                      'cores': CORES, 'run': self.run, 'stage': 'behaviour', 'progress': PROGRESS}))
        with leases.Store(self.folder) as state:
            leases.expire(state, 1001)
        for field in FIELDS:
            self.assertNotIn(field, self.entry())

    def test_lease_py_status_drops_a_stale_owners_run(self):
        write(self.folder, dict(leases.empty(), owner={'name': 'plugincheck', 'pid': 38596, 'heartbeat': 1000, 'running': True, 'servers': 4,
                                                      'cores': CORES, 'run': self.run, 'stage': 'behaviour', 'progress': PROGRESS}))
        asked = []
        out = io.StringIO()
        with mock.patch.object(leases, 'FOLDER', self.folder), mock.patch.object(leases.Store.__init__, '__defaults__', (self.folder, 60, True)), \
                mock.patch.object(leases, 'docker_names', lambda: asked.append('docker ps') or set()), \
                mock.patch.object(sys, 'argv', ['lease.py', 'status']), mock.patch('sys.stdout', out):
            lease.main()
        self.assertEqual(asked, ['docker ps'], 'the stand-in for Docker was asked, not Docker')
        self.assertFalse(json.loads(out.getvalue())['pluginCheckRunning'])
        entry = self.entry()
        self.assertFalse(entry['running'])
        for field in FIELDS:
            self.assertNotIn(field, entry)

    def test_cleared_when_the_stage_crashes(self):
        """An exception that ends the stage still runs atexit, where behaviour.py stops its lending."""
        script = os.path.join(self.folder, 'crash.py')
        with open(script, 'w', encoding='utf-8') as f:
            f.write(DOCKER_SPY + f'''
import atexit, sys
sys.path.insert(0, {STAGES!r})
import leases
lending = leases.Lending(lambda: leases.Owner({CORES!r}, folder={self.folder!r}, names=lambda: set(), run={self.run!r}))
atexit.register(lending.stop)
lending.report(stage='behaviour: servers', progress={PROGRESS!r})
with open({os.path.join(self.folder, 'reported')!r}, 'w', encoding='utf-8') as f:
    f.write(open({os.path.join(self.folder, 'leases.json')!r}, encoding='utf-8').read())
raise RuntimeError('a crash in the stage')
''')
        calls = os.path.join(self.folder, 'docker-calls')
        done = subprocess.run([sys.executable, '-I', script], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                              env={**os.environ, 'DOCKER_SPY_CALLS': calls}, **NO_WINDOW)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('a crash in the stage', done.stderr)
        with open(os.path.join(self.folder, 'reported'), encoding='utf-8') as f:
            self.assertEqual(json.load(f)['owner']['progress'], PROGRESS, 'written while it ran')
        entry = self.entry()
        self.assertFalse(entry['running'])
        for field in FIELDS:
            self.assertNotIn(field, entry)
        self.assertFalse(os.path.exists(calls), 'nothing here reaches for Docker')

    @unittest.skipUnless(os.name == 'nt', 'console events are a Windows thing')
    def test_cleared_on_an_interrupt(self):
        """behaviour.py's own console handler, lifted, with real lending: a Ctrl-Break while the main thread waits on the server pool."""
        from test_clean_end import interrupt_in_hidden_console, lifted
        calls, ready = os.path.join(self.folder, 'calls.txt'), os.path.join(self.folder, 'ready')
        prelude = DOCKER_SPY + f'''
import atexit, concurrent.futures, contextlib, json, os, re, signal, sys, threading, time, types
sys.path.insert(0, {STAGES!r})
import leases
calls, ready = sys.argv[1], sys.argv[2]


def docker(*args, **options):
    with open(calls, 'a', encoding='utf-8') as f:
        f.write(json.dumps(args) + '\\n')
    return types.SimpleNamespace(returncode=0, stderr='', stdout='')


lending = leases.Lending(lambda: leases.Owner({CORES!r}, folder={self.folder!r}, names=lambda: set(), run={self.run!r}))
lending.report(stage='behaviour: servers', progress={PROGRESS!r})
'''
        block = '''
with open(ready, 'w') as f:
    f.write(str(os.getpid()))
with concurrent.futures.ThreadPoolExecutor(1) as pool:
    for future in concurrent.futures.as_completed([pool.submit(time.sleep, 120)]):
        pass
'''
        parts = lifted(('SERVER_NAME', 'END_SECONDS', 'CLEAN_SECONDS', 'ending', 'remove_servers', 'remove_own_servers', 'say_safely', 'interrupt_lock',
                        'interrupted'), main_block=True)
        script = os.path.join(self.folder, 'stage.py')
        with open(script, 'w', encoding='utf-8') as f:
            f.write(prelude + '\n\n'.join(parts) + '\n' + block)
        code = interrupt_in_hidden_console(os.path.join(self.folder, 'out.txt'), ready, [sys.executable, '-I', script, calls, ready],
                                           env={**os.environ, 'DOCKER_SPY_CALLS': os.path.join(self.folder, 'docker-calls')})
        self.assertEqual(code, '1')
        self.assertTrue(os.path.exists(ready), 'the stage got as far as its server pool')
        with open(calls, encoding='utf-8') as f:
            self.assertIn('"ps"', f.read(), "the stand-in for Docker took the clean-up's listing")
        entry = self.entry()
        self.assertFalse(entry['running'])
        for field in FIELDS:
            self.assertNotIn(field, entry)
        self.assertFalse(os.path.exists(os.path.join(self.folder, 'docker-calls')), 'nothing reached for Docker itself')


class BehaviourTests(unittest.TestCase):
    SOURCE = os.path.join(STAGES, 'behaviour.py')

    def source(self):
        with open(self.SOURCE, encoding='utf-8') as f:
            return f.read()

    def test_a_progress_step_feeds_the_panel_and_the_owner_entry_with_the_same_numbers(self):
        tree = ast.parse(self.source())
        wanted = [ast.get_source_segment(self.source(), n) for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ('status', 'progress')]
        self.assertEqual(len(wanted), 2, 'behaviour.py has status and progress')
        reported = []
        namespace = {'json': json, 'lending': types.SimpleNamespace(report=lambda **fields: reported.append(fields))}
        exec(compile('\n\n'.join(wanted), self.SOURCE, 'exec'), namespace)
        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            namespace['progress'](**PROGRESS)
        self.assertEqual(out.getvalue(), '@@' + json.dumps({'kind': 'progress', **PROGRESS}) + '\n')
        self.assertEqual(reported, [{'progress': PROGRESS}])

    def test_every_progress_step_goes_through_progress(self):
        source = self.source()
        self.assertFalse("status(kind='progress'" in source.replace("    status(kind='progress', **fields)", ''), 'no progress event outside progress()')
        self.assertEqual(source.count('progress(done='), 2)

    def test_the_owner_names_the_run_folder_and_each_stage(self):
        source = self.source()
        self.assertTrue('leases.Owner(all_slots, run=run_dir)' in source, 'the owner gets the run folder')
        steps = [source.index(f"lending.report(stage='behaviour: {s}')") for s in ('discovering builds', 'servers', 'alone reruns')]
        self.assertEqual(steps, sorted(steps))
        self.assertLess(source.index("lending.report(stage='behaviour: discovering builds')"), source.index('def discover(build)'))


class RunFolderTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix='pc-runs-')
        self.addCleanup(shutil.rmtree, self.folder, True)

    def test_this_checkouts_runs_folder_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('PLUGINCHECK_RUNS_DIR', None)
            self.assertEqual(runfolders.root(), os.path.join(ROOT, 'runs'))

    def test_plugincheck_runs_dir_moves_it_and_is_made_absolute(self):
        with mock.patch.dict(os.environ, {'PLUGINCHECK_RUNS_DIR': self.folder}):
            self.assertEqual(runfolders.root(), self.folder)
        with mock.patch.dict(os.environ, {'PLUGINCHECK_RUNS_DIR': 'relative-runs'}):
            self.assertEqual(runfolders.root(), os.path.abspath('relative-runs'))

    def test_a_run_folder_is_runs_target_stamp(self):
        at = datetime.datetime(2026, 11, 15, 10, 15, 0, 123456)
        path = runfolders.new('Demo', self.folder, lambda: at)
        self.assertEqual(path, os.path.join(self.folder, 'Demo', '20261115-101500'))
        self.assertTrue(os.path.isdir(path))

    def test_two_runs_in_one_second_get_their_own_folders(self):
        at = datetime.datetime(2026, 11, 15, 10, 15, 0)
        first, second, third = (runfolders.new('Demo', self.folder, lambda: at) for _ in range(3))
        self.assertEqual([os.path.basename(p) for p in (first, second, third)], ['20261115-101500', '20261115-101501', '20261115-101502'])

    def test_an_existing_run_folder_is_never_used_again(self):
        at = datetime.datetime(2026, 11, 15, 10, 15, 0)
        kept = os.path.join(self.folder, 'Demo', '20261115-101500')
        os.makedirs(kept)
        with open(os.path.join(kept, 'results.json'), 'w', encoding='utf-8') as f:
            f.write('{"kept": true}')
        path = runfolders.new('Demo', self.folder, lambda: at)
        self.assertNotEqual(path, kept)
        self.assertEqual(os.listdir(path), [])
        with open(os.path.join(kept, 'results.json'), encoding='utf-8') as f:
            self.assertEqual(f.read(), '{"kept": true}')

    def test_check_py_makes_its_run_folder_there_and_reads_past_timings_there(self):
        with open(os.path.join(ROOT, 'check.py'), encoding='utf-8') as f:
            source = f.read()
        self.assertTrue("run_dir = runfolders.new(target['name'])" in source, 'the run folder comes from runfolders.new')
        self.assertTrue("glob.glob(os.path.join(runfolders.root(), target['name'], '*', 'results.json'))" in source, 'past timings from the same root')
        self.assertFalse("os.path.join(HERE, 'runs'" in source, "no run folder under the checkout's runs folder by name")

    def test_a_copy_writes_its_run_into_the_runs_folder_it_is_given(self):
        """A copy of check.py with PLUGINCHECK_RUNS_DIR set: its run lands in that folder, next to (never in) the run folders already there."""
        root = os.path.join(self.folder, 'copy')
        os.makedirs(os.path.join(root, 'targets'))
        shutil.copy(os.path.join(ROOT, 'check.py'), root)
        shutil.copytree(STAGES, os.path.join(root, 'stages'), ignore=shutil.ignore_patterns('__pycache__', 'bin', 'obj'))
        with open(os.path.join(root, 'stages', 'manifest.py'), 'w', encoding='utf-8') as f:
            f.write("import json, sys\njson.dump({'status': 'pass'}, open(sys.argv[-1], 'w'))\n")
        repo = os.path.join(self.folder, 'plugin')
        os.makedirs(repo)
        with open(os.path.join(root, 'targets', 'Fake.json'), 'w', encoding='utf-8') as f:
            json.dump({'name': 'Fake', 'repo': repo}, f)
        site = os.path.join(self.folder, 'site')
        os.makedirs(site)
        with open(os.path.join(site, 'sitecustomize.py'), 'w', encoding='utf-8') as f:
            f.write(DOCKER_SPY)
        shared = os.path.join(self.folder, 'shared-runs')
        now = datetime.datetime.now()
        # Every stamp the run could take in the next minute is there already, with a run's results in it.
        taken = [(now + datetime.timedelta(seconds=s)).strftime(runfolders.STAMP) for s in range(60)]
        for stamp in taken:
            os.makedirs(os.path.join(shared, 'Fake', stamp))
            with open(os.path.join(shared, 'Fake', stamp, 'results.json'), 'w', encoding='utf-8') as f:
                f.write(stamp)
        calls = os.path.join(self.folder, 'docker-calls')
        # No GitHub token: check.py would ask GitHub whether it is still good.
        env = {**{k: v for k, v in os.environ.items() if k != 'PULSAR_GITHUB_TOKEN'}, 'PLUGINCHECK_TOKEN_REJECTED': '1', 'PYTHONPATH': site,
               'PLUGINCHECK_RUNS_DIR': shared, 'PLUGINCHECK_PLAIN': '1', 'DOCKER_SPY_CALLS': calls,
               'PLUGINCHECK_MACHINE_DIR': os.path.join(self.folder, 'machine'), 'PLUGINCHECK_RUN_QUEUE': f'pc-test-tile-data-{os.getpid()}'}
        done = subprocess.run([sys.executable, os.path.join(root, 'check.py'), 'Fake', '--only', 'manifest'], capture_output=True, text=True,
                              encoding='utf-8', errors='replace', env=env, timeout=120, stdin=subprocess.DEVNULL, **NO_WINDOW)
        said = done.stdout + done.stderr
        self.assertEqual(done.returncode, 0, said)
        runs = sorted(os.listdir(os.path.join(shared, 'Fake')))
        new = [r for r in runs if r not in taken]
        self.assertEqual(len(new), 1, said)
        self.assertRegex(new[0], STAMP)
        self.assertGreater(new[0], taken[-1], 'a stamp after every folder that was there')
        with open(os.path.join(shared, 'Fake', new[0], 'results.json'), encoding='utf-8') as f:
            self.assertEqual(json.load(f)['stages']['manifest']['status'], 'pass')
        self.assertTrue(os.path.exists(os.path.join(shared, 'Fake', new[0], 'live.json')))
        for stamp in taken:
            with open(os.path.join(shared, 'Fake', stamp, 'results.json'), encoding='utf-8') as f:
                self.assertEqual(f.read(), stamp, 'a run folder that was there is left as it was')
        self.assertFalse(os.path.exists(os.path.join(root, 'runs')), "nothing in the copy's own runs folder")
        with open(calls, encoding='utf-8') as f:
            asked = [json.loads(line) for line in f]
        self.assertIn('run', [a[1] for a in asked], "the stand-in for Docker took check.py's game build look")


class LeaseFolderTests(unittest.TestCase):
    """The lease folder, in this order: PLUGINCHECK_MACHINE_DIR, else machine-dir.txt in the checkout's root, else the checkout's machine folder."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix='pc-leasefolder-')
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.checkout = os.path.join(self.folder, 'checkout')
        os.makedirs(os.path.join(self.checkout, 'stages'))
        shutil.copy(os.path.join(STAGES, 'leases.py'), os.path.join(self.checkout, 'stages'))
        shutil.copy(os.path.join(ROOT, 'lease.py'), self.checkout)
        # Stands for a folder shared with live runs: nothing here may write there.
        self.forbidden = os.path.join(self.folder, 'forbidden')
        self.scratch = os.path.join(self.folder, 'scratch')

    def file(self, text):
        with open(os.path.join(self.checkout, 'machine-dir.txt'), 'w', encoding='utf-8') as f:
            f.write(text)

    def test_the_environment_variable_comes_first(self):
        self.file(self.forbidden + '\n')
        self.assertEqual(leases.machine_folder({'PLUGINCHECK_MACHINE_DIR': self.scratch}, self.checkout), self.scratch)

    def test_then_the_file(self):
        self.file('\ufeff  ' + self.forbidden + '  \r\n\r\n')
        self.assertEqual(leases.machine_folder({}, self.checkout), self.forbidden)
        self.assertEqual(leases.machine_folder({'PLUGINCHECK_MACHINE_DIR': ''}, self.checkout), self.forbidden, 'an empty variable is not set')

    def test_then_the_checkouts_own_folder(self):
        self.assertEqual(leases.machine_folder({}, self.checkout), os.path.join(self.checkout, 'machine'))

    def test_a_file_without_one_absolute_path_is_refused_by_name(self):
        for text in ('', '   \n', 'machine', r'..\machine', self.forbidden + '\n' + self.scratch + '\n'):
            with self.subTest(repr(text)):
                self.file(text)
                with self.assertRaisesRegex(ValueError, re.escape(os.path.join(self.checkout, 'machine-dir.txt'))):
                    leases.machine_folder({}, self.checkout)

    def test_a_bad_file_is_not_read_while_the_variable_is_set(self):
        self.file('relative')
        self.assertEqual(leases.machine_folder({'PLUGINCHECK_MACHINE_DIR': self.scratch}, self.checkout), self.scratch)

    def child(self, code, env):
        script = os.path.join(self.folder, 'child.py')
        with open(script, 'w', encoding='utf-8') as f:
            f.write(DOCKER_SPY + code)
        return subprocess.run([sys.executable, '-I', script], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                              env={**env, 'DOCKER_SPY_CALLS': os.path.join(self.folder, 'docker-calls')}, **NO_WINDOW)

    def env(self, folder=None):
        env = {k: v for k, v in os.environ.items() if k != 'PLUGINCHECK_MACHINE_DIR'}
        return env if folder is None else {**env, 'PLUGINCHECK_MACHINE_DIR': folder}

    def folder_of(self, env):
        done = self.child(f'import sys\nsys.path.insert(0, {os.path.join(self.checkout, "stages")!r})\nimport leases\nprint(leases.FOLDER)\n', env)
        return done

    def test_a_copy_uses_each_in_that_order(self):
        """Only the folder is named here: nothing opens a lease file."""
        self.assertEqual(self.folder_of(self.env()).stdout.strip(), os.path.join(self.checkout, 'machine'))
        self.file(self.forbidden + '\n')
        self.assertEqual(self.folder_of(self.env()).stdout.strip(), self.forbidden)
        self.assertEqual(self.folder_of(self.env(self.scratch)).stdout.strip(), self.scratch)
        self.assertFalse(os.path.exists(self.forbidden))

    def test_a_copy_with_a_bad_file_stops_with_one_line_naming_it(self):
        self.file('machine')
        self.assertEqual(self.folder_of(self.env()).stdout.strip(), 'None', 'importing leases does not fail')
        done = self.child(f'import runpy, sys\nsys.argv = [{os.path.join(self.checkout, "lease.py")!r}, "status"]\n'
                          f'runpy.run_path(sys.argv[0], run_name="__main__")\n', self.env())
        self.assertEqual(done.returncode, 1)
        self.assertNotIn('Traceback', done.stderr)
        self.assertEqual(len(done.stderr.strip().splitlines()), 1, done.stderr)
        self.assertIn('machine-dir.txt must hold one line', done.stderr)

    def test_check_py_help_works_with_a_bad_file(self):
        self.file('')
        shutil.copy(os.path.join(ROOT, 'check.py'), self.checkout)
        shutil.rmtree(os.path.join(self.checkout, 'stages'))
        shutil.copytree(STAGES, os.path.join(self.checkout, 'stages'), ignore=shutil.ignore_patterns('__pycache__', 'bin', 'obj'))
        done = subprocess.run([sys.executable, '-I', os.path.join(self.checkout, 'check.py'), '--help'], capture_output=True, text=True,
                              timeout=60, stdin=subprocess.DEVNULL, env=self.env(), **NO_WINDOW)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('usage:', done.stdout)

    def test_a_blank_variable_counts_as_unset(self):
        self.file(self.forbidden + '\n')
        self.assertEqual(leases.machine_folder({'PLUGINCHECK_MACHINE_DIR': '   '}, self.checkout), self.forbidden)

    def test_a_forbidden_folder_is_refused_from_any_source_by_real_path(self):
        os.makedirs(self.forbidden)
        spelled = os.path.join(self.forbidden, '..', 'forbidden') + os.sep
        marker = {leases.FORBIDDEN_ENV: os.pathsep.join([os.path.join(self.folder, 'other'), spelled])}
        with self.assertRaisesRegex(ValueError, leases.FORBIDDEN_ENV):
            leases.machine_folder({**marker, 'PLUGINCHECK_MACHINE_DIR': self.forbidden}, self.checkout)
        self.file(self.forbidden.upper() if os.name == 'nt' else self.forbidden)
        with self.assertRaisesRegex(ValueError, leases.FORBIDDEN_ENV):
            leases.machine_folder(marker, self.checkout)
        self.assertEqual(leases.machine_folder({**marker, 'PLUGINCHECK_MACHINE_DIR': self.scratch}, self.checkout), self.scratch)

    def test_a_child_that_drops_the_variable_is_still_refused(self):
        """The guard's marker reaches a process a test starts even when that process is given no PLUGINCHECK_MACHINE_DIR."""
        self.file(self.forbidden + '\n')
        env = {**self.env(), leases.FORBIDDEN_ENV: self.forbidden}
        done = self.child(f'import sys\nsys.path.insert(0, {os.path.join(self.checkout, "stages")!r})\nimport leases\nleases.Store()\n', env)
        self.assertEqual(done.returncode, 1)
        self.assertIn('is refused here', done.stderr)
        self.assertFalse(os.path.exists(self.forbidden))

    def test_lease_py_writes_the_variables_folder_and_never_the_files(self):
        """As a test run does: the variable is set, and a machine-dir.txt names a folder shared with live runs."""
        self.file(self.forbidden + '\n')
        done = self.child(f'import runpy, sys\nsys.argv = [{os.path.join(self.checkout, "lease.py")!r}, "status"]\n'
                          f'runpy.run_path(sys.argv[0], run_name="__main__")\n', self.env(self.scratch))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.scratch, 'leases.json')))
        self.assertFalse(os.path.exists(self.forbidden), "the file's folder was not touched")
        with open(os.path.join(self.folder, 'docker-calls'), encoding='utf-8') as f:
            self.assertEqual([json.loads(line)[:2] for line in f], [['docker', 'ps']], 'the stand-in for Docker took the one docker call')

    def test_no_user_folder_in_the_code(self):
        with open(os.path.join(STAGES, 'leases.py'), encoding='utf-8') as f:
            self.assertNotRegex(f.read(), r"['\"][A-Za-z]:[\\/]", 'no drive path of this computer in the code')


if __name__ == '__main__':
    unittest.main()
