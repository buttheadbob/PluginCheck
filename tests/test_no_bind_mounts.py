"""No behaviour server holds a folder of this computer, every docker call has a time limit, and a stall watchdog stops a run whose Docker
has stalled. Run with: python -I -m unittest discover -s tests

When Docker Desktop's file sharing stalled, every server's launcher waited on its mounted folders in state D (request_wait_answer), could not
be killed, and servers piled up. Servers now get their files by docker cp before they start, and the watchdog ends the run if one is stuck.
"""
import ast, contextlib, io, itertools, json, os, re, shutil, subprocess, sys, tarfile, tempfile, threading, time, types, unittest
from unittest import mock
import scratchmachine  # noqa: F401  (before anything of PluginCheck's: keeps every test off the real lease folders)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SOURCE = os.path.join(ROOT, 'stages', 'behaviour.py')
TEXT = open(SOURCE, encoding='utf-8').read()
TREE = ast.parse(TEXT)
TIMED_OUT = 124
NO_WINDOW = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}


def lifted(*wanted):
    """behaviour.py's top-level functions and assignments of these names, in file order, as source. The stage runs when imported."""
    parts = []
    for node in TREE.body:
        names = {node.name} if isinstance(node, ast.FunctionDef) else \
            {n.id for t in node.targets for n in ast.walk(t) if isinstance(n, ast.Name)} if isinstance(node, ast.Assign) else set()
        if names & set(wanted):
            parts.append(ast.get_source_segment(TEXT, node))
    found = set()
    for part in parts:
        found |= {n.name for n in ast.parse(part).body if isinstance(n, ast.FunctionDef)}
        found |= {n.id for s in ast.parse(part).body if isinstance(s, ast.Assign) for t in s.targets for n in ast.walk(t) if isinstance(n, ast.Name)}
    missing = set(wanted) - found
    if missing:
        raise AssertionError(f'behaviour.py has no {", ".join(sorted(missing))}')
    return '\n\n'.join(parts)


def load(wanted, **namespace):
    space = {'os': os, 're': re, 'json': json, 'time': time, 'threading': threading, 'contextlib': contextlib, 'tarfile': tarfile, 'subprocess': subprocess,
             **namespace}
    exec(compile(lifted(*wanted), SOURCE, 'exec'), space)
    return space


def answer(code=0, out='', err=''):
    return types.SimpleNamespace(returncode=code, stdout=out, stderr=err)


class Recorder:
    """docker, recording each call with its options; reply(args, options) gives the answer."""

    def __init__(self, reply=lambda args, options: answer()):
        self.calls, self.reply = [], reply

    def __call__(self, *args, **options):
        self.calls.append((args, options))
        return self.reply(args, options)

    def verbs(self):
        return [a[0] for a, _ in self.calls]


sys.path.insert(0, os.path.join(ROOT, 'stages'))
import containers  # noqa: E402


@contextlib.contextmanager
def patched(docker, sleep=lambda seconds: None):
    """containers.docker replaced by docker, and its pauses by sleep."""
    with mock.patch.object(containers, 'docker', docker), mock.patch.object(containers, 'time', types.SimpleNamespace(time=time.time, sleep=sleep)):
        yield


