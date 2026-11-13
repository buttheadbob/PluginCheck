"""A run that is interrupted, or whose stage ends with no clean-up, leaves no servers behind: python -I -m unittest discover -s tests

check.py stops its running stage on Ctrl-C or Ctrl-Break, sent to its group or typed at the console, and still writes its results; behaviour
removes its own servers on every way out, also when its output can no longer be written, and at its start those of a behaviour stage whose
process is gone; an owner that stopped beating reads as not running.
"""
import ast, contextlib, json, os, queue, shutil, subprocess, sys, tempfile, threading, time, types, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(ROOT, 'stages'))
import leases

SOURCE = os.path.join(ROOT, 'stages', 'behaviour.py')
NO_WINDOW = 0x08000000
# Run in a console of its own with no window, so the Ctrl-Break it sends reaches only the processes it starts. 'group' sends it to the
# started process's group alone. 'typed' sends it to every process on the console, as a Ctrl-Break typed at the console does, and this
# harness ignores it.
HARNESS = r'''
import ctypes, os, signal, subprocess, sys, time
output, ready, how, command = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
if how == 'typed':
    signal.signal(signal.SIGBREAK, signal.SIG_IGN)
with open(output, 'w', encoding='utf-8') as out:
    started = subprocess.Popen(command, stdout=out, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if how == 'group' else 0)
    deadline = time.time() + 60
    while not os.path.exists(ready) and time.time() < deadline and started.poll() is None:
        time.sleep(0.1)
    if how == 'typed':
        ctypes.windll.kernel32.GenerateConsoleCtrlEvent(signal.CTRL_BREAK_EVENT, 0)
    else:
        os.kill(started.pid, signal.CTRL_BREAK_EVENT)
    try:
        print(started.wait(60))
    except subprocess.TimeoutExpired:
        started.kill()
        print('timeout')
'''


def text(path):
    with open(path, encoding='utf-8', errors='replace') as file:
        return file.read()


def interrupt_in_hidden_console(output, ready, command, env=None, how='group'):
    done = subprocess.run([sys.executable, '-I', '-c', HARNESS, output, ready, how, *command], capture_output=True, text=True, creationflags=NO_WINDOW,
                          env=env, timeout=180)
    return done.stdout.strip()


def lifted(wanted, main_block=False):
    """behaviour.py's top-level statements that define the wanted names, and its `if __name__ == '__main__'` block, as source in file order."""
    source = text(SOURCE)
    parts = []
    for node in ast.parse(source).body:
        names = {node.name} if isinstance(node, ast.FunctionDef) else {t.id for t in node.targets if isinstance(t, ast.Name)} if isinstance(node, ast.Assign) else set()
        main = isinstance(node, ast.If) and ast.unparse(node.test) == "__name__ == '__main__'"
        if names & set(wanted) or (main_block and main):
            parts.append(ast.get_source_segment(source, node))
    return parts


class FakeDocker:
    def __init__(self, containers, volumes):
        self.containers, self.volumes, self.calls = containers, volumes, []

    def __call__(self, *args):
        self.calls.append(args)
        out = types.SimpleNamespace(returncode=0, stdout='', stderr='')
        if args[0] == 'ps':
            out.stdout = '\n'.join(self.containers) + '\n'
        elif args[:2] == ('volume', 'ls'):
            out.stdout = '\n'.join(self.volumes) + '\n'
        elif args[:2] == ('rm', '-f'):
            self.containers = [c for c in self.containers if c not in args[3:]]
        elif args[:2] == ('volume', 'rm'):
            self.volumes = [v for v in self.volumes if v not in args[2:]]
        return out

    def removed(self):
        return [a for a in self.calls if a[:2] in (('rm', '-f'), ('volume', 'rm'))]


def removal_functions(docker, alive=lambda pid: True):
    wanted = ('SERVER_NAME', 'END_SECONDS', 'ending', 'remove_servers', 'remove_own_servers', 'remove_abandoned_servers')
    parts = lifted(wanted)
    namespace = {'os': os, 're': __import__('re'), 'threading': threading, 'time': time, 'docker': docker,
                 'leases': types.SimpleNamespace(process_alive=alive)}
    exec(compile('\n\n'.join(parts), SOURCE, 'exec'), namespace)
    missing = [n for n in wanted if n not in namespace]
    if missing:
        raise AssertionError(f'behaviour.py has no {", ".join(missing)}')
    return namespace


