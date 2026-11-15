"""The user's reserved cores and bench sessions' bigger grants, without Docker: python -I -m unittest discover -s tests

No slot, server, container or borrower ever uses the user's own cores; a bench session gets up to 4 adjacent slots, at most 3 bench
sessions borrow at once within the 3 sessions, and borrowers together leave PluginCheck at least OWN_FLOOR slots.
"""
import ast, contextlib, json, os, queue, shutil, subprocess, sys, tempfile, types, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(ROOT, 'stages'))
sys.path.insert(0, ROOT)
import leases, pin

# This machine: 32 logical cores, 24-31 the user's, so 12 slots on cores 0-23.
SLOTS = [f'{i}-{i + 1}' for i in range(0, 24, 2)]
ENV = ('PLUGINCHECK_USER_RESERVED_CORES', 'PLUGINCHECK_BENCH_SESSIONS', 'PLUGINCHECK_LEASE_CORES', 'PLUGINCHECK_CPUSET')
NO_WINDOW = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}


@contextlib.contextmanager
def environment(**values):
    saved = {name: os.environ.pop(name, None) for name in ENV}
    os.environ.update(values)
    try:
        yield
    finally:
        for name in ENV:
            os.environ.pop(name, None)
            if saved[name] is not None:
                os.environ[name] = saved[name]


@contextlib.contextmanager
def docker_says(answer):
    """pin's docker info replaced: answer is what it prints, or an exception to raise. Yields the calls made, as (command, options)."""
    asked = []

    def run(command, **options):
        asked.append((command, options))
        if isinstance(answer, BaseException):
            raise answer
        return types.SimpleNamespace(returncode=0, stdout=f'{answer}\n', stderr='')
    saved = pin.subprocess
    pin.subprocess = types.SimpleNamespace(run=run, SubprocessError=subprocess.SubprocessError, CREATE_NO_WINDOW=0x08000000)
    pin._docker_cores.clear()
    try:
        yield asked
    finally:
        pin.subprocess = saved
        pin._docker_cores.clear()


