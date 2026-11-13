"""Slot lending, without Docker: python -m unittest discover -s tests"""
import json, os, shutil, sys, tempfile, threading, time, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'stages'))
import leases

CORES = [f'{i}-{i + 1}' for i in range(0, 32, 2)]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Decisions(unittest.TestCase):
    def setUp(self):
        self.state = leases.empty()

    def ask(self, session, count=2, at=0, exclusive=False):
        return leases.request(self.state, session, count, at, exclusive, 'test')

    def test_idle_machine_grants_at_once_lowest_cores_first(self):
        self.ask('mods')
        self.assertEqual(leases.grant_when_idle(self.state, 10, CORES), ['0-1', '2-3'])
        self.assertEqual([l['container'] for l in self.state['leases']], ['mods-1', 'mods-2'])
        self.assertEqual(self.state['requests'], [])

    def test_at_most_two_per_session_and_three_sessions(self):
        for name in ('a', 'b', 'c', 'd'):
            self.ask(name, 2)
        leases.grant_when_idle(self.state, 10, CORES)
        sessions = [l['session'] for l in self.state['leases']]
        self.assertEqual(sessions, ['a', 'a', 'b', 'b', 'c', 'c'])
        self.assertEqual([r['session'] for r in self.state['requests']], ['d'])
        # A count over the cap is held to it.
        self.state = leases.empty()
        self.ask('greedy', 5)
        leases.grant_when_idle(self.state, 10, CORES)
        self.assertEqual(len(self.state['leases']), 2)

    def test_running_owner_hands_over_only_finished_slots_first_come_first_served(self):
        self.state['owner'] = {'running': True, 'heartbeat': 100}
        self.ask('first', 1, at=101)
        self.ask('second', 2, at=102)
        self.assertEqual(leases.grant_when_idle(self.state, 103, CORES), [], 'no grant while PluginCheck runs')
        self.assertEqual(leases.owner_free(self.state, '6-7', 104)['session'], 'first')
        self.assertEqual(leases.owner_free(self.state, '8-9', 105)['session'], 'second')
        self.assertEqual(leases.owner_free(self.state, '10-11', 106)['session'], 'second')
        self.assertIsNone(leases.owner_free(self.state, '12-13', 107), 'nobody waits, so PluginCheck keeps it')

    def test_owner_that_stopped_beating_counts_as_idle(self):
        self.state['owner'] = {'running': True, 'heartbeat': 100}
        self.ask('mods', 1, at=100 + leases.OWNER_LIMIT + 1)
        self.assertEqual(leases.grant_when_idle(self.state, 100 + leases.OWNER_LIMIT + 1, CORES), ['0-1'])

    def test_heartbeat_keeps_a_lease_and_silence_ends_it(self):
        self.ask('mods', 1)
        leases.grant_when_idle(self.state, 0, CORES)
        leases.heartbeat(self.state, 'mods', 200)
        self.assertEqual(leases.expire(self.state, 200 + leases.HEARTBEAT_LIMIT), [])
        self.assertEqual(leases.expire(self.state, 201 + leases.HEARTBEAT_LIMIT), ['0-1'])
        self.assertEqual(self.state['leases'], [])

    def test_crashed_borrower_container_still_running_keeps_the_slot_out_of_use(self):
        self.ask('crashy', 1)
        leases.grant_when_idle(self.state, 0, CORES)
        running = {'crashy-1'}
        self.assertEqual(leases.expire(self.state, leases.HEARTBEAT_LIMIT + 1, lambda n: n in running), [])
        self.assertEqual([o['container'] for o in self.state['orphans']], ['crashy-1'])
        # Not lent again while the container runs.
        self.ask('other', 1, at=400)
        self.assertEqual(leases.grant_when_idle(self.state, 400, CORES), ['2-3'])
        running.clear()
        self.assertEqual(leases.expire(self.state, 500, lambda n: n in running), ['0-1'])

    def test_release_one_or_all(self):
        self.ask('mods', 2)
        leases.grant_when_idle(self.state, 0, CORES)
        self.assertEqual(leases.release(self.state, 'mods', '2-3'), ['2-3'])
        self.assertEqual(leases.release(self.state, 'mods'), ['0-1'])

    def test_exclusive_drains_everything(self):
        self.state['owner'] = {'running': True, 'heartbeat': 0}
        self.ask('mods', 1, at=1)
        leases.owner_free(self.state, '0-1', 2)
        self.ask('pam', 1, at=3, exclusive=True)
        self.ask('late', 2, at=4)
        self.assertFalse(leases.owner_may_start(self.state))
        self.assertIsNone(leases.owner_free(self.state, '2-3', 5), 'nothing new is lent while an exclusive request waits')
        self.assertFalse(leases.owner_grant_exclusive(self.state, 6, running=3), 'PluginCheck servers still run')
        self.assertFalse(leases.owner_grant_exclusive(self.state, 7, running=0), 'a slot is still lent')
        leases.release(self.state, 'mods')
        self.assertTrue(leases.owner_grant_exclusive(self.state, 8, running=0))
        self.assertEqual([l['cores'] for l in self.state['leases']], ['all'])
        self.assertFalse(leases.owner_may_start(self.state))
        self.assertEqual(leases.release(self.state, 'pam'), [])
        self.assertTrue(leases.owner_may_start(self.state))

    def test_exclusive_on_an_idle_machine(self):
        self.ask('pam', 1, exclusive=True)
        self.assertEqual(leases.grant_when_idle(self.state, 1, CORES), ['all'])
        self.ask('mods', 1, at=2)
        self.assertEqual(leases.grant_when_idle(self.state, 2, CORES), [])

    def test_waiting_request_without_heartbeat_is_dropped(self):
        self.state['owner'] = {'running': True, 'heartbeat': 0}
        self.ask('gone', 1, at=0)
        leases.expire(self.state, leases.HEARTBEAT_LIMIT + 1)
        self.assertEqual(self.state['requests'], [])

    def test_session_name_is_required(self):
        with self.assertRaises(ValueError):
            self.ask('')

    def test_borrower_log_path_rides_on_its_leases(self):
        leases.request(self.state, 'mods', 2, 0, log='C:/work/mods.log')
        leases.grant_when_idle(self.state, 1, CORES)
        self.assertEqual({l['log'] for l in self.state['leases']}, {'C:/work/mods.log'})
        leases.heartbeat(self.state, 'mods', 2, log='C:/work/mods-2.log')
        self.assertEqual({l['log'] for l in self.state['leases']}, {'C:/work/mods-2.log'})
        leases.heartbeat(self.state, 'mods', 3)
        self.assertEqual({l['log'] for l in self.state['leases']}, {'C:/work/mods-2.log'}, 'a heartbeat without --log keeps it')

    def test_session_name_cannot_be_a_pattern(self):
        # It goes into docker's name=^...$ filter and into container names.
        for bad in ('.*', 'a b', '-lead', '.hidden', 'x' * 65, 'mods$', 'a|b', 'plugin/check'):
            with self.assertRaises(ValueError, msg=bad):
                self.ask(bad)
        for good in ('pam', 'torch-fix.2', 'mod_test', 'x' * 64):
            self.ask(good)