def server(pid, n, volume=False):
    return f'plugincheck-behaviour-{pid}-{n}' + ('-data' if volume else '')


class OwnServersTests(unittest.TestCase):
    def test_the_clean_up_removes_exactly_this_stages_servers_and_volumes(self):
        me, other = os.getpid(), os.getpid() + 1
        docker = FakeDocker([server(me, 1), server(me, 12), server(other, 1), f'plugincheck-behaviour-libs-{me}', 'mods-1', f'x{server(me, 3)}'],
                            [server(me, 1, True), server(me, 12, True), server(other, 1, True), 'plugincheck-ds-magnetar', 'mods-1-data'])
        removal_functions(docker)['remove_own_servers']()
        self.assertEqual(docker.removed(), [('rm', '-f', '-v', server(me, 1), server(me, 12)), ('volume', 'rm', server(me, 1, True), server(me, 12, True))])

    def test_nothing_to_remove_runs_no_removal(self):
        docker = FakeDocker([], [])
        removal_functions(docker)['remove_own_servers']()
        self.assertEqual(docker.removed(), [])

    def test_the_handler_removes_the_servers_when_its_output_cannot_be_written(self):
        """On a console close check.py, which reads the stage's output, may have ended first: every write then fails."""
        me = os.getpid()
        docker = FakeDocker([server(me, 1), server(me, 2)], [server(me, 1, True)])
        wanted = ('SERVER_NAME', 'END_SECONDS', 'ending', 'remove_servers', 'remove_own_servers', 'say_safely', 'interrupt_lock', 'interrupted')

        class Ended(Exception):
            pass

        def leave(code):
            raise Ended(code)

        class Closed:
            def write(self, text):
                raise OSError(22, 'Invalid argument')

            def flush(self):
                raise OSError(22, 'Invalid argument')

        namespace = {'os': types.SimpleNamespace(getpid=os.getpid, _exit=leave), 're': __import__('re'), 'threading': threading, 'time': time,
                     'contextlib': contextlib, 'docker': docker, 'lending': types.SimpleNamespace(stop=lambda: print('lending stopped', flush=True))}
        exec(compile('\n\n'.join(lifted(wanted)), SOURCE, 'exec'), namespace)
        with mock.patch('sys.stdout', Closed()), self.assertRaises(Ended) as ended:
            namespace['interrupted']()
        self.assertEqual(ended.exception.args, (1,))
        self.assertEqual(docker.removed(), [('rm', '-f', '-v', server(me, 1), server(me, 2)), ('volume', 'rm', server(me, 1, True))])

    def test_a_listing_docker_refused_does_not_count_as_empty(self):
        me = os.getpid()

        class Refusing(FakeDocker):
            """The first two listings fail, as with Docker busy or restarting."""
            refused = 0

            def __call__(self, *args):
                out = super().__call__(*args)
                if args[0] == 'ps' and self.refused < 2:
                    self.refused += 1
                    out.returncode, out.stdout = 1, ''
                return out

        docker = Refusing([server(me, 1)], [])
        removal_functions(docker)['remove_own_servers']()
        self.assertEqual([a for a in docker.removed() if a[0] == 'rm'], [('rm', '-f', '-v', server(me, 1))])

    def test_registered_for_every_way_out(self):
        source = text(SOURCE)
        self.assertIn('atexit.register(remove_own_servers)', source)
        self.assertLess(source.index('atexit.register(remove_own_servers)'), source.index('with concurrent.futures.ThreadPoolExecutor('),
                        'registered before the first server starts')

    @unittest.skipUnless(os.name == 'nt', 'console events are a Windows thing')
    def test_ctrl_break_removes_the_servers_while_the_main_thread_waits_on_the_pool(self):
        """The stage's main thread waits in as_completed, where no Python signal handler runs on Windows. The console handler still runs."""
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        calls, ready = os.path.join(folder, 'calls.txt'), os.path.join(folder, 'ready')
        prelude = r'''
import atexit, concurrent.futures, contextlib, json, os, re, signal, sys, threading, time, types
calls, ready = sys.argv[1], sys.argv[2]


gone = set()


def docker(*args):
    with open(calls, 'a', encoding='utf-8') as f:
        f.write(json.dumps(args) + '\n')
    if args[:2] == ('rm', '-f'):
        gone.update(args[3:])
    elif args[:2] == ('volume', 'rm'):
        gone.update(args[2:])
    me = os.getpid()
    names = {'ps': f'plugincheck-behaviour-{me}-1 plugincheck-behaviour-{me}-2 plugincheck-behaviour-4-1 mods-1',
             'volume': f'plugincheck-behaviour-{me}-1-data plugincheck-behaviour-4-1-data'}
    listed = ' '.join(n for n in names.get(args[0], '').split() if n not in gone)
    return types.SimpleNamespace(returncode=0, stderr='', stdout=listed if args[0] == 'ps' or args[:2] == ('volume', 'ls') else '')


lending = types.SimpleNamespace(stop=lambda: docker('lending stopped'))
'''
        block = r'''
with open(ready, 'w') as f:
    f.write(str(os.getpid()))
with concurrent.futures.ThreadPoolExecutor(1) as pool:
    for future in concurrent.futures.as_completed([pool.submit(time.sleep, 120)]):
        pass
'''
        parts = lifted(('SERVER_NAME', 'END_SECONDS', 'ending', 'remove_servers', 'remove_own_servers', 'say_safely', 'interrupt_lock', 'interrupted'),
                       main_block=True)
        self.assertEqual(len(parts), 9, 'behaviour.py has the server name, the time limit, the event, the two removals, the safe print, the lock, '
                                        'the handler and its registration')
        script = os.path.join(folder, 'stage.py')
        with open(script, 'w', encoding='utf-8') as f:
            f.write(prelude + '\n\n'.join(parts) + '\n' + block)
        started = time.time()
        code = interrupt_in_hidden_console(os.path.join(folder, 'out.txt'), ready, [sys.executable, '-I', script, calls, ready])
        self.assertEqual(code, '1')
        self.assertLess(time.time() - started, 60)
        pid = int(text(ready))
        said = [tuple(json.loads(line)) for line in text(calls).splitlines()]
        self.assertEqual([c for c in said if c[0] in ('lending stopped', 'rm') or c[:2] == ('volume', 'rm')],
                         [('rm', '-f', '-v', server(pid, 1), server(pid, 2)), ('volume', 'rm', server(pid, 1, True)), ('lending stopped',)])


