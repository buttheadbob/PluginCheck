"""The pre-container lane. Run with: python -I -m unittest discover -s tests

Every container is pinned, --continue refuses what it must, and the lane holds one slot outside the borrowers' limits, steps aside
for an exclusive request and gives the slot back. No Docker and no real lease file: leases use a temporary folder.
"""
import ast, glob, json, os, shutil, subprocess, sys, tempfile, threading, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
sys.path.insert(0, HERE)
import replay
import leases, pin, prerun
from test_replay import synthetic_case

CORES = [f'{i}-{i + 1}' for i in range(0, 32, 2)]


def python_files():
    return [os.path.join(ROOT, 'check.py'), os.path.join(ROOT, 'lease.py')] + \
           [f for f in glob.glob(os.path.join(ROOT, 'stages', '**', '*.py'), recursive=True) if '__pycache__' not in f]


class PinTests(unittest.TestCase):
    def test_every_docker_run_is_pinned(self):
        """A docker run with neither *pin.args() nor its own --cpuset-cpus would run on cores a behaviour run measures on."""
        open_sites = []
        for path in python_files():
            text = open(path, encoding='utf-8').read()
            for node in ast.walk(ast.parse(text)):
                items = node.args if isinstance(node, ast.Call) else node.elts if isinstance(node, ast.List) else None
                if not items:
                    continue
                first = [a.value if isinstance(a, ast.Constant) else None for a in items[:2]]
                if (isinstance(node, ast.Call) and first[:1] == ['run'] and getattr(node.func, 'id', '') == 'docker') or \
                        (isinstance(node, ast.List) and first == ['docker', 'run']):
                    segment = ast.get_source_segment(text, node)
                    if 'pin.args()' not in segment and '--cpuset-cpus' not in segment:
                        open_sites.append(f'{os.path.relpath(path, ROOT)}:{node.lineno}')
            # No docker run hidden in a shell string either.
            for number, line in enumerate(text.splitlines(), 1):
                if 'docker run' in line and not line.lstrip().startswith(('#', '"', "'")) and 'docker run --cpuset-cpus' not in line:
                    open_sites.append(f'{os.path.relpath(path, ROOT)}:{number} (text)')
        self.assertEqual(open_sites, [])

    def test_a_file_that_pins_imports_pin(self):
        for path in python_files():
            text = open(path, encoding='utf-8').read()
            if 'pin.args()' in text:
                tree = ast.parse(text)
                imports = [n.lineno for n in tree.body if isinstance(n, ast.Import) and any(a.name == 'pin' for a in n.names)]
                self.assertTrue(imports, path)
                # Stage files run top to bottom, so a use at module level before the import is a NameError when the stage runs.
                inside = [(n.lineno, n.end_lineno) for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))]
                uses = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == 'pin'
                        and not any(start <= n.lineno <= end for start, end in inside)]
                if uses:
                    self.assertLess(min(imports), min(uses), f'{path}: pin is used on line {min(uses)} before it is imported')

    def test_a_stage_finds_pin_under_dash_i(self):
        """check.py runs each stage as `python -I`, which leaves the script's folder off sys.path, so the stage must add it before importing pin."""
        for path in glob.glob(os.path.join(ROOT, 'stages', '*.py')):
            tree = ast.parse(open(path, encoding='utf-8').read())
            imports = [n.lineno for n in tree.body if isinstance(n, ast.Import) and any(a.name == 'pin' for a in n.names)]
            if not imports:
                continue
            inserts = [n.lineno for n in tree.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                       and ast.unparse(n.value.func) in ('sys.path.insert', 'sys.path.append')]
            self.assertTrue(inserts and min(inserts) < min(imports), f'{path}: import pin on line {min(imports)} has no sys.path entry before it')

    def test_the_pin_follows_the_lane(self):
        saved = os.environ.pop('PLUGINCHECK_CPUSET', None)
        try:
            self.assertEqual(pin.args(), [])
            os.environ['PLUGINCHECK_CPUSET'] = '6-7'
            self.assertEqual(pin.args(), ['--cpuset-cpus', '6-7'])
        finally:
            os.environ.pop('PLUGINCHECK_CPUSET', None)
            if saved is not None:
                os.environ['PLUGINCHECK_CPUSET'] = saved