class OwnerAndFile(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.clock = Clock()
        self.running = set()
        self.owner = leases.Owner(CORES, self.folder, lambda: set(self.running), self.clock)

    def tearDown(self):
        shutil.rmtree(self.folder)

    def borrow(self, session, count=1, exclusive=False):
        with leases.Store(self.folder) as state:
            leases.request(state, session, count, self.clock(), exclusive)
            leases.grant_when_idle(state, self.clock(), CORES)

    def test_run_leaves_out_slots_lent_before_it_started(self):
        self.borrow('mods', 2)
        self.assertEqual(self.owner.start(), {'0-1', '2-3'})

    def test_a_beat_after_stop_leaves_the_owner_stopped(self):
        """The heartbeat thread can beat once more after the run stopped lending. That beat must not mark the run as running again,
        or borrowers wait for slots of a run that has ended."""
        lending = leases.Lending(lambda: self.owner, lambda text: None)
        lending.beat(2)
        lending.stop()
        self.assertEqual(lending.beat(2), [])
        with leases.Store(self.folder) as state:
            self.assertFalse(state['owner']['running'])
            self.assertFalse(leases.owner_active(state, self.clock()))

    def test_a_change_is_saved_even_while_another_program_reads_the_file(self):
        """Another program may read leases.json without the lock. On Windows os.replace is refused while it has the file open, and
        giving up lost the change."""
        with leases.Store(self.folder) as state:
            state['requests'].append({'session': 'first'})
        path = os.path.join(self.folder, 'leases.json')
        reader = open(path, encoding='utf-8')
        threading.Timer(0.3, reader.close).start()
        with leases.Store(self.folder) as state:
            state['requests'].append({'session': 'second'})
        saved = json.load(open(path, encoding='utf-8'))
        self.assertEqual([r['session'] for r in saved['requests']], ['first', 'second'])
        self.assertFalse(os.path.exists(os.path.join(self.folder, 'leases.lock')))

    def test_the_lock_goes_even_while_a_waiter_has_its_holder_file_open(self):
        """Every waiter opens `holder` ten times a second. On Windows a file open elsewhere can't be deleted, and giving up at the first
        try left the lock behind, so every later call waited until it went stale."""
        lock = os.path.join(self.folder, 'leases.lock')
        with leases.Store(self.folder):
            reader = open(os.path.join(lock, 'holder'), encoding='utf-8')
            threading.Timer(0.3, reader.close).start()
        self.assertFalse(os.path.exists(lock))

    def test_one_owner_called_from_two_threads_keeps_its_lock(self):
        """behaviour.py calls one Owner from every worker thread. A call waiting for the lock must not make the call that holds it
        lose it: that left the lock behind, so every later call timed out at 60 s until the lock went stale."""
        self.owner.start()
        inside, go_on, second_done, errors = threading.Event(), threading.Event(), threading.Event(), []
        real = leases.owner_may_start

        def held_open(state):
            if threading.current_thread().name == 'first':
                inside.set()
                go_on.wait(10)
            return real(state)

        def call(done=None):
            try:
                self.owner.may_start()
            except Exception as error:
                errors.append(error)
            if done:
                done.set()

        leases.owner_may_start = held_open
        try:
            first = threading.Thread(target=call, name='first')
            first.start()
            self.assertTrue(inside.wait(5))
            threading.Thread(target=call, args=(second_done,), name='second', daemon=True).start()
            time.sleep(0.5)  # the second call is now waiting for the lock the first holds
            go_on.set()
            first.join(5)
            self.assertTrue(second_done.wait(5), 'the second call is still waiting, so the first left the lock behind')
            self.assertEqual(errors, [])
            self.assertFalse(os.path.exists(os.path.join(self.folder, 'leases.lock')))
        finally:
            go_on.set()
            leases.owner_may_start = real

    def test_lent_slot_comes_back_on_release_and_on_expiry(self):
        self.owner.start()
        self.borrow('a', 1)
        self.borrow('b', 1)
        self.assertEqual(self.owner.free('4-5')['session'], 'a')
        self.assertEqual(self.owner.free('6-7')['session'], 'b')
        with leases.Store(self.folder) as state:
            leases.release(state, 'a')
        self.assertEqual(self.owner.beat(10), ['4-5'])
        self.clock.now += leases.HEARTBEAT_LIMIT + 1
        self.assertEqual(self.owner.beat(10), ['6-7'])
        self.assertEqual(self.owner.beat(10), [])

    def test_crashed_borrower_slot_waits_for_its_container(self):
        self.owner.start()
        self.borrow('crashy', 1)
        self.owner.free('8-9')
        self.running.add('crashy-1')
        self.clock.now += leases.HEARTBEAT_LIMIT + 1
        self.assertEqual(self.owner.beat(5), [])
        self.running.clear()
        self.assertEqual(self.owner.beat(5), ['8-9'])

    def test_exclusive_waits_for_the_run_to_drain(self):
        self.owner.start()
        self.borrow('pam', 1, exclusive=True)
        self.assertFalse(self.owner.may_start())
        self.owner.beat(2)
        with leases.Store(self.folder) as state:
            self.assertEqual(state['leases'], [])
        self.owner.beat(0)
        with leases.Store(self.folder) as state:
            self.assertEqual([l['cores'] for l in state['leases']], ['all'])

    def hold(self, pid, age):
        lock = os.path.join(self.folder, 'leases.lock')
        os.makedirs(lock)
        with open(os.path.join(lock, 'holder'), 'w', encoding='utf-8') as out:
            json.dump({'pid': pid, 'time': time.time() - age, 'token': 'theirs'}, out)
        return lock

    def test_live_holder_keeps_the_lock_while_it_works(self):
        lock = self.hold(os.getpid(), 40)
        with self.assertRaises(TimeoutError):
            with leases.Store(self.folder, wait=1):
                pass
        self.assertTrue(os.path.exists(os.path.join(lock, 'holder')), 'a live holder 40 s in keeps it')

    def test_lock_of_a_dead_holder_is_taken_over(self):
        self.hold(2 ** 22 + 12345, 5)
        with leases.Store(self.folder, wait=5) as state:
            state['owner'] = None

    def test_lock_older_than_the_limit_is_taken_over_even_from_a_live_pid(self):
        self.hold(os.getpid(), leases.LOCK_STALE + 5)
        with leases.Store(self.folder, wait=5) as state:
            state['owner'] = None

    def test_a_holder_whose_lock_was_taken_over_neither_saves_nor_removes_it(self):
        store = leases.Store(self.folder)
        with self.assertRaises(RuntimeError):
            with store as state:
                state['leases'] = [{'stale': True}]
                # Someone took the lock over meanwhile.
                with open(os.path.join(self.folder, 'leases.lock', 'holder'), 'w', encoding='utf-8') as out:
                    json.dump({'pid': os.getpid(), 'time': time.time(), 'token': 'theirs'}, out)
        self.assertTrue(os.path.exists(os.path.join(self.folder, 'leases.lock', 'holder')))
        self.assertFalse(os.path.exists(os.path.join(self.folder, 'leases.json')), 'the stale change was not written')

    def test_stale_lock_is_taken_over(self):
        os.makedirs(os.path.join(self.folder, 'leases.lock'))
        old = time.time() - leases.LOCK_STALE - 5
        os.utime(os.path.join(self.folder, 'leases.lock'), (old, old))
        with leases.Store(self.folder) as state:
            state['owner'] = None
        self.assertFalse(os.path.exists(os.path.join(self.folder, 'leases.lock')))

    def test_an_error_inside_leaves_the_file_as_it_was(self):
        self.borrow('mods', 1)
        with self.assertRaises(RuntimeError):
            with leases.Store(self.folder) as state:
                state['leases'] = []
                raise RuntimeError('boom')
        with leases.Store(self.folder) as state:
            self.assertEqual(len(state['leases']), 1)


class Raising:
    """An Owner whose every call fails, as with a lock that times out or an unreadable file."""

    def __init__(self, fail_start=False):
        self.fail_start, self.calls = fail_start, 0

    def start(self):
        if self.fail_start:
            raise TimeoutError('leases.lock is held')
        return {'4-5'}

    def may_start(self):
        raise OSError('disk gone')

    def free(self, cores):
        raise ValueError('bad json')

    def beat(self, running):
        self.calls += 1
        raise TimeoutError('leases.lock is held')

    def stop(self):
        raise OSError('disk gone')


class LendingErrors(unittest.TestCase):
    def setUp(self):
        self.said = []

    def test_lending_off_when_the_owner_cannot_start(self):
        lending = leases.Lending(lambda: Raising(fail_start=True), self.said.append)
        self.assertIn('lending off for this run', self.said[0])
        self.assertEqual(lending.lent_at_start, set())
        self.assertTrue(lending.may_start())
        self.assertIsNone(lending.free('0-1'))
        self.assertEqual(lending.beat(3), [])
        lending.stop()

    def test_errors_keep_the_slot_and_the_run_going_and_are_logged_once_per_kind(self):
        owner = Raising()
        lending = leases.Lending(lambda: owner, self.said.append)
        self.assertEqual(lending.lent_at_start, {'4-5'})
        self.assertTrue(lending.may_start(), 'an error never holds servers back')
        self.assertIsNone(lending.free('0-1'), 'so the run keeps the slot')
        for _ in range(5):
            self.assertEqual(lending.beat(2), [])
        lending.stop()
        self.assertEqual(owner.calls, 5, 'beats go on after a failure')
        self.assertEqual(len([s for s in self.said if 'beat failed' in s]), 1)
        self.assertEqual(len(self.said), 4, self.said)

    def test_owner_heartbeat_is_saved_even_when_expiry_fails(self):
        folder = tempfile.mkdtemp()
        try:
            clock = Clock()
            owner = leases.Owner(CORES, folder, lambda: set(), clock)
            owner.start()
            real, leases.expire = leases.expire, lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom'))
            try:
                clock.now += 50
                with self.assertRaises(RuntimeError):
                    owner.beat(4)
            finally:
                leases.expire = real
            with leases.Store(folder) as state:
                self.assertEqual(state['owner']['heartbeat'], clock.now)
                self.assertEqual(state['owner']['servers'], 4)
                self.assertTrue(leases.owner_active(state, clock.now + leases.OWNER_LIMIT - 1), 'so lease.py does not take it for idle')
        finally:
            shutil.rmtree(folder)

    def test_docker_failure_in_names_counts_every_borrowed_container_as_running(self):
        folder = tempfile.mkdtemp()
        try:
            clock = Clock()
            def broken():
                raise OSError('docker gone')
            owner = leases.Owner(CORES, folder, broken, clock)
            owner.start()
            self.assertEqual(owner.beat(1), [])
        finally:
            shutil.rmtree(folder)


if __name__ == '__main__':
    unittest.main()
