"""The quiet-machine hold, without Docker: python -I -m unittest discover -s tests

An exclusive request waits for a full run's stages, not only for the behaviour stage's servers; a full run starts no Docker stage while
one waits or holds; any running container the lease file does not know holds the grant; lease.py tells borrowers.
"""
import glob, json, os, re, shutil, subprocess, sys, tempfile, threading, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(ROOT, 'stages'))
sys.path.insert(0, ROOT)
import leases
import lease

CORES = [f'{i}-{i + 1}' for i in range(0, 32, 2)]
ALIVE = lambda pid: True


def text(path):
    with open(path, encoding='utf-8') as file:
        return file.read()


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def exclusive(state, session='bench', at=1.0):
    return leases.request(state, session, 1, at, True, 'PI timing')


class BlockerTests(unittest.TestCase):
    def setUp(self):
        self.state = leases.empty()

    def test_a_stage_of_a_full_run_holds_the_grant(self):
        exclusive(self.state)
        leases.put_run(self.state, 5, 111, 'Demo', ['lint'])
        self.assertFalse(leases.owner_grant_exclusive(self.state, 6, 0))
        self.assertFalse(leases.grant_when_idle(self.state, 6, CORES))
        self.assertEqual(leases.exclusive_blockers(self.state, 6), ['stages of a full run: lint'])
        leases.put_run(self.state, 7, 111, 'Demo', [])
        self.assertTrue(leases.owner_grant_exclusive(self.state, 8, 0))

    def test_the_behaviour_stage_counts_its_own_servers_and_other_stages_count_as_stages(self):
        exclusive(self.state)
        leases.put_run(self.state, 5, 111, 'Demo', ['behaviour'])
        self.assertTrue(leases.exclusive_blockers(self.state, 6), 'the run itself waits while behaviour is going')
        self.assertTrue(leases.exclusive_blockers(self.state, 6, running=2, own_stage=True))
        leases.put_run(self.state, 7, 111, 'Demo', ['behaviour', 'dependencies'])
        self.assertEqual(leases.exclusive_blockers(self.state, 8, own_stage=True), ['stages of a full run: dependencies'])
        leases.put_run(self.state, 9, 111, 'Demo', ['behaviour'])
        self.assertEqual(leases.exclusive_blockers(self.state, 10, own_stage=True), [])
        self.assertTrue(leases.owner_grant_exclusive(self.state, 10, 0, own_stage=True))

    def test_a_run_that_stopped_beating_or_is_gone_does_not_hold(self):
        leases.put_run(self.state, 5, 111, 'Demo', ['torch'])
        self.assertEqual(leases.run_stages(self.state, 5 + leases.OWNER_LIMIT), ['torch'])
        self.assertEqual(leases.run_stages(self.state, 6 + leases.OWNER_LIMIT), [])
        leases.expire(self.state, 10, alive=lambda pid: False)
        self.assertEqual(self.state['runs'], [])
        leases.put_run(self.state, 10, 111, 'Demo', ['torch'])
        leases.expire(self.state, 11, alive=ALIVE)
        self.assertEqual(len(self.state['runs']), 1)

    def test_a_run_is_one_entry_per_process(self):
        leases.put_run(self.state, 1, 111, 'A', ['lint'])
        leases.put_run(self.state, 2, 222, 'B', ['torch'])
        leases.put_run(self.state, 3, 111, 'A', ['lint', 'nulls'])
        self.assertEqual([(r['pid'], r['stages']) for r in self.state['runs']], [(222, ['torch']), (111, ['lint', 'nulls'])])
        leases.drop_run(self.state, 222)
        self.assertEqual(leases.run_stages(self.state, 4), ['lint', 'nulls'])

    def test_a_run_may_begin_unless_an_exclusive_request_waits_or_holds(self):
        self.assertTrue(leases.run_may_begin(self.state))
        exclusive(self.state)
        self.assertFalse(leases.run_may_begin(self.state))
        leases.grant_when_idle(self.state, 2, CORES, containers=set())
        self.assertEqual([l['session'] for l in self.state['leases']], ['bench'])
        self.assertFalse(leases.run_may_begin(self.state), 'still held')
        leases.release(self.state, 'bench')
        self.assertTrue(leases.run_may_begin(self.state))