class EndingTests(unittest.TestCase):
    def test_a_server_waiting_for_a_slot_when_the_run_ends_starts_no_container(self):
        started, slots = [], queue.Queue()

        def serve(build, batch, save, probe_zip, cores, timing, free):
            started.append(('docker', 'run', cores))
            return [{'build': build['name'], 'state': 'recorded', 'scenario': batch[0]['name']}]

        namespace = {'time': time, 'threading': threading, 'slots': slots, 'serve': serve, 'running': [0], 'running_lock': threading.Lock(),
                     'status': lambda **fields: None, 'ending': threading.Event(),
                     'lending': types.SimpleNamespace(may_start=lambda: True, free=lambda cores: None, note=lambda *a: None),
                     'server_timing': types.SimpleNamespace(summary=lambda timing, probe: (None, None))}
        exec(compile('\n\n'.join(lifted(('ending', 'run_server'))), SOURCE, 'exec'), namespace)
        returned = []
        waiter = threading.Thread(target=lambda: returned.append(namespace['run_server']({'name': 'b'}, [{'name': 's'}], 'save', 'zip')), daemon=True)
        waiter.start()
        time.sleep(0.3)
        self.assertTrue(waiter.is_alive(), 'it waits for a slot')
        namespace['ending'].set()
        slots.put('0-3')
        waiter.join(5)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(started, [], 'no container is started once the run is ending')
        self.assertEqual(list(slots.queue), ['0-3'], 'the slot goes back, so the next waiter wakes too')
        self.assertNotEqual(returned[0][0]['state'], 'recorded')
        self.assertFalse(returned[0][0].get('blocked'), 'not tried again')

    def test_the_clean_up_repeats_until_no_server_is_listed(self):
        me = os.getpid()

        class Starting(FakeDocker):
            """A server whose docker run was under way: it is listed only after the first listing."""
            def __call__(self, *args):
                out = super().__call__(*args)
                if args[0] == 'ps' and not hasattr(self, 'appeared'):
                    self.appeared = True
                    self.containers.append(server(me, 2))
                return out

        docker = Starting([server(me, 1)], [])
        namespace = removal_functions(docker)
        namespace['remove_own_servers']()
        self.assertTrue(namespace['ending'].is_set())
        self.assertEqual([a for a in docker.removed() if a[0] == 'rm'], [('rm', '-f', '-v', server(me, 1)), ('rm', '-f', '-v', server(me, 2))])
        self.assertEqual(docker.containers, [])
        self.assertEqual(sum(a[0] == 'ps' for a in docker.calls), 4, 'two listings in a row found none')