class ContinueTests(unittest.TestCase):
    INPUTS = {'source': 's1', 'tools': 't1', 'game': 'g1', 'target': 'x1'}
    READY = {'manifest': {'status': 'pass'}, 'lint': {'status': 'review'}, 'behaviour': {'status': 'pass', 'discoverOnly': True, 'commands': 4}}

    def test_a_ready_first_half(self):
        pre = prerun.record(self.READY, True, self.INPUTS)
        self.assertEqual(pre['status'], 'ready')
        self.assertEqual(prerun.problems(pre, dict(self.INPUTS)), [])

    def test_it_is_not_ready_when(self):
        for stages, reading, why in ((dict(self.READY, lint={'status': 'fail', 'reason': 'CS0103'}), True, 'lint failed'),
                                     (dict(self.READY, manifest={'status': 'fail', 'reason': 'bad path'}), True, 'manifest failed'),
                                     (dict(self.READY, behaviour={'status': 'not run', 'reason': 'working-torch could not be discovered'}), True, 'discovery did not pass'),
                                     (dict(self.READY, behaviour={'status': 'review'}), True, 'discovery did not pass'),
                                     (self.READY, False, 'no current reading')):
            with self.subTest(why=why):
                pre = prerun.record(stages, reading, self.INPUTS)
                self.assertEqual(pre['status'], 'failed')
                self.assertTrue(any(why in r for r in pre['reasons']), pre['reasons'])
                refused = prerun.problems(pre, dict(self.INPUTS))
                self.assertTrue(refused and 'did not end ready' in refused[0])

    def test_a_plugin_with_no_behaviour_stage_is_ready(self):
        stages = {'manifest': {'status': 'pass'}, 'lint': {'status': 'review'}, 'behaviour': {'status': 'not present', 'reason': 'no Magnetar or Torch'}}
        self.assertEqual(prerun.readiness(stages, False), [])

    def test_a_stage_is_timed_from_when_the_lane_lets_it_start(self):
        source = open(os.path.join(ROOT, 'check.py'), encoding='utf-8').read()
        entered = source.index("with (lane.stage() if lane else contextlib.nullcontext()) as cores")
        self.assertLess(entered, source.index('        start = time.time()', entered))
        self.assertNotIn('    start = time.time()\n    env = ', source)

    def test_a_plugin_with_no_commands_needs_no_reading(self):
        stages = dict(self.READY, behaviour={'status': 'pass', 'discoverOnly': True, 'commands': 0})
        self.assertEqual(prerun.record(stages, False, self.INPUTS)['status'], 'ready')

    def test_continue_refuses_changed_inputs(self):
        pre = prerun.record(self.READY, True, self.INPUTS)
        for key, words in (('source', "plugin's files"), ('tools', "PluginCheck's stages"), ('game', 'the game'), ('target', 'the target')):
            with self.subTest(key=key):
                refused = prerun.problems(pre, dict(self.INPUTS, **{key: 'other'}))
                self.assertEqual(len(refused), 1)
                self.assertIn(words, refused[0])

    def test_continue_refuses_a_folder_never_started_with_pre(self):
        self.assertIn('not started with --pre', prerun.problems(None, self.INPUTS)[0])

    def test_the_tools_hash_sees_stage_changes_and_ignores_caches(self):
        folder = tempfile.mkdtemp(prefix='pc-tools-')
        try:
            os.makedirs(os.path.join(folder, 'stages', 'behaviour', '__pycache__'))
            open(os.path.join(folder, 'stages', 'a.py'), 'w').write('x = 1\n')
            before = prerun.tools_hash(folder)
            open(os.path.join(folder, 'stages', 'behaviour', '__pycache__', 'a.pyc'), 'w').write('compiled')
            self.assertEqual(prerun.tools_hash(folder), before)
            open(os.path.join(folder, 'stages', 'a.py'), 'w').write('x = 2\n')
            self.assertNotEqual(prerun.tools_hash(folder), before)
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_check_py_checks_before_it_runs_anything(self):
        source = open(os.path.join(ROOT, 'check.py'), encoding='utf-8').read()
        self.assertLess(source.index('refused = prerun.problems(pre, inputs)'), source.index('workers = [threading.Thread(target=when_ready'))
        self.assertIn("planned = [s for s in planned if s in (ALONE, LAST)]", source)
        self.assertIn("if not args.keep and not args.hold and not args.pre:", source)


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix='pc-lane-')

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def state(self):
        with leases.Store(self.folder) as state:
            return json.loads(json.dumps(state))

    def lane(self, **kwargs):
        return leases.Lane(CORES, folder=self.folder, names=lambda: set(), poll=0.05, beat_every=0.05, say=lambda text: None, **kwargs)

    def test_with_no_run_the_slot_comes_at_once_and_goes_back(self):
        lane = self.lane()
        cores = lane.acquire()
        self.assertIn(cores, CORES)
        self.assertEqual([l['session'] for l in self.state()['leases']], [leases.LANE])
        lane.release()
        self.assertEqual(self.state()['leases'], [])
        self.assertEqual(self.state()['requests'], [])

    def test_during_a_run_it_waits_for_a_server_to_finish_and_the_run_gets_the_slot_back(self):
        owner = leases.Owner(CORES, folder=self.folder, names=lambda: set())
        owner.start()
        lane = self.lane()
        self.assertIsNone(lane.ask(), 'granted while every slot is the run\'s')
        lease = owner.free('8-9')
        self.assertEqual((lease or {}).get('session'), leases.LANE)
        self.assertEqual(lane.acquire(), '8-9')
        lane.release()
        self.assertEqual(owner.beat(15), ['8-9'])

    def test_a_lease_file_that_fails_for_a_moment_is_asked_again(self):
        lane = self.lane()
        calls = []
        real = lane.ask

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError('the lock is held for a moment')
            return real()
        lane.ask = flaky
        self.assertIn(lane.acquire(), CORES)
        self.assertEqual(len(calls), 3)
        lane.release()

    def test_it_counts_against_neither_borrower_limit_and_the_run_keeps_nine(self):
        with leases.Store(self.folder) as state:
            now = time.time()
            for session in ('mods', 'torchfix', 'third', 'fourth'):
                leases.request(state, session, 2, now)
                now += 1
            leases.request(state, leases.LANE, 5, now)
            leases.grant_when_idle(state, now, CORES)
        held = {}
        for lease in self.state()['leases']:
            held[lease['session']] = held.get(lease['session'], 0) + 1
        self.assertEqual(held, {'mods': 2, 'torchfix': 2, 'third': 2, leases.LANE: 1})
        self.assertEqual(len(CORES) - sum(held.values()), 9)

    def test_it_steps_aside_for_an_exclusive_request_and_comes_back(self):
        lane = self.lane()
        lane.acquire()
        with lane.stage() as cores:
            with leases.Store(self.folder) as state:
                leases.request(state, 'pam', 1, time.time(), exclusive=True)
            time.sleep(0.3)
            # A stage running is never cut: the slot stays until it ends.
            self.assertEqual([l['session'] for l in self.state()['leases']], [leases.LANE])
        deadline = time.time() + 5
        while any(l['session'] == leases.LANE for l in self.state()['leases']) and time.time() < deadline:
            time.sleep(0.05)
        # With no run going, the lane's own next ask grants the exclusive request, as lease.py would.
        with leases.Store(self.folder) as state:
            leases.grant_when_idle(state, time.time(), CORES)
        self.assertEqual([(l['session'], l['cores']) for l in self.state()['leases']], [('pam', 'all')])
        started = threading.Event()

        def next_stage():
            with lane.stage():
                started.set()
        threading.Thread(target=next_stage, daemon=True).start()
        time.sleep(0.3)
        self.assertFalse(started.is_set(), 'a stage started while the exclusive lease held the machine')
        with leases.Store(self.folder) as state:
            leases.release(state, 'pam')
        self.assertTrue(started.wait(5))
        self.assertEqual([l['session'] for l in self.state()['leases']], [leases.LANE])
        lane.release()
        self.assertEqual(self.state()['leases'], [])

    def failing_writes(self, count):
        """The lease file's write fails `count` times when the lane gives its slot back, as on Windows while another program reads it."""
        real, failures = leases.release, [count]

        def release(state, session, cores=None):
            if session == leases.LANE and failures[0] > 0:
                failures[0] -= 1
                raise PermissionError('leases.json is open in another program')
            return real(state, session, cores)
        leases.release = release
        self.addCleanup(setattr, leases, 'release', real)

    def test_a_failed_write_while_stepping_aside_is_tried_again(self):
        """One failed write used to end the thread that keeps the lane: no more heartbeats, and the next stage waited for ever."""
        said = []
        lane = leases.Lane(CORES, folder=self.folder, names=lambda: set(), poll=0.05, beat_every=0.05, say=said.append)
        lane.acquire()
        self.failing_writes(2)
        with leases.Store(self.folder) as state:
            leases.request(state, 'pam', 1, time.time(), exclusive=True)
        deadline = time.time() + 5
        while any(l['session'] == leases.LANE for l in self.state()['leases']) and time.time() < deadline:
            time.sleep(0.05)
        self.assertNotIn(leases.LANE, [l['session'] for l in self.state()['leases']], 'the slot never went back')
        self.assertEqual(sum('giving the slot back failed' in s for s in said), 2)
        with leases.Store(self.folder) as state:
            leases.grant_when_idle(state, time.time(), CORES)
            leases.release(state, 'pam')
        started = threading.Event()

        def next_stage():
            with lane.stage():
                started.set()
        threading.Thread(target=next_stage, daemon=True).start()
        self.assertTrue(started.wait(5), 'the lane never came back for the next stage')
        self.assertTrue(lane.thread.is_alive())
        lane.release()
        self.assertEqual(self.state()['leases'], [])

    def test_release_tries_a_failed_write_again(self):
        lane = self.lane()
        lane.acquire()
        self.failing_writes(2)
        lane.release()
        self.assertEqual(self.state()['leases'], [])

    def test_release_says_so_when_the_write_keeps_failing(self):
        said = []
        lane = leases.Lane(CORES, folder=self.folder, names=lambda: set(), poll=0.05, beat_every=0.05, say=said.append)
        lane.acquire()
        self.failing_writes(10 ** 6)
        real_seconds, leases.RELEASE_SECONDS = leases.RELEASE_SECONDS, 0.3
        self.addCleanup(setattr, leases, 'RELEASE_SECONDS', real_seconds)
        lane.release()
        self.assertTrue(any('lease ends by itself' in s for s in said), said)

    def test_one_stage_at_a_time(self):
        lane = self.lane()
        lane.acquire()
        inside, most = [0], [0]

        def work():
            with lane.stage():
                inside[0] += 1
                most[0] = max(most[0], inside[0])
                time.sleep(0.05)
                inside[0] -= 1
        threads = [threading.Thread(target=work) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        lane.release()
        self.assertEqual(most[0], 1)

    def test_lease_py_refuses_the_lane_name(self):
        done = subprocess.run([sys.executable, '-I', os.path.join(ROOT, 'lease.py'), 'request', '--session', leases.LANE],
                              capture_output=True, text=True, env={**os.environ, 'PLUGINCHECK_MACHINE_DIR': self.folder})
        self.assertEqual(done.returncode, 2)
        self.assertIn("pre-container lane", done.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.folder, 'leases.json')))


class DiscoverOnlyTests(unittest.TestCase):
    def test_behaviour_only_discovers_in_the_lane(self):
        os.environ['PLUGINCHECK_BEHAVIOUR_DISCOVER_ONLY'] = '1'
        try:
            r = replay.Replay(os.path.join(replay.STAGES, 'behaviour.py'), synthetic_case(discover_delay={'published-torch': 0.3}), replay.folder()).run()
        finally:
            del os.environ['PLUGINCHECK_BEHAVIOUR_DISCOVER_ONLY']
        self.assertEqual(r.outcome.status, 'pass')
        self.assertTrue(r.outcome.fields['discoverOnly'])
        self.assertEqual(r.server_started, [])
        self.assertGreater(r.outcome.fields['commands'], 0)
        self.assertTrue(r.outcome.fields['reading'])
        full = replay.Replay(os.path.join(replay.STAGES, 'behaviour.py'), synthetic_case(), replay.folder()).run()
        self.assertEqual(r.outcome.fields['scenarios'], len(full.outcome.fields['scenarios']))


if __name__ == '__main__':
    unittest.main()