class UnknownContainerTests(unittest.TestCase):
    def setUp(self):
        self.state = leases.empty()
        exclusive(self.state)

    def test_an_unknown_container_holds_both_grants_and_is_named(self):
        for grant in (lambda: leases.grant_when_idle(self.state, 5, CORES, containers={'quasar-hold'}),
                      lambda: leases.owner_grant_exclusive(self.state, 5, 0, {'quasar-hold'})):
            self.assertFalse(grant())
        self.assertEqual(leases.exclusive_blockers(self.state, 5, 0, {'quasar-hold'}), ['containers the lease file does not know: quasar-hold'])
        self.assertTrue(leases.owner_grant_exclusive(self.state, 6, 0, set()))

    def test_a_leased_or_orphaned_container_is_known_to_the_lease_file(self):
        state = leases.empty()
        leases.request(state, 'mods', 1, 0)
        leases.grant_when_idle(state, 1, CORES)
        self.assertEqual(leases.unknown_containers(state, {'mods-1', 'stray'}), ['stray'])
        state['orphans'].append(state['leases'].pop())
        self.assertEqual(leases.unknown_containers(state, {'mods-1', 'stray'}), ['stray'])

    def test_the_holders_own_containers_are_not_unknown(self):
        leases.grant_when_idle(self.state, 5, CORES, containers=set())
        self.assertEqual(leases.unknown_containers(self.state, {'bench-1', 'bench-2', 'other'}), ['other'])

    def test_docker_that_cannot_be_asked_holds_the_grant(self):
        self.assertFalse(leases.owner_grant_exclusive(self.state, 5, 0, None))
        self.assertIn('Docker could not be asked', leases.exclusive_blockers(self.state, 5, 0, None)[0])
        # Not asked for at all (a caller that doesn't read the containers): as before.
        self.assertTrue(leases.owner_grant_exclusive(self.state, 6, 0))

    def test_docker_s_own_build_daemon_and_listed_prefixes_are_ignored(self):
        self.assertEqual(leases.unknown_containers(self.state, {'buildx_buildkit_default0', 'x'}), ['x'])
        saved = os.environ.get('PLUGINCHECK_IGNORE_CONTAINERS')
        os.environ['PLUGINCHECK_IGNORE_CONTAINERS'] = 'desktop-, other-'
        try:
            self.assertEqual(leases.unknown_containers(self.state, {'desktop-a', 'other-b', 'x'}), ['x'])
        finally:
            os.environ.pop('PLUGINCHECK_IGNORE_CONTAINERS', None)
            if saved is not None:
                os.environ['PLUGINCHECK_IGNORE_CONTAINERS'] = saved


class HoldTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix='pc-hold-')
        self.names = set()

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def state(self):
        with leases.Store(self.folder) as state:
            return json.loads(json.dumps(state))

    def hold(self, **kwargs):
        return leases.RunHold('Demo', folder=self.folder, names=lambda: set(self.names), poll=0.05, beat_every=0.05, say=lambda text: None, **kwargs)

    def request(self, exclusive_request=True):
        with leases.Store(self.folder) as state:
            exclusive(state, at=time.time())

    def test_a_stage_is_counted_while_it_runs_and_the_run_is_registered(self):
        hold = self.hold()
        hold.start()
        self.assertEqual([r['stages'] for r in self.state()['runs']], [[]])
        with hold.stage('lint'):
            self.assertEqual(self.state()['runs'][0]['stages'], ['lint'])
            self.assertEqual(self.state()['runs'][0]['pid'], os.getpid())
        self.assertEqual(self.state()['runs'][0]['stages'], [])
        hold.close()
        self.assertEqual(self.state()['runs'], [])

    def test_a_request_made_while_a_stage_runs_is_granted_when_the_stage_ends_and_not_before(self):
        hold = self.hold()
        hold.start()
        with hold.stage('torch'):
            self.request()
            time.sleep(0.3)  # the heartbeat thread tries the grant meanwhile
            self.assertEqual(self.state()['leases'], [])
        self.assertEqual([l['session'] for l in self.state()['leases']], ['bench'])
        hold.close()

    def test_a_stage_waits_while_a_request_waits_or_holds_and_then_starts(self):
        hold = self.hold()
        hold.start()
        self.request()
        # A container nobody leased keeps the request from being granted, so it is waiting, not holding.
        self.names = {'stray'}
        started, done = threading.Event(), threading.Event()

        def stage():
            with hold.stage('nulls'):
                started.set()
                done.wait(5)
        thread = threading.Thread(target=stage, daemon=True)
        thread.start()
        self.assertFalse(started.wait(0.5), 'a stage started while an exclusive request waited')
        self.assertEqual(self.state()['runs'][0]['stages'], [])
        self.names = set()
        hold.beat()
        self.assertEqual([l['session'] for l in self.state()['leases']], ['bench'])
        self.assertFalse(started.wait(0.5), 'a stage started while the machine was held')
        with leases.Store(self.folder) as state:
            leases.release(state, 'bench')
        self.assertTrue(started.wait(5), 'the stage never started after the hold ended')
        done.set()
        thread.join(5)
        hold.close()

    def test_a_lapsed_hold_lets_a_waiting_stage_go(self):
        """The holder died: its lease lapses after HEARTBEAT_LIMIT, and a waiting stage notices without anyone else asking."""
        clock = Clock()
        hold = leases.RunHold('Demo', folder=self.folder, names=lambda: set(), clock=clock, poll=0.05, beat_every=3600, say=lambda t: None)
        with leases.Store(self.folder) as state:
            exclusive(state, at=clock())
            leases.grant_when_idle(state, clock(), CORES, containers=set())
        started = threading.Event()

        def stage():
            with hold.stage('quasar'):
                started.set()
        threading.Thread(target=stage, daemon=True).start()
        self.assertFalse(started.wait(0.3))
        clock.now += leases.HEARTBEAT_LIMIT + 1
        self.assertTrue(started.wait(5), 'a hold whose holder stopped beating never ended')

    def test_a_lease_file_that_cannot_be_used_never_stops_a_run(self):
        notes = []
        hold = leases.RunHold('Demo', folder=self.folder, say=notes.append, poll=0.05, beat_every=3600, names=lambda: set())

        class Broken:
            def __init__(self, *args, **kwargs):
                raise PermissionError('held for a moment')
        real = leases.Store
        leases.Store = Broken
        try:
            with hold.stage('lint'):
                pass
            hold.close()
        finally:
            leases.Store = real
        self.assertEqual(len([n for n in notes if 'carrying on' in n]), 3)
        self.assertEqual(hold.stages, [])

    def test_closing_a_run_grants_a_waiting_request(self):
        hold = self.hold()
        hold.start()
        with hold.stage('lint'):
            self.request()
        hold.close()
        self.assertEqual([l['session'] for l in self.state()['leases']], ['bench'])
        self.assertEqual(self.state()['runs'], [])

    def test_the_heartbeat_keeps_the_run_fresh(self):
        hold = self.hold()
        hold.start()
        first = self.state()['runs'][0]['heartbeat']
        time.sleep(0.4)
        self.assertGreater(self.state()['runs'][0]['heartbeat'], first)
        hold.close()

    def test_stages_waiting_together_ask_docker_once_between_them(self):
        calls = []
        hold = leases.RunHold('Demo', folder=self.folder, names=lambda: calls.append(1) or set(), poll=60, say=lambda t: None)
        hold.read_names(60)
        hold.read_names(60)
        hold.read_names(60)
        self.assertEqual(len(calls), 1)
        hold.read_names()
        self.assertEqual(len(calls), 2)