class SweepTests(unittest.TestCase):
    def test_only_servers_of_a_stage_that_is_gone_are_removed(self):
        me, dead, live = os.getpid(), 111, 222
        docker = FakeDocker([server(dead, 1), server(dead, 2), server(live, 1), server(me, 1), 'mods-1', 'plugincheck-magnetar-1700000000',
                             'plugincheck-quasar-1-1700000000', f'plugincheck-behaviour-libs-{dead}', 'plugincheck-pre-1'],
                            [server(dead, 1, True), server(dead, 2, True), server(live, 1, True), server(me, 1, True), 'mods-1-data',
                             'plugincheck-ds-torch', 'plugincheck-quasarrun-1700000000'])
        asked = []

        def alive(pid):
            asked.append(pid)
            return pid != dead

        removed = removal_functions(docker, alive)['remove_abandoned_servers']()
        self.assertEqual(removed, [server(dead, 1), server(dead, 2)])
        self.assertEqual(docker.removed(), [('rm', '-f', '-v', server(dead, 1), server(dead, 2)), ('volume', 'rm', server(dead, 1, True), server(dead, 2, True))])
        self.assertNotIn(me, asked, 'its own servers are never a gone process')

    def test_nothing_left_runs_no_removal(self):
        docker = FakeDocker([server(222, 1)], [server(222, 1, True)])
        self.assertEqual(removal_functions(docker, lambda pid: True)['remove_abandoned_servers'](), [])
        self.assertEqual(docker.removed(), [])

    def test_swept_before_any_server_of_this_run_starts(self):
        source = text(SOURCE)
        self.assertLess(source.index('swept = remove_abandoned_servers()'), source.index('workers, slots = capacity()'))