class ReservedCores(unittest.TestCase):
    def test_parsing_ranges_lists_and_empty(self):
        self.assertEqual(leases.parse_cores('24-31'), set(range(24, 32)))
        self.assertEqual(leases.parse_cores('8,9,12-15'), {8, 9, 12, 13, 14, 15})
        self.assertEqual(leases.parse_cores(' 30, 31 '), {30, 31})
        self.assertEqual(leases.parse_cores('7'), {7})
        self.assertEqual(leases.parse_cores(''), set())
        self.assertEqual(leases.parse_cores('  '), set())
        for bad in ('a', '31-24', '-1', '1-', '1-2-3', '24..31', '1;2'):
            with self.assertRaises(ValueError, msg=bad):
                leases.parse_cores(bad)

    def test_the_default_is_24_to_31_and_the_variable_overrides_it(self):
        with environment():
            self.assertEqual(leases.reserved_cores(), set(range(24, 32)))
        with environment(PLUGINCHECK_USER_RESERVED_CORES='0-3'):
            self.assertEqual(leases.reserved_cores(), {0, 1, 2, 3})
        with environment(PLUGINCHECK_USER_RESERVED_CORES=''):
            self.assertEqual(leases.reserved_cores(), set())

    def test_slots_never_touch_the_reserved_cores(self):
        with environment(PLUGINCHECK_LEASE_CORES='32'):
            self.assertEqual(leases.machine_cores(), SLOTS, '16 slots become 12, on cores 0-23')
            self.assertFalse(set(leases.slot_cores(leases.machine_cores())) & set(range(24, 32)))
        with environment(PLUGINCHECK_LEASE_CORES='32', PLUGINCHECK_USER_RESERVED_CORES='25-31'):
            self.assertEqual(leases.machine_cores(), SLOTS, 'a slot with one reserved core (24-25) is left out')
        with environment(PLUGINCHECK_LEASE_CORES='32', PLUGINCHECK_USER_RESERVED_CORES='0,1,30,31'):
            self.assertEqual(leases.machine_cores(), [f'{i}-{i + 1}' for i in range(2, 30, 2)])
        with environment(PLUGINCHECK_LEASE_CORES='32', PLUGINCHECK_USER_RESERVED_CORES=''):
            self.assertEqual(len(leases.machine_cores()), 16, 'none reserved: as before')
        with environment(PLUGINCHECK_LEASE_CORES='16'):
            self.assertEqual(len(leases.machine_cores()), 8, 'reserved cores the machine does not have change nothing')

    def test_cpusets(self):
        self.assertEqual(leases.cpuset(range(24)), '0-23')
        self.assertEqual(leases.cpuset([0, 1, 2, 3, 16, 17, 5]), '0-3,5,16-17')
        with environment(PLUGINCHECK_LEASE_CORES='32'):
            self.assertEqual(leases.unreserved_cpuset(), '0-23')
        with environment(PLUGINCHECK_LEASE_CORES='32', PLUGINCHECK_USER_RESERVED_CORES='8-15'):
            self.assertEqual(leases.unreserved_cpuset(), '0-7,16-31')
        with environment(PLUGINCHECK_LEASE_CORES='32', PLUGINCHECK_USER_RESERVED_CORES=''):
            self.assertEqual(leases.unreserved_cpuset(), '')
        with environment(PLUGINCHECK_LEASE_CORES='16'):
            self.assertEqual(leases.unreserved_cpuset(), '', 'none of the machine is reserved: nothing pinned')

    def test_containers_outside_a_slot_are_pinned_off_the_reserved_cores(self):
        with environment(PLUGINCHECK_LEASE_CORES='32'), docker_says(32):
            self.assertEqual(pin.args(), ['--cpuset-cpus', '0-23'])
        with environment(PLUGINCHECK_LEASE_CORES='32', PLUGINCHECK_CPUSET='6-7'), docker_says(32) as asked:
            self.assertEqual(pin.args(), ['--cpuset-cpus', '6-7'], 'the lane pins to its own slot, as before')
            self.assertEqual(asked, [], 'the lane needs no count')

    def test_the_pin_counts_the_cores_docker_has(self):
        """Docker Desktop can have fewer cores than this computer, and Docker refuses a cpuset naming a core it lacks: pin.args() counts
        Docker's (docker info's NCPU), as capacity() does, not os.cpu_count()."""
        with environment(PLUGINCHECK_LEASE_CORES='32', PLUGINCHECK_USER_RESERVED_CORES='8-11'), docker_says(16) as asked:
            self.assertEqual(pin.args(), ['--cpuset-cpus', '0-7,12-15'], "only Docker's 16 cores, less the reserved 8-11")
            self.assertEqual(pin.args(), ['--cpuset-cpus', '0-7,12-15'])
            self.assertEqual(len(asked), 1, 'Docker is asked once per process')
            command, options = asked[0]
            self.assertEqual(command, ['docker', 'info', '--format', '{{.NCPU}}'])
            self.assertEqual(options['timeout'], 120)
            if os.name == 'nt':
                self.assertEqual(options['creationflags'], 0x08000000, 'no window')
        with environment(PLUGINCHECK_LEASE_CORES='32'), docker_says(16):
            self.assertEqual(pin.args(), [], "the user's 24-31 are not among Docker's 16, so nothing is pinned, as capacity() keeps none back")
        with environment(PLUGINCHECK_LEASE_CORES='32', PLUGINCHECK_USER_RESERVED_CORES=''), docker_says(16) as asked:
            self.assertEqual(pin.args(), [])
            self.assertEqual(asked, [], 'nothing reserved: Docker is not asked')
        with environment(PLUGINCHECK_LEASE_CORES='32'), docker_says(subprocess.TimeoutExpired('docker', 120)) as asked:
            self.assertEqual(pin.args(), ['--cpuset-cpus', '0-23'], "no answer from Docker: this computer's count, as before")
            pin.args()
            self.assertEqual(len(asked), 2, 'no answer is not kept: the next container asks again')

    def test_the_pin_and_the_slots_use_the_same_cores(self):
        for ncpu, reserved in ((16, '8-11'), (32, '24-31'), (24, '0-3,20-31'), (12, '')):
            with self.subTest(ncpu=ncpu, reserved=reserved), environment(PLUGINCHECK_LEASE_CORES='32', PLUGINCHECK_USER_RESERVED_CORES=reserved), \
                    docker_says(ncpu):
                _, slots, _ = self.capacity(ncpu)
                pinned = pin.args()
                cores = leases.parse_cores(pinned[1]) if pinned else set(range(ncpu))
                self.assertEqual(set(leases.slot_cores(slots)), cores)

    def capacity(self, ncpu, memory_gib=54, workers=None):
        """behaviour.py's capacity(), lifted out with Docker's answer replaced."""
        with open(os.path.join(ROOT, 'stages', 'behaviour.py'), encoding='utf-8') as source:
            tree = ast.parse(source.read())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'capacity')
        printed = []
        space = {'os': os, 'queue': queue, 'leases': leases, 'CORES_PER_SERVER': 2, 'MEMORY_PER_SERVER_GIB': 1.8, 'print': printed.append,
                 'docker': lambda *args: types.SimpleNamespace(stdout=f'{ncpu} {memory_gib * 2 ** 30}')}
        exec(compile(ast.Module([node], []), 'behaviour.py', 'exec'), space)
        saved = os.environ.pop('PLUGINCHECK_BEHAVIOUR_WORKERS', None)
        if workers:
            os.environ['PLUGINCHECK_BEHAVIOUR_WORKERS'] = str(workers)
        try:
            count, slots = space['capacity']()
        finally:
            os.environ.pop('PLUGINCHECK_BEHAVIOUR_WORKERS', None)
            if saved is not None:
                os.environ['PLUGINCHECK_BEHAVIOUR_WORKERS'] = saved
        return count, list(slots.queue), printed

    def test_the_behaviour_stage_runs_12_servers_on_cores_0_to_23(self):
        with environment():
            count, slots, printed = self.capacity(32)
            self.assertEqual((count, slots), (12, SLOTS))
            self.assertIn('cores 24-31 kept for the user', printed[0])
            count, slots, _ = self.capacity(32, workers=14)
            self.assertEqual(slots, SLOTS + SLOTS[:2], 'more workers than slots share them, never a reserved core')
            self.assertEqual(self.capacity(32, memory_gib=9)[:2], (5, SLOTS[:5]), 'memory still limits')
        with environment(PLUGINCHECK_USER_RESERVED_CORES=''):
            self.assertEqual(self.capacity(32)[1], [f'{i}-{i + 1}' for i in range(0, 32, 2)])
        with environment(PLUGINCHECK_USER_RESERVED_CORES='0-31'):
            with self.assertRaises(SystemExit):
                self.capacity(32)


