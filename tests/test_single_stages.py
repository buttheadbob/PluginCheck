"""The single-server stages (magnetar, legacy, torch, quasar, quasar_managed) hand their servers files by docker cp, bind-mount no folder of
this computer, give every docker call a time limit, and say why when a server cannot start. Run with: python -I -m unittest discover -s tests

Each stage runs as it is, in a process of its own, with Docker replaced by a stand-in (FAKE, as sitecustomize) that records every docker
call and answers it, and with time.sleep made instant.
"""
import ast, json, os, re, shutil, subprocess, sys, tarfile, tempfile, unittest, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
STAGES = ('magnetar', 'legacy', 'torch', 'quasar', 'quasar_managed')
NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

FAKE = r'''
import io, json, os, subprocess, tarfile, time
_run, _popen = subprocess.run, subprocess.Popen
calls = os.environ['FAKE_DOCKER_CALLS']
fail = os.environ.get('FAKE_DOCKER_FAIL', '')
interrupt = os.environ.get('FAKE_DOCKER_INTERRUPT', '')
time.sleep = lambda seconds: None


def record(args, options, tar=None):
    with open(calls, 'a', encoding='utf-8') as f:
        f.write(json.dumps({'args': list(args), 'timeout': options.get('timeout'), 'tar': tar}) + '\n')


def answer(args):
    verb = args[0]
    if verb == fail:
        return 124 if verb == 'cp' else 1, '', f'docker {verb} gave no answer in 120s' if verb == 'cp' else 'boom'
    if verb == 'exec':
        if 'curl' in args:
            return 0, '200', ''
        return 0, 'Game ready Starting server watchdog', ''
    if verb == 'inspect':
        return 0, 'true', ''
    if verb == 'image' and '--format' in args:
        return 0, '1.4.1.1', ''
    if verb == 'run' and 'cat' in args:
        return 0, json.dumps({'version': '1.4.1-1'}), ''
    if verb == 'logs':
        return 0, 'PLUGINCHECK_BUILD_OK', ''
    return 0, '', ''


def run(args, *rest, **options):
    if not (isinstance(args, (list, tuple)) and args and args[0] == 'docker'):
        return _run(args, *rest, **options)
    tar = None
    stdin = options.get('stdin')
    if args[1] == 'cp' and stdin is not None:
        members = tarfile.open(fileobj=io.BytesIO(stdin.read())).getmembers()
        tar = [[m.name, m.uid, m.gid, m.mode, m.isdir()] for m in members]
    record(args[1:], options, tar)
    if args[1] == interrupt:
        # As check.py's Ctrl-Break (Ctrl-C elsewhere) reaches the stage: what the stage does with the signal is what is tested.
        import signal
        signal.raise_signal(getattr(signal, 'SIGBREAK', signal.SIGINT))
    code, out, err = answer(args[1:])
    return subprocess.CompletedProcess(args, code, out, err)


class Follower:
    def __init__(self, args, **options):
        record(args[1:], {'timeout': 'follower'})

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def popen(args, *rest, **options):
    if isinstance(args, (list, tuple)) and args and args[0] == 'docker':
        return Follower(args, **options)
    return _popen(args, *rest, **options)


subprocess.run, subprocess.Popen = run, popen
'''


def named_volume(mount):
    """Whether a -v value's source is a Docker volume, not a folder: everything before the first ':/', so a drive letter counts as a folder."""
    return re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]+', mount.split(':/')[0]) is not None