class LeaseCliTests(unittest.TestCase):
    def setUp(self):
        self.state = leases.empty()

    def test_every_reply_says_whether_an_exclusive_request_waits_and_what_for(self):
        shown = lease.show(self.state, 'mods')
        self.assertEqual((shown['exclusiveWaiting'], shown['exclusiveHeld']), (False, False))
        self.assertNotIn('waitingFor', shown)
        exclusive(self.state)
        leases.put_run(self.state, time.time(), 1, 'Demo', ['lint'])
        shown = lease.show(self.state, 'mods', {'stray'})
        self.assertTrue(shown['exclusiveWaiting'])
        self.assertEqual(shown['waitingFor'], ['stages of a full run: lint', 'containers the lease file does not know: stray'])
        self.assertNotIn('unknownContainers', shown, 'a session sees what it asked about; status shows the rest')

    def test_status_names_the_unknown_containers_and_the_stages(self):
        shown = lease.show(self.state, None, {'quasar-hold', 'buildx_buildkit_default0'})
        self.assertEqual(shown['unknownContainers'], ['quasar-hold'])
        self.assertEqual(shown['stages'], [])
        self.assertEqual(lease.show(self.state, None, None)['unknownContainers'], None)
        self.assertNotIn('unknownContainers', lease.show(self.state, None))

    def test_held_is_reported(self):
        exclusive(self.state)
        leases.grant_when_idle(self.state, 5, CORES, containers=set())
        shown = lease.show(self.state, 'mods')
        self.assertEqual((shown['exclusiveWaiting'], shown['exclusiveHeld']), (False, True))

    def test_the_command_line_names_an_unknown_container_it_cannot_ask_docker_about(self):
        folder = tempfile.mkdtemp(prefix='pc-cli-')
        try:
            env = {k: v for k, v in os.environ.items() if k.upper() != 'PATH'}
            env.update({'PLUGINCHECK_MACHINE_DIR': folder, 'PATH': os.path.dirname(sys.executable)})
            run = lambda *args: json.loads(subprocess.run([sys.executable, '-I', os.path.join(ROOT, 'lease.py'), *args], capture_output=True, text=True,
                                                          env=env, check=True).stdout)
            asked = run('request', '--session', 'bench', '--exclusive', '--purpose', 'x')
            self.assertTrue(asked['exclusiveWaiting'])
            self.assertEqual(asked['leases'], [], 'granted with Docker out of reach')
            self.assertIn('Docker could not be asked', ' '.join(asked['waitingFor']))
            status = run('status')
            self.assertIsNone(status['unknownContainers'])
        finally:
            shutil.rmtree(folder, ignore_errors=True)


class CheckPyTests(unittest.TestCase):
    SOURCE = text(os.path.join(ROOT, 'check.py'))

    def no_docker(self):
        return set(re.findall(r"'(\w+)'", re.search(r'NO_DOCKER = \((.*)\)', self.SOURCE).group(1)))

    def test_a_stage_that_mentions_docker_is_gated(self):
        for name in self.no_docker() - {'LAST'}:
            path = os.path.join(ROOT, 'stages', name + '.py')
            self.assertTrue(os.path.exists(path), path)
            self.assertNotIn('docker', text(path).lower(), f'{name} starts containers, so it must not be in NO_DOCKER')
        self.assertNotIn('docker', text(os.path.join(ROOT, 'stages', 'logcheck.py')).lower())

    def test_every_other_stage_is_gated_and_the_gate_is_before_anything_starts(self):
        stages = set(re.findall(r"^    '(\w+)': 'stages/", self.SOURCE, re.M))
        self.assertIn('behaviour', stages)
        self.assertEqual(self.no_docker() - {'LAST'} - stages, set())
        source = self.SOURCE
        gate = source.index("gate = hold.stage(stage) if hold and stage not in NO_DOCKER")
        self.assertLess(gate, source.index('subprocess.Popen(command'))
        self.assertLess(source.index('reuse_stage(stage, key)'), gate, 'a reused stage starts no container, so it does not wait')

    def test_the_run_registers_for_its_life_except_in_the_lane(self):
        source = self.SOURCE
        self.assertIn('if not args.pre:\n    hold = leases.RunHold(', source)
        self.assertIn('atexit.register(hold.close)', source)
        self.assertLess(source.index('hold = leases.RunHold('), source.index('def game_build'))
        self.assertIn("hold.stage('game build')", source)
        self.assertLess(source.index('hold.close()\n'), source.index('display.close()'))


if __name__ == '__main__':
    unittest.main()