class Bench(unittest.TestCase):
    def setUp(self):
        self.state = leases.empty()
        self.env = environment()
        self.env.__enter__()

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def ask(self, session, count, at=0, exclusive=False):
        return leases.request(self.state, session, count, at, exclusive, 'test')

    def holding(self, session):
        return [l['cores'] for l in leases.held(self.state, session)]

    def running(self, at=0):
        self.state['owner'] = {'running': True, 'heartbeat': at, 'cores': SLOTS}

    def test_a_bench_session_gets_4_adjacent_slots_on_an_idle_machine(self):
        self.ask('physbench-a', 4)
        self.assertEqual(leases.grant_when_idle(self.state, 1, SLOTS), ['0-1', '2-3', '4-5', '6-7'])
        self.assertEqual(self.state['requests'], [])
        self.assertEqual(leases.cpuset(leases.slot_cores(self.holding('physbench-a'))), '0-7')

    def test_the_pattern_is_configurable(self):
        with environment(PLUGINCHECK_BENCH_SESSIONS='inertia-*'):
            self.assertFalse(leases.bench('physbench-a'))
            self.assertTrue(leases.bench('inertia-1'))
        with environment(PLUGINCHECK_BENCH_SESSIONS=''):
            self.assertFalse(leases.bench('physbench-a'), 'empty: no bench sessions')
        self.assertFalse(leases.bench('PhysBench-a'), 'case sensitive, as container names are')
        self.assertFalse(leases.bench(leases.LANE))

    def test_other_sessions_keep_2_slots_and_3_sessions(self):
        self.ask('mods', 4)
        leases.grant_when_idle(self.state, 1, SLOTS)
        self.assertEqual(self.holding('mods'), ['0-1', '2-3'])
        for name in ('b', 'c', 'd'):
            self.ask(name, 2, at=2)
        leases.grant_when_idle(self.state, 3, SLOTS)
        self.assertEqual(sorted({l['session'] for l in self.state['leases']}), ['b', 'c', 'mods'])
        self.assertEqual([r['session'] for r in self.state['requests']], ['d'])
        self.assertEqual(len(self.state['leases']), 6)

    def test_at_most_3_bench_sessions_and_they_count_among_the_3_sessions(self):
        for name in ('physbench-a', 'physbench-b', 'physbench-c', 'physbench-d'):
            self.ask(name, 1)
        leases.grant_when_idle(self.state, 1, SLOTS)
        self.assertEqual([r['session'] for r in self.state['requests']], ['physbench-d'])
        self.state = leases.empty()
        self.ask('physbench-a', 2)
        self.ask('physbench-b', 2, at=1)
        self.ask('mods', 2, at=2)
        self.ask('other', 1, at=3)
        leases.grant_when_idle(self.state, 4, SLOTS)
        self.assertEqual([r['session'] for r in self.state['requests']], ['other'], 'one total of 3 sessions')
        with self.subTest('BENCH_MAX_SESSIONS limits benches on its own'):
            saved = leases.BENCH_MAX_SESSIONS
            leases.BENCH_MAX_SESSIONS = 1
            try:
                self.state = leases.empty()
                self.ask('physbench-a', 1)
                self.ask('physbench-b', 1, at=1)
                self.ask('mods', 1, at=2)
                leases.grant_when_idle(self.state, 3, SLOTS)
                self.assertEqual(sorted({l['session'] for l in self.state['leases']}), ['mods', 'physbench-a'])
            finally:
                leases.BENCH_MAX_SESSIONS = saved

    def free(self, *slots, at=10):
        """A run's servers finish in these slots, in turn: who got each (None: PluginCheck kept it)."""
        return [(lease or {}).get('session') for lease in (leases.owner_free(self.state, c, at + i) for i, c in enumerate(slots))]

    def test_with_no_run_three_benches_at_4_take_all_12_slots(self):
        """The floor is for a run going. With none, 3 x 4 = 12 benches take every unreserved slot."""
        for at, name in enumerate(('physbench-a', 'physbench-b', 'physbench-c')):
            self.ask(name, 4, at=at)
        self.assertEqual(len(leases.grant_when_idle(self.state, 5, SLOTS)), 12)
        self.assertEqual(self.holding('physbench-a'), ['0-1', '2-3', '4-5', '6-7'])
        self.assertEqual(self.holding('physbench-b'), ['8-9', '10-11', '12-13', '14-15'])
        self.assertEqual(self.holding('physbench-c'), ['16-17', '18-19', '20-21', '22-23'])

    def test_during_a_run_the_floor_two_benches_at_4_and_the_third_waits(self):
        """While a run is going borrowers get no slot past 12 - OWN_FLOOR = 8: two blocks of 4, and the third waits."""
        self.assertEqual(leases.OWN_FLOOR, 4)
        self.running()
        for at, name in enumerate(('physbench-a', 'physbench-b', 'physbench-c')):
            self.ask(name, 4, at=at)
        self.assertEqual(self.free(*SLOTS), ['physbench-a'] * 4 + ['physbench-b'] * 4 + [None] * 4)
        self.assertEqual(self.holding('physbench-a'), ['0-1', '2-3', '4-5', '6-7'])
        self.assertEqual(self.holding('physbench-c'), [])
        leases.release(self.state, 'physbench-a')
        self.assertEqual(self.free('0-1', '2-3', '4-5', '6-7', at=30), ['physbench-c'] * 4)

    def test_during_a_run_three_benches_asking_less_all_run(self):
        self.running()
        self.ask('physbench-a', 4)
        self.ask('physbench-b', 2, at=1)
        self.ask('physbench-c', 2, at=2)
        self.free(*SLOTS)
        self.assertEqual([len(self.holding(s)) for s in ('physbench-a', 'physbench-b', 'physbench-c')], [4, 2, 2])

    def test_during_a_run_the_floor_gives_fewer_adjacent_slots_and_the_block_grows_when_room_comes_back(self):
        self.running()
        self.ask('physbench-a', 4)
        self.ask('mods', 2, at=1)
        self.ask('other', 2, at=2)
        self.free(*SLOTS[:8])
        self.assertEqual(len(leases.borrowed(self.state)), 8)
        self.ask('physbench-b', 4, at=4)
        self.assertEqual(self.free('16-17', at=20), [None], 'no room, and the sessions are full')
        leases.release(self.state, 'other')
        leases.release(self.state, 'mods', '10-11')
        # Room for 3: physbench-b gets 3 adjacent slots, not 4.
        self.assertEqual(self.free('16-17', '18-19', '20-21', '22-23', at=30), ['physbench-b'] * 3 + [None])
        leases.release(self.state, 'mods')
        self.assertEqual(self.free('22-23', at=40), ['physbench-b'])
        self.assertEqual(leases.cpuset(leases.slot_cores(self.holding('physbench-b'))), '16-23', 'still adjacent')

    def test_during_a_run_orphans_count_against_the_floor(self):
        self.running()
        self.state['orphans'] = [{'session': 'gone', 'cores': c, 'container': f'gone-{i}'} for i, c in enumerate(SLOTS[:5])]
        self.ask('physbench-a', 4)
        self.assertEqual(self.free('10-11', '12-13', '14-15', '16-17'), ['physbench-a'] * 3 + [None], 'room for 3 only, adjacent')

    def test_fragmented_slots_make_a_bench_wait_rather_than_split(self):
        for cores in ('4-5', '12-13', '18-19'):
            self.state['leases'].append({'session': 'mods' if cores != '18-19' else 'other', 'cores': cores, 'index': 1, 'granted': 0,
                                         'heartbeat': 0, 'container': 'x'})
        # Open runs: 0-3 (2 slots), 6-11 (3), 14-17 (2), 20-23 (2): no 4 anywhere.
        self.ask('physbench-a', 4, at=1)
        leases.grant_when_idle(self.state, 2, SLOTS)
        self.assertEqual(self.holding('physbench-a'), [], 'it waits')
        self.state['leases'] = [l for l in self.state['leases'] if l['cores'] != '12-13']
        leases.grant_when_idle(self.state, 3, SLOTS)
        self.assertEqual(self.holding('physbench-a'), ['6-7', '8-9', '10-11', '12-13'])

    def test_during_a_run_the_block_grows_as_neighbouring_servers_finish(self):
        self.running()
        self.ask('physbench-a', 4, at=1)
        self.ask('mods', 1, at=2)
        self.assertEqual(leases.owner_free(self.state, '10-11', 3)['session'], 'physbench-a', 'the first slot starts the block')
        self.assertEqual(leases.owner_free(self.state, '0-1', 4)['session'], 'mods', 'not next to the block: the next request gets it')
        self.ask('early', 1, at=0)
        self.assertEqual(leases.owner_free(self.state, '8-9', 7)['session'], 'physbench-a', 'next to the block goes to it first, even before an older request')
        self.assertEqual(leases.owner_free(self.state, '12-13', 8)['session'], 'physbench-a')
        self.assertEqual(leases.owner_free(self.state, '14-15', 9)['session'], 'physbench-a')
        self.assertEqual(self.holding('physbench-a'), ['10-11', '8-9', '12-13', '14-15'])
        self.assertEqual(leases.cpuset(leases.slot_cores(self.holding('physbench-a'))), '8-15')
        self.assertEqual(leases.owner_free(self.state, '16-17', 10)['session'], 'early', 'the block is whole')

    def test_no_slot_is_lent_where_it_would_leave_a_growing_block_no_room(self):
        self.running()
        for cores, session in (('4-5', 'x'), ('14-15', 'y')):
            self.state['leases'].append({'session': session, 'cores': cores, 'index': 1, 'granted': 0, 'heartbeat': 0, 'container': f'{session}-1'})
        self.ask('mods', 1, at=1)
        self.ask('physbench-a', 4, at=2)
        self.assertEqual(leases.owner_free(self.state, '8-9', 3)['session'], 'mods', 'mods asked first, and the block could still fit')
        # Now the open run around 10-11 is 10-11, 12-13: too short, and around 6-7 it is just 6-7.
        self.assertIsNone(leases.owner_free(self.state, '10-11', 4), 'no place for the whole block: it waits')
        self.state['leases'] = [l for l in self.state['leases'] if l['session'] != 'mods']
        self.ask('mods', 1, at=5)
        self.assertEqual(leases.owner_free(self.state, '8-9', 6)['session'], 'physbench-a', 'mods asked again later: the bench is first now')
        # The open run is 6-7 to 12-13, exactly 4. Giving 12-13 to mods would leave 3, so nobody gets it, and the next one grows the block.
        self.assertIsNone(leases.owner_free(self.state, '12-13', 7))
        self.assertEqual(leases.owner_free(self.state, '10-11', 8)['session'], 'physbench-a')

    def test_a_block_container_keeps_all_its_slots_out_of_use(self):
        self.ask('physbench-a', 4)
        leases.grant_when_idle(self.state, 0, SLOTS)
        running = {'physbench-a-1'}
        self.assertEqual(leases.expire(self.state, leases.HEARTBEAT_LIMIT + 1, lambda n: n in running), [])
        self.assertEqual(len(self.state['orphans']), 4, 'one container over the block holds all four slots')
        running.clear()
        self.assertEqual(sorted(leases.expire(self.state, leases.HEARTBEAT_LIMIT + 2, lambda n: n in running)), ['0-1', '2-3', '4-5', '6-7'])

    def test_asking_again_keeps_the_slots_a_growing_block_already_has(self):
        """lease.py says to call request or heartbeat again until the count is reached. A block that grows for longer than
        HEARTBEAT_LIMIT lost its first slots when the borrower only asked again (seen live)."""
        self.running()
        self.ask('physbench-a', 4, at=0)
        leases.owner_free(self.state, '6-7', 1)
        self.ask('physbench-a', 4, at=leases.HEARTBEAT_LIMIT)
        self.assertEqual(leases.expire(self.state, leases.HEARTBEAT_LIMIT + 2), [])
        self.assertEqual(self.holding('physbench-a'), ['6-7'])

    def test_an_exclusive_request_clears_bench_grants_as_it_does_others(self):
        self.running()
        self.ask('physbench-a', 4, at=1)
        self.ask('mods', 2, at=2)
        leases.owner_free(self.state, '0-1', 3)
        leases.owner_free(self.state, '2-3', 4)
        leases.owner_free(self.state, '20-21', 5)
        self.assertEqual((len(self.holding('physbench-a')), len(self.holding('mods'))), (2, 1))
        self.ask('pam', 1, at=6, exclusive=True)
        self.assertFalse(leases.owner_may_start(self.state))
        self.assertIsNone(leases.owner_free(self.state, '4-5', 7), 'the growing block gets nothing more')
        self.assertIsNone(leases.owner_free(self.state, '18-19', 8), 'nor the other borrower')
        self.assertFalse(leases.owner_grant_exclusive(self.state, 9, running=0))
        self.assertIn('leases held by mods, physbench-a', leases.exclusive_blockers(self.state, 9))
        leases.release(self.state, 'physbench-a')
        self.assertFalse(leases.owner_grant_exclusive(self.state, 10, running=0))
        leases.release(self.state, 'mods')
        self.assertTrue(leases.owner_grant_exclusive(self.state, 11, running=0))
        self.assertEqual([l['cores'] for l in self.state['leases']], ['all'])
        self.state['owner']['running'] = False
        self.ask('physbench-a', 4, at=12)
        self.assertEqual(leases.grant_when_idle(self.state, 12, SLOTS), [], 'nothing is lent while it holds')
        leases.release(self.state, 'pam')
        self.assertEqual(len(leases.grant_when_idle(self.state, 13, SLOTS)), 4)

    def test_the_lane_is_inside_the_floor(self):
        self.running()
        self.ask('physbench-a', 4)
        self.ask('physbench-b', 4, at=1)
        self.free(*SLOTS[:8])
        leases.request(self.state, leases.LANE, 1, 3)
        self.ask('mods', 1, at=4)
        self.assertEqual(self.free('16-17', '18-19', at=20), [leases.LANE, None], "the lane is PluginCheck's own slot, not a borrower's")