class Stage:
    """One stage run against a small made-up plugin."""

    def __init__(self, test, stage, fail='', interrupt=''):
        self.folder = tempfile.mkdtemp(prefix='pc-single-')
        test.addCleanup(shutil.rmtree, self.folder, True)
        repo = os.path.join(self.folder, 'plugin')
        os.makedirs(os.path.join(repo, 'src'))
        with open(os.path.join(repo, 'src', 'Demo.cs'), 'w') as f:
            f.write('class Demo {}\n')
        with open(os.path.join(repo, 'Demo.xml'), 'w') as f:
            f.write('<PluginData><Id>demo</Id><FriendlyName>Demo</FriendlyName><SourceDirectories><Directory>src</Directory></SourceDirectories></PluginData>')
        with open(os.path.join(repo, 'quasar-plugin.json'), 'w') as f:
            json.dump({'id': 'demo', 'projectPath': 'src/Demo.csproj', 'stylesheets': ['demo.css']}, f)
        with zipfile.ZipFile(os.path.join(repo, 'Demo.zip'), 'w') as z:
            z.writestr('manifest.xml', '<PluginManifest><Name>Demo</Name></PluginManifest>')
        target = {'name': 'Demo', 'repo': repo, 'magnetar': {'manifest': 'Demo.xml'}, 'torch': {'zip': 'Demo.zip', 'guid': 'g'},
                  'quasar': {'packageManifest': 'quasar-plugin.json'}}
        target_path = os.path.join(self.folder, 'target.json')
        with open(target_path, 'w') as f:
            json.dump(target, f)
        site = os.path.join(self.folder, 'site')
        os.makedirs(site)
        with open(os.path.join(site, 'sitecustomize.py'), 'w', encoding='utf-8') as f:
            f.write(FAKE)
        self.run_dir = os.path.join(self.folder, 'run')
        os.makedirs(self.run_dir)
        self.calls_path = os.path.join(self.folder, 'calls.jsonl')
        self.result_path = os.path.join(self.run_dir, f'{stage}.json')
        env = {**os.environ, 'PYTHONPATH': site, 'FAKE_DOCKER_CALLS': self.calls_path, 'FAKE_DOCKER_FAIL': fail, 'FAKE_DOCKER_INTERRUPT': interrupt, 'PLUGINCHECK_CPUSET': '0-1',
               'PLUGINCHECK_TOKEN_REJECTED': '1', 'PYTHONUTF8': '1'}
        env.pop('PLUGINCHECK_HOLD', None)
        env.pop('PLUGINCHECK_WORLD', None)
        self.done = subprocess.run([sys.executable, os.path.join(ROOT, 'stages', f'{stage}.py'), target_path, self.run_dir, self.result_path],
                                   capture_output=True, text=True, encoding='utf-8', errors='replace', env=env, timeout=120,
                                   stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        self.calls = []
        if os.path.exists(self.calls_path):
            with open(self.calls_path, encoding='utf-8') as f:
                self.calls = [json.loads(line) for line in f]
        self.result = None
        if os.path.exists(self.result_path):
            with open(self.result_path, encoding='utf-8') as f:
                self.result = json.load(f)

    def said(self):
        return self.done.stdout + self.done.stderr


class SingleStageTests(unittest.TestCase):
    def test_every_server_is_created_handed_its_files_and_started(self):
        for stage in STAGES:
            with self.subTest(stage=stage):
                run = Stage(self, stage)
                self.assertIsNotNone(run.result, run.said())
                verbs = [c['args'][0] for c in run.calls]
                creates = [i for i, v in enumerate(verbs) if v == 'create' and 'plugincheck-' in run.calls[i]['args'][2] and 'sdk' not in run.calls[i]['args'][2]]
                self.assertTrue(creates, run.said())
                for i in creates:
                    name = run.calls[i]['args'][2]
                    after = [c['args'] for c in run.calls[i + 1:]]
                    hand_in = next(n for n, a in enumerate(after) if a[:3] == ['cp', '-', f'{name}:/'])
                    started = next(n for n, a in enumerate(after) if a == ['start', name])
                    self.assertLess(hand_in, started, f'{stage}: {name} started before its files were copied in')
                self.assertFalse(any(v == 'run' and '-d' in c['args'] for v, c in zip(verbs, run.calls)), f'{stage} still starts a server with docker run -d')

    def test_no_container_bind_mounts_a_folder_of_this_computer(self):
        for stage in STAGES:
            with self.subTest(stage=stage):
                run = Stage(self, stage)
                for call in run.calls:
                    args = call['args']
                    if args[0] in ('create', 'run'):
                        mounts = [args[i + 1] for i, a in enumerate(args[:-1]) if a in ('-v', '--volume', '--mount')]
                        self.assertEqual([m for m in mounts if not named_volume(m)], [], f'{stage}: {args}')

    def test_every_container_that_runs_is_pinned(self):
        """Each server and each docker run takes pin.args(), here PLUGINCHECK_CPUSET 0-1. A container created and never started runs nothing."""
        for stage in STAGES:
            with self.subTest(stage=stage):
                run = Stage(self, stage)
                started = {c['args'][1] for c in run.calls if c['args'][0] == 'start'}
                running = [c['args'] for c in run.calls if c['args'][0] == 'run' or (c['args'][0] == 'create' and c['args'][2] in started)]
                self.assertTrue(started, run.said())
                for args in running:
                    self.assertTrue(any(args[i:i + 2] == ['--cpuset-cpus', '0-1'] for i in range(len(args))), f'{stage}: not pinned: {args}')

    def test_every_docker_call_has_a_time_limit(self):
        for stage in STAGES:
            with self.subTest(stage=stage):
                run = Stage(self, stage)
                self.assertTrue(run.calls)
                untimed = [c['args'][:3] for c in run.calls if c['timeout'] is None]
                self.assertEqual(untimed, [], stage)
                # The follower of magnetar's log is the one process that runs on: it is waited on for 30 s at most, then killed.
                followed = [c for c in run.calls if c['timeout'] == 'follower']
                self.assertEqual(len(followed), 1 if stage == 'magnetar' else 0, stage)

    def test_the_files_are_owned_by_root_and_readable(self):
        expected = {'magnetar': ('seed/Magnetar/Sources/sources.xml', 'plugin/Demo/Demo.xml'), 'legacy': ('seed/Magnetar/Sources/sources.xml', 'plugin/Demo/Demo.xml'),
                    'torch': ('seed/data/Torch.cfg', 'seed/plugins/Demo.zip'), 'quasar': ('plugins/Demo/quasar-plugin.json',),
                    'quasar_managed': ('plugins/Demo/quasar-plugin.json', 'seed/dev-folders.json')}
        for stage, paths in expected.items():
            with self.subTest(stage=stage):
                run = Stage(self, stage)
                members = [m for c in run.calls if c['tar'] for m in c['tar']]
                names = {m[0] for m in members}
                for path in paths:
                    self.assertIn(path, names, stage)
                for name, uid, gid, mode, folder in members:
                    self.assertEqual((uid, gid, mode), (0, 0, 0o755 if folder else 0o644), f'{stage}: {name}')

    def test_quasar_replaces_release_json_on_its_own_and_reads_the_receipt_from_the_volume(self):
        run = Stage(self, 'quasar')
        self.assertTrue(any(c['args'][0] == 'cp' and c['args'][-1].endswith(':/opt/quasar/release.json') for c in run.calls))
        self.assertFalse(any(m[0].startswith('opt') for c in run.calls if c['tar'] for m in c['tar']), "the image's /opt keeps its owner and mode")
        self.assertTrue(any(c['args'][:2] == ['volume', 'create'] for c in run.calls))
        self.assertTrue(any(c['args'][:3] == ['volume', 'rm', '-f'] for c in run.calls), 'the data volume is removed')
        self.assertTrue(any(c['args'][0] == 'cp' and c['args'][1].endswith(':/data/Updates/migration-1.4.1.json') for c in run.calls))

    def test_quasar_removes_its_server_and_its_data_volume_when_interrupted(self):
        run = Stage(self, 'quasar', interrupt='logs')
        verbs = [c['args'] for c in run.calls]
        at = next(i for i, a in enumerate(verbs) if a[0] == 'logs')
        after = verbs[at + 1:]
        server = verbs[at][1]
        removed = next((i for i, a in enumerate(after) if a[:3] == ['rm', '-f', '-v'] and server in a), None)
        dropped = next((i for i, a in enumerate(after) if a[:3] == ['volume', 'rm', '-f']), None)
        self.assertIsNotNone(removed, f'the server is removed: {after}')
        self.assertIsNotNone(dropped, f'the data volume is removed: {after}')
        self.assertLess(removed, dropped, 'a volume in use is not removed, so the server goes first')
        self.assertFalse(any(a[0] == 'start' for a in after), 'nothing starts after the interrupt')

    def test_a_server_whose_files_cannot_be_copied_in_says_why_and_is_removed(self):
        for stage in STAGES:
            with self.subTest(stage=stage):
                run = Stage(self, stage, fail='cp')
                self.assertIsNotNone(run.result, run.said())
                text = json.dumps(run.result)
                self.assertIn('did not start', text, stage)
                self.assertIn('its files could not be copied in', text, stage)
                self.assertIn('gave no answer in 120s', text, stage)
                self.assertTrue(any(c['args'][:2] == ['rm', '-f'] for c in run.calls), f'{stage}: the created container is removed')
                self.assertNotIn('start', [c['args'][0] for c in run.calls], stage)


class DepthGuardTests(unittest.TestCase):
    """The depth guard is gone: Docker Desktop's depth limit hits only a container that runs as a user and bind-mounts a folder, and none
    does any more. What is still bind-mounted is in short build runs of the .NET SDK image and lint's Quasar image, which run as root."""

    def test_no_guard_and_no_check(self):
        self.assertFalse(os.path.exists(os.path.join(ROOT, 'stages', 'mountdepth.py')))
        with open(os.path.join(ROOT, 'check.py'), encoding='utf-8') as f:
            self.assertNotIn('mountdepth', f.read())

    def test_every_remaining_bind_mount_is_a_root_container(self):
        """Any '-v' whose source is a folder here must be in a docker run of the SDK or Quasar image, which run as root (neither sets a USER)."""
        for path in [os.path.join(ROOT, 'stages', f) for f in os.listdir(os.path.join(ROOT, 'stages')) if f.endswith('.py')] + \
                [os.path.join(ROOT, 'stages', 'behaviour', f) for f in os.listdir(os.path.join(ROOT, 'stages', 'behaviour')) if f.endswith('.py')]:
            with open(path, encoding='utf-8') as f:
                text = f.read()
            for node in ast.walk(ast.parse(text)):
                items = node.args if isinstance(node, ast.Call) else node.elts if isinstance(node, ast.List) else None
                # docker rm -v removes volumes; it mounts nothing.
                if not items or any(isinstance(x, ast.Constant) and x.value == 'rm' for x in items[:3]):
                    continue
                for i, item in enumerate(items[:-1]):
                    if not (isinstance(item, ast.Constant) and item.value == '-v'):
                        continue
                    mount = items[i + 1]
                    first = mount.values[0] if isinstance(mount, ast.JoinedStr) else mount
                    volume = (isinstance(first, ast.Constant) and str(first.value).startswith('plugincheck-')) or \
                             (isinstance(first, ast.FormattedValue) and ast.unparse(first.value) in ('volume', 'v', 'ds_volume') or
                              isinstance(first, ast.FormattedValue) and ast.unparse(first.value).startswith('VOLUMES['))
                    if volume:
                        continue
                    segment = ast.get_source_segment(text, node)
                    self.assertTrue(re.search(r'\b(SDK|sdk|QUASAR)\b', segment), f'{os.path.basename(path)}:{node.lineno} bind-mounts {ast.unparse(mount)} outside the SDK and Quasar images')


if __name__ == '__main__':
    unittest.main()
