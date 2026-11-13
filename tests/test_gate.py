"""The machine gate and a claim made at once, without Docker: python -I -m unittest discover -s tests

Work outside a lease calls the gate first: it waits while an exclusive request waits or holds and returns once it is released, and its
timeout names who claims the machine and since when. A claim made while a full run is going is granted as soon as the stage running
then ends, with no wait of its own, and the run's next stage and the gated work go on once it is released.
"""
import json, os, shutil, subprocess, sys, tempfile, threading, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(ROOT, 'stages'))
sys.path.insert(0, ROOT)
import leases
import lease

CORES = [f'{i}-{i + 1}' for i in range(0, 32, 2)]


class Clock:
    def __init__(self, now=None):
        self.now = time.time() if now is None else now

    def __call__(self):
        return self.now


def data(path):
    with open(path, 'rb') as file:
        return file.read()


def claim(folder, session='physbench', at=None, granted=False):
    with leases.Store(folder) as state:
        now = time.time() if at is None else at
        leases.request(state, session, 1, now, True, 'PI a2 timing')
        if granted:
            leases.grant_when_idle(state, now, CORES, containers=set())


def release(folder, session='physbench'):
    with leases.Store(folder) as state:
        leases.release(state, session)


class ClaimsTests(unittest.TestCase):
    def setUp(self):
        self.state = leases.empty()

    def test_a_free_machine_has_no_claims(self):
        self.assertEqual(leases.claims(self.state, 10), [])
        leases.request(self.state, 'mods', 2, 1, purpose='mod test')
        leases.grant_when_idle(self.state, 1, CORES)
        self.assertEqual(leases.claims(self.state, 10), [], 'a normal lease does not shut the gate')

    def test_a_held_claim_is_named_with_its_purpose_and_since_when(self):
        leases.request(self.state, 'physbench', 1, 100.0, True, 'PI a2 timing')
        leases.grant_when_idle(self.state, 160.0, CORES, containers=set())
        self.assertEqual(leases.claims(self.state, 170.0), [f'exclusive lease held by physbench (PI a2 timing) since {leases.when(160.0)}'])

    def test_a_holder_that_asked_again_is_one_claim(self):
        leases.request(self.state, 'physbench', 1, 100.0, True, 'PI a2 timing')
        leases.grant_when_idle(self.state, 160.0, CORES, containers=set())
        leases.request(self.state, 'physbench', 1, 161.0, True, 'PI a2 timing')  # its next ask, sent before it saw the grant
        self.assertEqual(leases.claims(self.state, 170.0), [f'exclusive lease held by physbench (PI a2 timing) since {leases.when(160.0)}'])

    def test_a_waiting_claim_says_what_it_still_waits_for(self):
        leases.request(self.state, 'physbench', 1, 100.0, True, 'PI a2 timing')
        leases.put_run(self.state, 150.0, 111, 'SensEssentials', ['torch'])
        said = leases.claims(self.state, 160.0)
        self.assertEqual(said, [f'exclusive request by physbench (PI a2 timing) waiting since {leases.when(100.0)}, for: stages of a full run: torch'])

    def test_a_lapsed_claim_or_the_callers_own_does_not_count(self):
        leases.request(self.state, 'physbench', 1, 100.0, True, 'x')
        self.assertEqual(leases.claims(self.state, 100.0, session='physbench'), [])
        self.assertEqual(leases.claims(self.state, 100.0 + leases.HEARTBEAT_LIMIT + 1), [])
        self.assertTrue(self.state['requests'], 'worked out on a copy: the state is left as it was')


class GateTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix='pc-gate-')
        self.said = []

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def gate(self, **kwargs):
        return leases.gate('dummy build', folder=self.folder, poll=0.02, say=self.said.append, **kwargs)

    def test_a_free_machine_lets_the_work_through_at_once_and_quietly(self):
        self.assertLess(self.gate(timeout=5), 1)
        self.assertEqual(self.said, [])

    def test_it_waits_while_a_claim_is_held_and_returns_once_it_is_released(self):
        claim(self.folder, granted=True)
        done = []
        thread = threading.Thread(target=lambda: done.append(self.gate()), daemon=True)
        thread.start()
        time.sleep(0.3)
        self.assertTrue(thread.is_alive(), 'the gate let work through while the machine was held')
        self.assertIn('exclusive lease held by physbench (PI a2 timing)', self.said[0])
        release(self.folder)
        thread.join(5)
        self.assertFalse(thread.is_alive(), 'the gate stayed shut after the release')
        self.assertTrue(self.said[-1].startswith('machine gate (dummy build): free after'))

    def test_it_waits_while_a_claim_is_still_waiting(self):
        claim(self.folder)
        thread = threading.Thread(target=self.gate, daemon=True)
        thread.start()
        time.sleep(0.3)
        self.assertTrue(thread.is_alive())
        self.assertIn('exclusive request by physbench (PI a2 timing) waiting since', self.said[0])
        release(self.folder)
        thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_the_timeout_names_who_claims_the_machine_and_since_when(self):
        at = time.time() - 30
        claim(self.folder, at=at, granted=True)
        started = time.time()
        with self.assertRaises(leases.GateTimeout) as raised:
            self.gate(timeout=0.3)
        self.assertGreaterEqual(time.time() - started, 0.3)
        self.assertLess(time.time() - started, 5)
        text = str(raised.exception)
        self.assertTrue(text.startswith('machine gate (dummy build): still shut after 0 s: '), text)
        self.assertIn(f'exclusive lease held by physbench (PI a2 timing) since {leases.when(at)}', text)
        self.assertEqual(raised.exception.claims, [f'exclusive lease held by physbench (PI a2 timing) since {leases.when(at)}'])
        self.assertIsInstance(raised.exception, TimeoutError)

    def test_a_timeout_of_0_looks_once(self):
        self.assertLess(self.gate(timeout=0), 1)
        claim(self.folder)
        with self.assertRaises(leases.GateTimeout):
            self.gate(timeout=0)
        self.assertEqual(self.said, [])

    def test_the_session_s_own_claim_does_not_shut_it_for_that_session(self):
        claim(self.folder, granted=True)
        self.assertLess(self.gate(timeout=0, session='physbench'), 1)
        with self.assertRaises(leases.GateTimeout):
            self.gate(timeout=0, session='other')

    def test_it_changes_nothing_in_the_lease_file(self):
        claim(self.folder, granted=True)
        path = os.path.join(self.folder, 'leases.json')
        read = lambda: (data(path), os.path.getmtime(path))
        before = read()
        time.sleep(0.05)
        with self.assertRaises(leases.GateTimeout):
            self.gate(timeout=0.1)
        self.assertEqual(read(), before)
        self.assertFalse(os.path.exists(os.path.join(self.folder, 'leases.lock')), 'the lock was not given back')

    def test_a_lease_file_it_cannot_read_counts_as_shut_and_is_named(self):
        lock = os.path.join(self.folder, 'leases.lock')
        os.makedirs(lock)
        with open(os.path.join(lock, 'holder'), 'w', encoding='utf-8') as out:
            json.dump({'pid': os.getpid(), 'time': time.time(), 'token': 'theirs'}, out)
        with self.assertRaises(leases.GateTimeout) as raised:
            self.gate(timeout=1)
        self.assertIn('the lease file could not be read (TimeoutError', str(raised.exception))

    def test_a_long_wait_is_reported_now_and_then_not_every_look(self):
        claim(self.folder, granted=True)
        clock = Clock()

        def sleep(seconds):
            clock.now += 60
            with leases.Store(self.folder) as state:
                leases.heartbeat(state, 'physbench', clock.now)  # the holder keeps its claim alive
            if clock.now - start >= 660:
                release(self.folder)
        start = clock()
        leases.gate('dummy build', folder=self.folder, clock=clock, sleep=sleep, poll=60, say=self.said.append)
        waiting = [s for s in self.said if 'waiting:' in s]
        self.assertEqual(len(waiting), 3, self.said)  # at 0, 300 and 600 s
        self.assertEqual(self.said[-1], 'machine gate (dummy build): free after 660 s')

    def test_python_callers_get_it_from_lease_py(self):
        self.assertIs(lease.gate, leases.gate)
        self.assertIs(lease.GateTimeout, leases.GateTimeout)


class GateCliTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix='pc-gate-cli-')
        env = {k: v for k, v in os.environ.items() if k.upper() != 'PATH'}
        env.update({'PLUGINCHECK_MACHINE_DIR': self.folder, 'PATH': os.path.dirname(sys.executable)})
        self.env = env

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def run_gate(self, *args, **kwargs):
        return subprocess.run([sys.executable, '-I', os.path.join(ROOT, 'lease.py'), 'gate', *args], capture_output=True, text=True,
                              env=self.env, creationflags=0x08000000 if os.name == 'nt' else 0, timeout=60, **kwargs)

    def test_free_exits_0_with_one_json_line(self):
        done = self.run_gate('--purpose', 'dummy build', '--timeout', '5')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), {'gate': 'free', 'purpose': 'dummy build', 'waitedSeconds': 0})
        self.assertEqual(done.stderr, '')

    def test_shut_until_the_timeout_exits_3_and_names_the_claim(self):
        at = time.time() - 5
        claim(self.folder, at=at)
        done = self.run_gate('--purpose', 'dummy build', '--timeout', '1')
        self.assertEqual(done.returncode, 3)
        self.assertIn(f'exclusive request by physbench (PI a2 timing) waiting since {leases.when(at)}', done.stderr)
        self.assertIn('still shut after 1 s', done.stderr)
        self.assertEqual(json.loads(done.stdout)['gate'], 'shut')

    def test_a_held_claim_released_while_it_waits_lets_it_through(self):
        claim(self.folder, granted=True)
        process = subprocess.Popen([sys.executable, '-I', os.path.join(ROOT, 'lease.py'), 'gate', '--purpose', 'dummy build'], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, env=self.env, creationflags=0x08000000 if os.name == 'nt' else 0)
        try:
            time.sleep(1.5)
            self.assertIsNone(process.poll(), 'the gate let work through while the machine was held')
            release(self.folder)
            out, err = process.communicate(timeout=30)
        finally:
            if process.poll() is None:
                process.kill()
        self.assertEqual(process.returncode, 0)
        self.assertEqual(json.loads(out)['gate'], 'free')
        self.assertIn('exclusive lease held by physbench', err)

    def test_gate_needs_a_purpose(self):
        done = self.run_gate()
        self.assertEqual(done.returncode, 2)
        self.assertIn('gate needs --purpose', done.stderr)


class ClaimAtOnceTests(unittest.TestCase):
    """A bench claims the machine while a full run is going and a build waits at the gate."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix='pc-claim-')

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def state(self):
        with leases.Store(self.folder) as state:
            return json.loads(json.dumps(state))

    def test_the_run_holds_at_its_next_stage_the_claim_is_granted_when_the_running_stage_ends_and_all_go_on_after(self):
        # The clock stands still: a grant that came only after some time had passed could not happen here.
        clock = Clock()
        hold = leases.RunHold('SensEssentials', folder=self.folder, names=lambda: set(), clock=clock, poll=0.02, beat_every=3600, say=lambda t: None)
        hold.start()
        hold.begin('torch')
        claim(self.folder, at=clock())
        self.assertEqual(self.state()['leases'], [], 'granted beside a running stage')
        gated, next_stage = threading.Event(), threading.Event()
        threading.Thread(target=lambda: (leases.gate('dummy build', folder=self.folder, poll=0.02, say=lambda t: None), gated.set()), daemon=True).start()
        threading.Thread(target=lambda: (hold.begin('quasar'), next_stage.set()), daemon=True).start()
        self.assertFalse(gated.wait(0.3), 'the gate let a build through while the claim waited')
        self.assertFalse(next_stage.is_set(), 'the next stage started while the claim waited')
        hold.end('torch')
        self.assertEqual([(l['session'], l['cores']) for l in self.state()['leases']], [('physbench', 'all')],
                         'the claim was not granted when the running stage ended')
        self.assertEqual(self.state()['leases'][0]['granted'], clock(), 'the grant came with no wait of its own')
        self.assertFalse(gated.wait(0.3), 'the gate let a build through while the claim was held')
        self.assertFalse(next_stage.is_set(), 'the next stage started while the claim was held')
        release(self.folder)
        self.assertTrue(gated.wait(5), 'the gate stayed shut after the release')
        self.assertTrue(next_stage.wait(5), 'the run did not go on after the release')
        hold.end('quasar')
        hold.close()

    def test_on_an_idle_machine_the_claim_is_granted_by_the_request_itself(self):
        with leases.Store(self.folder) as state:
            leases.request(state, 'physbench', 1, 50.0, True, 'PI a2 timing')
            self.assertEqual(leases.grant_when_idle(state, 50.0, CORES, containers=set()), ['all'])


if __name__ == '__main__':
    unittest.main()
