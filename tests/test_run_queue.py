"""One PluginCheck run at a time, without Docker: python -I -m unittest discover -s tests

A second check.py waits for the run before it to end, whatever lease folder either uses; the machine gate stays shut while a run is
going, except for a session with an exclusive claim of its own; exclusive requests work as before.
"""
import ctypes, os, re, shutil, subprocess, sys, tempfile, threading, time, unittest, uuid
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(ROOT, 'stages'))
import leases

NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

HOLDER = r'''
import os, sys, time
sys.path.insert(0, sys.argv[1])
import leases
queue = leases.RunQueue(sys.argv[2])
assert queue.take(30)
open(sys.argv[3], 'w').close()
while not os.path.exists(sys.argv[4]):
    time.sleep(0.05)
if sys.argv[5] == 'crash':
    os._exit(1)
queue.release()
'''


class Holder:
    """Another process, standing in for a run from another copy: it takes the turn and keeps it until stopped."""

    def __init__(self, name, how='release'):
        self.folder = tempfile.mkdtemp(prefix='pc-queue-')
        self.held, self.stop = os.path.join(self.folder, 'held'), os.path.join(self.folder, 'stop')
        self.process = subprocess.Popen([sys.executable, '-I', '-c', HOLDER, os.path.join(ROOT, 'stages'), name, self.held, self.stop, how],
                                        creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
        deadline = time.time() + 30
        while not os.path.exists(self.held):
            if time.time() > deadline or self.process.poll() is not None:
                raise RuntimeError('the holder did not take the turn')
            time.sleep(0.05)

    def end(self):
        open(self.stop, 'w').close()
        self.process.wait(30)
        shutil.rmtree(self.folder, ignore_errors=True)


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.name = 'pc-test-' + uuid.uuid4().hex

    def test_a_second_run_waits_until_the_first_ends(self):
        holder = Holder(self.name)
        try:
            queue = leases.RunQueue(self.name)
            self.assertFalse(queue.take(0))
            self.assertTrue(queue.going())
            said, waited = [], []
            thread = threading.Thread(target=lambda: waited.append(queue.wait(say=said.append, poll=0.2)))
            thread.start()
            time.sleep(0.8)
            self.assertTrue(thread.is_alive(), 'it waits while the first run holds the turn')
            self.assertIn('another PluginCheck run is going: one at a time, so this run waits for its turn', said[0])
        finally:
            holder.end()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertIn('the run before has ended', said[-1])
        self.assertGreater(waited[0], 0.5)
        # The holding process is not going for itself, and another look sees it held.
        self.assertTrue(queue.take(0))
        self.assertFalse(queue.going())

    def test_a_run_that_ends_without_releasing_frees_the_turn(self):
        holder = Holder(self.name, how='crash')
        holder.end()
        queue = leases.RunQueue(self.name)
        self.assertTrue(queue.take(5), 'the operating system frees it with the process')
        queue.release()

    @unittest.skipUnless(os.name == 'nt', 'the named mutex is Windows only')
    def test_a_wait_the_system_refuses_is_an_error_not_a_turn_still_held(self):
        """WaitForSingleObject gives WAIT_FAILED at once: counted as a turn still held, wait() would ask again and again without end."""
        real = ctypes.WinDLL('kernel32', use_last_error=True)
        asked = []

        class Kernel:
            CreateMutexW, CloseHandle = real.CreateMutexW, real.CloseHandle

            class WaitForSingleObject:
                def __call__(self, handle, milliseconds):
                    asked.append(milliseconds)
                    if len(asked) > 50:
                        raise RuntimeError('it asks again and again')
                    return 0xFFFFFFFF
            WaitForSingleObject = WaitForSingleObject()
        with mock.patch.object(ctypes, 'WinDLL', lambda *args, **kwargs: Kernel):
            queue = leases.RunQueue(self.name)
            with self.assertRaises(OSError) as raised:
                queue.take(0)
            self.assertIn('could not be waited on', str(raised.exception))
            self.assertIsNone(queue.handle)
            asked.clear()
            with self.assertRaises(OSError):
                queue.wait(say=lambda text: None, poll=0)
            self.assertEqual(len(asked), 1, 'wait() stops at the first refusal')

    def test_free_at_once_with_no_run(self):
        queue = leases.RunQueue(self.name)
        said = []
        self.assertLess(queue.wait(say=said.append), 1)
        self.assertEqual(said, [], 'nothing to say with no run before it')
        queue.release()
        self.assertFalse(leases.RunQueue(self.name).going())

    def test_the_name_comes_from_the_environment_for_tests(self):
        saved = os.environ.pop('PLUGINCHECK_RUN_QUEUE', None)
        try:
            self.assertEqual(leases.RunQueue().name, leases.RUN_QUEUE)
            os.environ['PLUGINCHECK_RUN_QUEUE'] = self.name
            self.assertEqual(leases.RunQueue().name, self.name)
        finally:
            os.environ.pop('PLUGINCHECK_RUN_QUEUE', None)
            if saved is not None:
                os.environ['PLUGINCHECK_RUN_QUEUE'] = saved


class GateTests(unittest.TestCase):
    def setUp(self):
        self.name = 'pc-test-' + uuid.uuid4().hex
        self.folder = tempfile.mkdtemp(prefix='pc-gate-')

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def gate(self, **kwargs):
        return leases.gate('dummy build', folder=self.folder, queue=self.name, say=lambda text: None, **kwargs)

    def test_the_gate_is_shut_while_a_run_is_going_and_opens_when_it_ends(self):
        with leases.Store(self.folder) as state:
            leases.put_run(state, time.time(), 4242, 'Monitor', [])
        holder = Holder(self.name)
        try:
            with self.assertRaises(leases.GateTimeout) as shut:
                self.gate(timeout=0)
            self.assertIn('a PluginCheck run is going (Monitor): one at a time', str(shut.exception))
            ended = threading.Timer(0.5, holder.end)
            ended.start()
            self.assertGreater(self.gate(timeout=20, poll=0.1), 0.3)
        finally:
            if holder.process.poll() is None:
                holder.end()

    def test_a_session_with_its_own_exclusive_claim_is_not_held_back_by_a_run(self):
        holder = Holder(self.name)
        try:
            with leases.Store(self.folder) as state:
                leases.request(state, 'physbench', 1, time.time(), exclusive=True, purpose='timing block')
            self.assertEqual(round(self.gate(timeout=0, session='physbench')), 0, 'its own build goes; the run holds for its claim')
            with self.assertRaises(leases.GateTimeout) as shut:
                self.gate(timeout=0, session='other')
            self.assertIn('exclusive request by physbench', str(shut.exception), 'others still wait for the claim first')
            with self.assertRaises(leases.GateTimeout):
                self.gate(timeout=0)
        finally:
            holder.end()

    def test_with_no_run_and_no_claim_the_gate_is_free(self):
        self.assertEqual(round(self.gate(timeout=0)), 0)


class CheckPyTests(unittest.TestCase):
    def test_a_full_run_takes_the_turn_before_it_registers_and_the_lane_does_not(self):
        with open(os.path.join(ROOT, 'check.py'), encoding='utf-8') as file:
            source = file.read()
        self.assertIn('if not args.pre:\n    turn = leases.RunQueue()\n    turn.wait(', source)
        self.assertIn('atexit.register(turn.release)', source)
        self.assertLess(source.index('turn.wait('), source.index('hold = leases.RunHold('))
        self.assertLess(source.index('turn.wait('), source.index('def game_build'), 'before any container starts')


if __name__ == '__main__':
    unittest.main()