class StaleOwnerTests(unittest.TestCase):
    def test_an_owner_that_stopped_beating_reads_as_not_running(self):
        state = leases.empty()
        state['owner'] = {'name': 'plugincheck', 'pid': 38596, 'running': True, 'servers': 16, 'heartbeat': 1000}
        leases.expire(state, 1000 + leases.OWNER_LIMIT + 1, alive=lambda pid: True)
        self.assertFalse(state['owner']['running'])

    def test_a_beating_owner_stays_running(self):
        state = leases.empty()
        state['owner'] = {'name': 'plugincheck', 'pid': 38596, 'running': True, 'servers': 16, 'heartbeat': 1000}
        leases.expire(state, 1000 + leases.OWNER_LIMIT, alive=lambda pid: True)
        self.assertTrue(state['owner']['running'])

    def test_saved_as_not_running_by_the_next_call(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        with open(os.path.join(folder, 'leases.json'), 'w', encoding='utf-8') as f:
            json.dump(dict(leases.empty(), owner={'name': 'plugincheck', 'pid': 38596, 'running': True, 'servers': 16, 'heartbeat': 1000}), f)
        with leases.Store(folder) as state:
            leases.expire(state, 1000 + leases.OWNER_LIMIT + 1)
        with open(os.path.join(folder, 'leases.json'), encoding='utf-8') as f:
            self.assertFalse(json.load(f)['owner']['running'])


FAKE_STAGE = r'''
import os, signal, sys, time
folder = os.environ['CLEAN_END_FOLDER']


def stop(*_):
    with open(os.path.join(folder, 'stage-stopped'), 'w') as f:
        f.write('cleaned up')
    sys.exit(0)


signal.signal(signal.SIGBREAK, stop)
signal.signal(signal.SIGINT, stop)
with open(os.path.join(folder, 'stage-pid'), 'w') as f:
    f.write(str(os.getpid()))
print('working', flush=True)
# In short steps: a Python handler for SIGBREAK runs only once a sleep ends.
for _ in range(1200):
    time.sleep(0.1)
'''
# check.py runs without -I here, so this replaces Docker for it: every docker command fails, as with no Docker at all. Its stages run with -I.
NO_DOCKER = r'''
import subprocess
_run = subprocess.run


def run(args, *rest, **options):
    if args and args[0] == 'docker':
        return subprocess.CompletedProcess(args, 1, '', '')
    return _run(args, *rest, **options)


subprocess.run = run
'''


@unittest.skipUnless(os.name == 'nt', 'console events are a Windows thing')
class CheckInterruptTests(unittest.TestCase):
    def test_ctrl_break_stops_the_stage_and_the_results_are_written(self):
        said = self.interrupted_run('group')
        self.assertIn('interrupted: stopping 1 running stages', said)

    def test_a_typed_ctrl_break_reaches_the_stage_too_and_it_is_still_recorded_as_interrupted(self):
        """A process group does not keep a typed Ctrl-Break from a stage: it gets the event at once, as check.py does, and may end on it before
        check.py's main thread has seen it."""
        self.interrupted_run('typed')

    def interrupted_run(self, how):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        root = os.path.join(folder, 'tool')
        os.makedirs(os.path.join(root, 'targets'))
        shutil.copy(os.path.join(ROOT, 'check.py'), root)
        shutil.copytree(os.path.join(ROOT, 'stages'), os.path.join(root, 'stages'), ignore=shutil.ignore_patterns('__pycache__', 'bin', 'obj'))
        with open(os.path.join(root, 'stages', 'manifest.py'), 'w', encoding='utf-8') as f:
            f.write(FAKE_STAGE)
        # This copy lives in a temporary folder, deeper than Docker Desktop's mounts allow, and starts no container.
        with open(os.path.join(root, 'stages', 'mountdepth.py'), 'w', encoding='utf-8') as f:
            f.write('def problem(run_dir):\n    return None\n')
        repo = os.path.join(folder, 'plugin')
        os.makedirs(repo)
        with open(os.path.join(root, 'targets', 'Fake.json'), 'w', encoding='utf-8') as f:
            json.dump({'name': 'Fake', 'repo': repo}, f)
        site = os.path.join(folder, 'site')
        os.makedirs(site)
        with open(os.path.join(site, 'sitecustomize.py'), 'w', encoding='utf-8') as f:
            f.write(NO_DOCKER)
        env = {**os.environ, 'PYTHONPATH': site, 'PLUGINCHECK_MACHINE_DIR': os.path.join(folder, 'machine'), 'PLUGINCHECK_PLAIN': '1',
               'CLEAN_END_FOLDER': folder}
        output = os.path.join(folder, 'check.txt')
        code = interrupt_in_hidden_console(output, os.path.join(folder, 'stage-pid'),
                                           [sys.executable, os.path.join(root, 'check.py'), 'Fake', '--only', 'manifest'], env, how)
        said = text(output)
        self.assertTrue(os.path.exists(os.path.join(folder, 'stage-pid')), said)
        self.assertEqual(code, '1', said)
        self.assertFalse(leases.process_alive(int(text(os.path.join(folder, 'stage-pid')))))
        self.assertTrue(os.path.exists(os.path.join(folder, 'stage-stopped')), 'the stage was sent the interrupt and cleaned up')
        results = [os.path.join(dirpath, 'results.json') for dirpath, _, files in os.walk(os.path.join(root, 'runs')) if 'results.json' in files]
        self.assertEqual(len(results), 1, said)
        with open(results[0], encoding='utf-8') as f:
            manifest = json.load(f)['stages'].get('manifest')
        self.assertEqual(manifest, {'status': 'not run', 'reason': 'the run was interrupted while this stage ran'}, said)
        self.assertIn('interrupted: stopping', said)
        return said


if __name__ == '__main__':
    unittest.main()