class CommandLine(unittest.TestCase):
    def run_lease(self, folder, *args, check=True):
        env = {k: v for k, v in os.environ.items() if k.upper() != 'PATH' and k not in ENV}
        env.update({'PLUGINCHECK_MACHINE_DIR': folder, 'PATH': os.path.dirname(sys.executable), 'PLUGINCHECK_LEASE_CORES': '32'})
        return subprocess.run([sys.executable, '-I', os.path.join(ROOT, 'lease.py'), *args], capture_output=True, text=True, env=env, check=check,
                              stdin=subprocess.DEVNULL, **NO_WINDOW)

    def test_a_bench_asks_for_4_and_gets_one_cpuset_others_are_refused_more_than_2(self):
        folder = tempfile.mkdtemp(prefix='pc-rc-')
        try:
            refused = self.run_lease(folder, 'request', '--session', 'mods', '--count', '4', check=False)
            self.assertEqual(refused.returncode, 2)
            self.assertIn('at most 2', refused.stderr)
            shown = json.loads(self.run_lease(folder, 'request', '--session', 'physbench-test', '--count', '4', '--purpose', 'x').stdout)
            self.assertEqual([l['cores'] for l in shown['leases']], ['0-1', '2-3', '4-5', '6-7'])
            self.assertEqual(shown['cpuset'], '0-7')
            shown = json.loads(self.run_lease(folder, 'request', '--session', 'mods', '--count', '2').stdout)
            self.assertEqual(shown['cpuset'], '8-11')
            status = json.loads(self.run_lease(folder, 'status').stdout)
            self.assertFalse({c for l in status['leases'] for c in leases.slot_cores([l['cores']])} & set(range(24, 32)))
        finally:
            shutil.rmtree(folder, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