class StartTests(unittest.TestCase):
    """start_magnetar, start_legacy and start_torch, with docker recorded."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix='pc-nobind-')
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.seed = os.path.join(self.folder, 'seed')
        os.makedirs(os.path.join(self.seed, 'Instance', 'Saves', 'Fixture'))
        with open(os.path.join(self.seed, 'Instance', 'probe-scenario.txt'), 'w') as f:
            f.write('mode=run\n')
        for sub in ('source', 'plugins'):
            os.makedirs(os.path.join(self.folder, sub, 'Demo'))
            with open(os.path.join(self.folder, sub, 'Demo', 'Demo.cs'), 'w') as f:
                f.write('class Demo {}\n')
        self.tars = []

    def docker(self, fail=None):
        def reply(args, options):
            if args[0] == 'cp' and options.get('stdin') is not None:
                self.tars.append(tarfile.open(fileobj=io.BytesIO(options['stdin'].read())))
            if fail and args[0] == fail:
                return answer(TIMED_OUT if fail == 'cp' else 1, err='docker cp gave no answer in 120s' if fail == 'cp' else 'boom')
            return answer()
        return Recorder(reply)

    def start(self, host, docker):
        space = load(('start_magnetar', 'start_legacy', 'start_torch', 'VOLUMES', 'MAGNETAR', 'LEGACY', 'TORCH', 'NO_CRASH_REPORTS', 'CLUSTER_NODE',
                      'MEMORY_PER_SERVER_GIB'), start_server=containers.start_server, githubtoken=types.SimpleNamespace(for_container=lambda: ([], None)))
        container, volume = 'plugincheck-behaviour-1-1', 'plugincheck-behaviour-1-1-data'
        with patched(docker):
            if host == 'magnetar':
                return space['start_magnetar']({}, self.seed, self.folder, container, volume, '0-1')
            if host == 'legacy':
                return space['start_legacy'](self.seed, self.folder, container, volume, '0-1')
            return space['start_torch']({}, self.seed, self.folder, container, volume, '0-1')

    def test_created_then_copied_in_then_started(self):
        for host in ('magnetar', 'legacy', 'torch'):
            with self.subTest(host=host):
                docker = self.docker()
                self.assertIsNone(self.start(host, docker))
                self.assertEqual(docker.verbs(), ['create', 'cp', 'start'])
                self.assertEqual(docker.calls[1][0], ('cp', '-', 'plugincheck-behaviour-1-1:/'))
                self.assertEqual(docker.calls[2][0], ('start', 'plugincheck-behaviour-1-1'))
                self.assertEqual([f for f in os.listdir(self.folder) if f.startswith('hand-in')], [], 'the tar is removed once copied')

    def test_the_hand_in_is_measured(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.start('magnetar', self.docker())
        self.assertRegex(out.getvalue(), r'^  hand-in plugincheck-behaviour-1-1: \d+\.\d MB, tar \d+\.\ds, docker cp \d+\.\ds$')

    def test_no_server_container_bind_mounts_a_folder_of_this_computer(self):
        for host in ('magnetar', 'legacy', 'torch'):
            with self.subTest(host=host):
                docker = self.docker()
                self.start(host, docker)
                create = docker.calls[0][0]
                mounts = [create[i + 1] for i, a in enumerate(create) if a in ('-v', '--volume', '--mount')]
                self.assertTrue(mounts)
                for mount in mounts:
                    self.assertRegex(mount.split(':')[0], r'^[A-Za-z0-9][A-Za-z0-9_.-]*$', f'{mount} is not a named volume')
                self.assertNotIn(self.folder, ' '.join(create))
                self.assertNotIn('marker', ' '.join(create))

    def test_the_files_arrive_where_the_entrypoints_read_them_owned_by_root_and_readable(self):
        expected = {'magnetar': ('seed/Instance/probe-scenario.txt', 'plugin/Demo/Demo.cs'), 'legacy': ('seed/Instance/probe-scenario.txt', 'plugin/Demo/Demo.cs'),
                    'torch': ('seed/data/Instance/probe-scenario.txt', 'seed/plugins/Demo/Demo.cs')}
        for host, paths in expected.items():
            with self.subTest(host=host):
                self.tars.clear()
                self.start(host, self.docker())
                members = {m.name: m for m in self.tars[0].getmembers()}
                for path in paths:
                    self.assertIn(path, members)
                # The folders above each path come first, so the copy needs nothing of the image.
                self.assertIn('seed', members)
                self.assertTrue(members['seed'].isdir())
                for member in members.values():
                    self.assertEqual((member.uid, member.gid), (0, 0), member.name)
                    self.assertEqual(member.mode, 0o755 if member.isdir() else 0o644, member.name)

    def test_a_copy_that_fails_starts_no_server_and_says_why(self):
        docker = self.docker(fail='cp')
        why = self.start('legacy', docker)
        self.assertEqual(docker.verbs(), ['create', 'cp', 'cp'])
        self.assertIn('its files could not be copied in', why)
        self.assertIn('gave no answer in 120s', why)

    def test_a_create_that_fails_says_why(self):
        docker = self.docker(fail='create')
        self.assertEqual(self.start('torch', docker), 'docker create failed: boom')
        self.assertEqual(docker.verbs(), ['create'])

    def test_the_marker_folder_is_gone_and_the_probe_skips_it_when_absent(self):
        self.assertNotIn('marker', TEXT)
        probe = open(os.path.join(ROOT, 'stages', 'behaviour', 'probe', 'src', 'Probe.cs'), encoding='utf-8').read()
        finish = probe[probe.index('private void Finish(string json)'):]
        self.assertIn('if (Directory.Exists(marker))', finish)



class SharedFolderTests(unittest.TestCase):
    """The single stages share one run folder and run side by side (check.py), so two hand-ins into it must never touch each other's tar."""

    def test_two_hand_ins_into_one_folder_at_once(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        source = os.path.join(folder, 'seed')
        os.makedirs(source)
        with open(os.path.join(source, 'a.txt'), 'w') as f:
            f.write('x')
        both_in, sent, tars, errors = threading.Barrier(2), {}, set(), []

        def slow_copy(src, dest, archive=None, **_):
            both_in.wait(5)  # both tars exist before either is sent or removed
            tars.add(archive)
            with tarfile.open(archive) as tar:
                sent[dest] = sorted(tar.getnames())
            both_in.wait(5)
            return None

        def run(name):
            try:
                self.assertIsNone(containers.hand_in(name, folder, {source: '/seed'}))
            except BaseException as e:  # noqa: BLE001
                errors.append(e)
        with mock.patch.object(containers, 'copy', slow_copy), contextlib.redirect_stdout(io.StringIO()):
            threads = [threading.Thread(target=run, args=(n,)) for n in ('plugincheck-magnetar-1', 'plugincheck-torch-1')]
            [t.start() for t in threads]
            [t.join(10) for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(len(tars), 2, 'each hand-in has a tar of its own: ' + repr(sorted(tars)))
        self.assertEqual(set(sent), {'plugincheck-magnetar-1:/', 'plugincheck-torch-1:/'})
        self.assertEqual([f for f in os.listdir(folder) if f.startswith('hand-in')], [], 'each tar is removed once copied')

class CopyTests(unittest.TestCase):
    def copy(self, replies):
        replies = iter(replies)
        docker = Recorder(lambda args, options: next(replies))
        slept = []
        with patched(docker, slept.append):
            return containers.copy('c:/data/Magnetar/.', 'here'), docker, slept

    def test_a_timeout_is_tried_again(self):
        why, docker, slept = self.copy([answer(TIMED_OUT, err='docker cp gave no answer in 120s'), answer()])
        self.assertIsNone(why)
        self.assertEqual(len(docker.calls), 2)
        self.assertTrue(all(options['timeout'] == 120 for _, options in docker.calls))
        self.assertEqual(slept, [2])

    def test_two_failures_give_a_clear_reason(self):
        why, docker, _ = self.copy([answer(TIMED_OUT, err='docker cp gave no answer in 120s')] * 2)
        self.assertEqual(why, 'docker cp c:/data/Magnetar/. here failed: docker cp gave no answer in 120s')
        self.assertEqual(len(docker.calls), 2)

    def test_a_path_the_server_never_wrote_is_not_tried_again(self):
        why, docker, slept = self.copy([answer(1, err='Error response from daemon: Could not find the file /data/x in container c')])
        self.assertIn('Could not find the file', why)
        self.assertEqual((len(docker.calls), slept), (1, []))

    def test_a_container_that_is_gone_is_not_tried_again(self):
        why, docker, slept = self.copy([answer(1, err='Error response from daemon: No such container: c')])
        self.assertEqual((len(docker.calls), slept), (1, []))


class HandInTests(unittest.TestCase):
    def test_no_tar_is_left_behind_when_it_cannot_be_built(self):
        folder = tempfile.mkdtemp(prefix='pc-handin-')
        self.addCleanup(shutil.rmtree, folder, True)
        with mock.patch.object(containers, 'copy', lambda *args, **options: None):
            with self.assertRaises(OSError):
                containers.hand_in('plugincheck-magnetar-1', folder, {os.path.join(folder, 'missing'): '/seed'})
        self.assertEqual([f for f in os.listdir(folder) if f.startswith('hand-in')], [])


def hanging_docker():
    """containers.docker with a real process that hangs standing in for the docker CLI, so this is subprocess's own timeout at work."""
    def run(command, **options):
        return subprocess.run([sys.executable, '-c', 'import time; time.sleep(60)'], **options, **NO_WINDOW)
    fake = types.SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired, CompletedProcess=subprocess.CompletedProcess)

    def docker(*args, **options):
        with mock.patch.object(containers, 'subprocess', fake):
            return containers.docker(*args, **options)
    return docker


class TimeLimitTests(unittest.TestCase):
    def test_a_hung_docker_call_returns_after_its_limit(self):
        started = time.time()
        said = hanging_docker()('exec', 'c', 'true', timeout=1)
        self.assertLess(time.time() - started, 20)
        self.assertEqual(said.returncode, TIMED_OUT)
        self.assertEqual(said.stderr, 'docker exec gave no answer in 1s')

    def test_every_docker_process_has_a_time_limit(self):
        shared = ast.parse(open(os.path.join(ROOT, 'stages', 'containers.py'), encoding='utf-8').read())
        runs = [n for n in ast.walk(shared) if isinstance(n, ast.Call) and ast.unparse(n.func) == 'subprocess.run']
        self.assertEqual(len(runs), 1, 'containers.docker() is the one place a docker process starts')
        self.assertIn('timeout', {k.arg for k in runs[0].keywords})
        started = [n for n in ast.walk(TREE) if isinstance(n, ast.Call) and ast.unparse(n.func).startswith('subprocess.')]
        self.assertEqual(started, [], 'behaviour.py starts no process of its own')
        self.assertIn('return containers.docker(*args, **given)', TEXT)

    def test_builds_get_the_long_limit(self):
        self.assertIn('extraplugins.torch_plugin(spec, cache, work, docker_long,', TEXT)
        self.assertEqual(TEXT.count('docker_long, SDK,'), 2)


class Watchdog:
    """watch_servers lifted, with the clock, Docker and stop_stalled replaced. It runs until it stops the run or has looked rounds times."""

    def __init__(self, servers, reply, rounds=4, clock=None, real_stop=False, docker=None):
        self.now = [1000.0]
        self.rounds, self.stopped, self.said = rounds, [], []

        class Done(Exception):
            pass
        self.Done = Done

        def sleep(seconds):
            if self.rounds == 0:
                raise Done()
            self.rounds -= 1
            self.now[0] += seconds

        self.docker = docker or Recorder(reply)
        self.space = load(('watched', 'WATCH_SECONDS', 'AGE_LIMIT', 'START_TIMEOUT', 'STALLED_WAIT', 'LOOK_SECONDS', 'WAITING', 'waiting_on_a_share',
                           'watch_servers'),
                          docker=self.docker, say_safely=self.said.append, TIMED_OUT=TIMED_OUT, time=types.SimpleNamespace(time=clock or (lambda: self.now[0]), sleep=sleep))
        if not real_stop:
            self.space['stop_stalled'] = self.stopped.append
        self.space['watched'].update(servers)

    def run(self):
        try:
            self.space['watch_servers']()
        except self.Done:
            pass
        return self


class WatchdogTests(unittest.TestCase):
    def test_a_server_older_than_its_time_limit_stops_the_run_without_asking_docker(self):
        dog = Watchdog({'plugincheck-behaviour-1-1': 1000.0 + 45 - 1600, 'plugincheck-behaviour-1-2': 1000.0}, lambda a, o: answer()).run()
        self.assertEqual(len(dog.stopped), 1)
        self.assertIn('plugincheck-behaviour-1-1 has run for 1600s, past the 1500s any server is given', dog.stopped[0])
        self.assertEqual(dog.docker.calls, [])

    def test_a_server_within_its_limit_is_left_alone(self):
        dog = Watchdog({'plugincheck-behaviour-1-1': 1000.0}, lambda a, o: answer(), rounds=3).run()
        self.assertEqual(dog.stopped, [])
        # One look per server per round, each with a time limit.
        self.assertEqual(len(dog.docker.calls), 3)
        self.assertTrue(all(o['timeout'] == 30 and a[0] == 'exec' for a, o in dog.docker.calls))

    def test_a_process_waiting_on_a_file_share_at_two_looks_stops_the_run(self):
        stuck = answer(out='212 request_wait_answer wine64-preload\n9 do_epoll_wait server\n')
        dog = Watchdog({'plugincheck-behaviour-1-3': 1000.0}, lambda a, o: stuck).run()
        self.assertEqual(len(dog.docker.calls), 2, 'it trips at the second look')
        self.assertEqual(len(dog.stopped), 1)
        self.assertIn('process 212 (request_wait_answer wine64-preload) of plugincheck-behaviour-1-3 waits on a file share', dog.stopped[0])

    def test_a_passing_wait_or_another_d_state_does_not(self):
        looks = iter([answer(out='212 request_wait_answer cp\n'), answer(out=''), answer(out='212 request_wait_answer cp\n'),
                      answer(out='300 io_schedule wine\n'), answer(out='300 io_schedule wine\n')])
        dog = Watchdog({'plugincheck-behaviour-1-3': 1000.0}, lambda a, o: next(looks), rounds=5).run()
        self.assertEqual(dog.stopped, [])

    def test_the_look_reads_state_and_wchan_from_proc(self):
        script = load(('WAITING',))['WAITING']
        self.assertIn('/proc/[0-9]*', script)
        self.assertIn('$p/stat', script)
        self.assertIn('$p/wchan', script)
        self.assertIn('[ "$s" = D ]', script)
        # Run for real where a shell and /proc exist; the shape of each line is what waiting_on_a_share reads.
        if os.path.isdir('/proc/self') and shutil.which('sh'):
            done = subprocess.run(['sh', '-c', script], capture_output=True, text=True, timeout=30)
            self.assertEqual(done.returncode, 0)

    def test_a_hung_docker_call_never_holds_up_the_watchdog(self):
        """Each look is a real hung process ended by its time limit; the age check still trips on time."""
        real = hanging_docker()
        dog = Watchdog({'plugincheck-behaviour-1-4': 1000.0 - 1400}, None, rounds=10, docker=lambda *a, **o: real(*a, **{**o, 'timeout': 1}))
        started = time.time()
        dog.run()
        self.assertLess(time.time() - started, 60)
        # 1400 s old at the start, so the third round passes 1500 s: the age trip, with each look before it hung.
        self.assertEqual(len(dog.stopped), 1)
        self.assertIn('past the 1500s', dog.stopped[0])
        self.assertTrue(any('gave no answer in 30s' in s for s in dog.said))

    def test_a_server_that_is_gone_leaves_the_watch(self):
        dog = Watchdog({'plugincheck-behaviour-1-5': 1000.0}, lambda a, o: answer(1, err='Error response from daemon: No such container: plugincheck-behaviour-1-5'),
                       rounds=2).run()
        self.assertEqual(dog.space['watched'], {})
        self.assertEqual(len(dog.docker.calls), 1)

    def test_started_with_the_lending_thread(self):
        self.assertIn('threading.Thread(target=watch_servers, daemon=True).start()', TEXT)
        self.assertLess(TEXT.index('threading.Thread(target=watch_servers, daemon=True).start()'), TEXT.index('with concurrent.futures.ThreadPoolExecutor('))


class FakeDocker:
    """Docker's listing and removal of containers and volumes, as test_clean_end has it."""

    def __init__(self, containers, volumes):
        self.containers, self.volumes, self.calls = containers, volumes, []

    def __call__(self, *args, **options):
        self.calls.append(args)
        out = answer()
        if args[0] == 'ps':
            out.stdout = '\n'.join(self.containers) + '\n'
        elif args[:2] == ('volume', 'ls'):
            out.stdout = '\n'.join(self.volumes) + '\n'
        elif args[:2] == ('rm', '-f'):
            self.containers = [c for c in self.containers if c not in args[3:]]
        elif args[:2] == ('volume', 'rm'):
            self.volumes = [v for v in self.volumes if v not in args[2:]]
        return out


class StalledEndTests(unittest.TestCase):
    def test_the_watchdog_stop_ends_as_the_clean_end_does_and_says_why(self):
        me = os.getpid()
        docker = FakeDocker([f'plugincheck-behaviour-{me}-1', f'plugincheck-behaviour-{me}-2', 'plugincheck-behaviour-4-1'], [f'plugincheck-behaviour-{me}-1-data'])
        folder = tempfile.mkdtemp(prefix='pc-nobind-')
        self.addCleanup(shutil.rmtree, folder, True)
        result = os.path.join(folder, 'behaviour.json')

        class Ended(Exception):
            pass

        def leave(code):
            raise Ended(code)
        said, stopped = [], []
        space = load(('SERVER_NAME', 'END_SECONDS', 'CLEAN_SECONDS', 'ending', 'remove_servers', 'remove_own_servers', 'interrupt_lock', 'stop_stalled'),
                     docker=docker, os=types.SimpleNamespace(getpid=os.getpid, _exit=leave), say_safely=said.append, result_path=result,
                     lending=types.SimpleNamespace(stop=lambda: stopped.append(True)))
        with self.assertRaises(Ended) as ended:
            space['stop_stalled']('plugincheck-behaviour-1-1 has run for 1600s')
        self.assertEqual(ended.exception.args, (1,))
        self.assertTrue(space['ending'].is_set(), 'no server starts after it')
        self.assertEqual([c for c in docker.calls if c[0] == 'rm' or c[:2] == ('volume', 'rm')],
                         [('rm', '-f', '-v', f'plugincheck-behaviour-{me}-1', f'plugincheck-behaviour-{me}-2'), ('volume', 'rm', f'plugincheck-behaviour-{me}-1-data')])
        self.assertEqual(docker.containers, ['plugincheck-behaviour-4-1'], "another run's server is left alone")
        self.assertEqual(stopped, [True])
        with open(result, encoding='utf-8') as f:
            outcome = json.load(f)
        self.assertEqual(outcome, {'status': 'not run', 'reason': 'stopped because Docker stalled: plugincheck-behaviour-1-1 has run for 1600s', 'stalled': True})
        self.assertTrue(said[0].startswith('STALLED  plugincheck-behaviour-1-1 has run for 1600s'))


class ServeTests(unittest.TestCase):
    """serve() lifted, as test_free_slot does, for what this change adds to it."""

    def serve(self, start_problem=None, exec_answer=None, rm_answer=None, start_timeout=3, kill_raises=None):
        sys.path.insert(0, os.path.join(ROOT, 'stages'))
        import startlog
        tree = ast.parse(TEXT)
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in {'serve', 'write', 'read_text', 'container_log'}]
        work = tempfile.mkdtemp(prefix='pc-nobind-')
        self.addCleanup(shutil.rmtree, work, True)
        calls = []

        def docker(*args, **options):
            calls.append(args)
            if args[0] == 'exec':
                return exec_answer() if exec_answer else answer(0, 'waiting')
            if args[0] == 'rm' and rm_answer:
                return rm_answer
            if args[0] == 'inspect':
                return answer(0, 'true')
            if args[0] == 'kill' and kill_raises:
                raise kill_raises
            return answer()
        watched = {}
        self.calls, self.watched = calls, watched
        space = {'os': os, 'shutil': shutil, 'json': json, 're': re, 'glob': __import__('glob'), 'time': time, 'docker': docker, 'contextlib': contextlib,
                 'counter': itertools.count(1), 'work': work, 'scenario_file': lambda build, batch: 'mode=run\n', 'START_TIMEOUT': start_timeout,
                 'stall_limit': lambda batch: 600, 'options': {}, 'CHECK_SECONDS': 1, 'startlog': startlog, 'remember_magnetar_cache': lambda folder: None,
                 'seed_magnetar': lambda *a: None, 'seed_torch': lambda *a: None, 'start_magnetar': lambda *a: start_problem,
                 'start_legacy': lambda *a: start_problem, 'start_torch': lambda *a: start_problem, 'watched': watched, 'watched_lock': threading.Lock(),
                 'TIMED_OUT': TIMED_OUT, 'LOOK_SECONDS': 30, 'CLEAN_SECONDS': 30}
        space['copy'] = lambda *args, archive=None: None if docker('cp', *args).returncode == 0 else f"docker cp {' '.join(args)} failed"
        exec(compile(ast.Module(body=nodes, type_ignores=[]), SOURCE, 'exec'), space)
        os.makedirs(os.path.join(work, 'save'))
        seen = []
        space['time'] = types.SimpleNamespace(time=time.time, sleep=lambda s: (seen.append(dict(watched)), time.sleep(min(s, 0.05))))
        records = space['serve']({'name': 'working-legacy', 'host': 'legacy', 'plugin': 'Demo'}, [{'name': 's', 'command': '!demo'}],
                                 os.path.join(work, 'save'), None, '0-1', {}, lambda: None)
        return records, calls, watched, seen

    def test_watched_while_it_runs_and_not_after(self):
        records, calls, watched, seen = self.serve(exec_answer=lambda: answer(0, 'recorded'))
        self.assertEqual(watched, {})
        records, calls, watched, seen = self.serve(start_timeout=1.5)
        self.assertTrue(any('plugincheck-behaviour-' in ''.join(s) for s in seen), 'it was watched while it ran')
        self.assertEqual(watched, {})

    def test_a_server_whose_serve_raised_is_still_removed_and_leaves_the_watch(self):
        """Anything that raises once the server is up still removes it and takes it off the watch, where it would read as a stalled server
        once past AGE_LIMIT and stop the run."""
        with self.assertRaises(OSError):
            self.serve(exec_answer=lambda: answer(0, 'recorded'), kill_raises=OSError('docker went away'))
        self.assertIn('rm', [c[0] for c in self.calls])
        self.assertEqual(self.watched, {})

    def test_a_server_whose_removal_got_no_answer_stays_watched(self):
        _, _, watched, _ = self.serve(exec_answer=lambda: answer(0, 'recorded'), rm_answer=answer(TIMED_OUT, err='docker rm gave no answer in 30s'))
        self.assertEqual(len(watched), 1)

    def test_a_look_that_got_no_answer_is_not_a_server_that_ended(self):
        records, calls, _, _ = self.serve(exec_answer=lambda: answer(TIMED_OUT, err='docker exec gave no answer in 30s'), start_timeout=2.5)
        self.assertEqual(records[0]['state'], 'timeout')
        verbs = [c[0] for c in calls]
        self.assertNotIn('inspect', verbs[:verbs.index('kill')])

    def test_a_server_that_did_not_start_says_why_and_is_still_removed(self):
        records, calls, watched, _ = self.serve(start_problem='its files could not be copied in: docker cp - plugincheck-behaviour-1-1:/ failed: gave no answer')
        self.assertEqual(records[0]['state'], 'not started')
        self.assertEqual(records[0]['failed'], 'the server did not start: its files could not be copied in: docker cp - plugincheck-behaviour-1-1:/ failed: gave no answer')
        self.assertNotIn('exec', [c[0] for c in calls])
        self.assertIn('rm', [c[0] for c in calls])
        self.assertEqual(watched, {})


if __name__ == '__main__':
    unittest.main()
