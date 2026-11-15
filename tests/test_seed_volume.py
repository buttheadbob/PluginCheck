"""One volume per run holds Magnetar's download cache, filled once and mounted read-only into every Magnetar server, so each server's tar
carries only its own seed. Run with: python -I -m unittest discover -s tests"""
import ast, io, os, re, shutil, sys, tarfile, tempfile, threading, time, types, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(ROOT, 'stages'))
import classification, containers  # noqa: E402

SOURCE = os.path.join(ROOT, 'stages', 'behaviour.py')
TEXT = open(SOURCE, encoding='utf-8').read()


def lifted(*wanted, **namespace):
    tree = ast.parse(TEXT)
    parts = [ast.get_source_segment(TEXT, n) for n in tree.body
             if (isinstance(n, ast.FunctionDef) and n.name in wanted) or
             (isinstance(n, ast.Assign) and {x.id for t in n.targets for x in ast.walk(t) if isinstance(x, ast.Name)} & set(wanted))]
    space = {'os': os, 're': re, 'threading': threading, 'time': time, 'classification': classification, **namespace}
    exec(compile('\n\n'.join(parts), SOURCE, 'exec'), space)
    missing = [w for w in wanted if w not in space]
    if missing:
        raise AssertionError(f'behaviour.py has no {", ".join(missing)}')
    return space


def answer(code=0, out='', err=''):
    return types.SimpleNamespace(returncode=code, stdout=out, stderr=err)


class Recorder:
    def __init__(self, fail=None):
        self.calls, self.fail, self.lock = [], fail, threading.Lock()

    def __call__(self, *args, **options):
        with self.lock:
            self.calls.append(args)
        if self.fail and args[0] == self.fail:
            return answer(1, err='boom')
        return answer()


def cache_folder(test, text='one'):
    folder = tempfile.mkdtemp(prefix='pc-seedvol-')
    test.addCleanup(shutil.rmtree, folder, True)
    os.makedirs(os.path.join(folder, 'GitHub', 'MagnetarCore', 'Bin'))
    with open(os.path.join(folder, 'GitHub', 'MagnetarCore', 'Bin', 'core.dll'), 'w') as f:
        f.write(text)
    return folder


class FillTests(unittest.TestCase):
    def setUp(self):
        quiet = mock.patch('sys.stdout', io.StringIO())
        quiet.start()
        self.addCleanup(quiet.stop)

    def functions(self, docker, hand_in, cache):
        return lifted('seed_volumes', 'seed_volume', 'fill_seed_volume', docker=docker, hand_in=hand_in, magnetar_cache=cache, work=cache,
                      MAGNETAR='ghcr.io/cometworks/magnetar:latest', CLEAN_SECONDS=30)

    def test_filled_once_while_every_other_server_waits_for_it(self):
        docker, handed = Recorder(), []

        def hand_in(container, folder, files):
            time.sleep(0.2)
            handed.append((container, files))
            return None
        cache = cache_folder(self)
        space = self.functions(docker, hand_in, cache)
        got = []
        threads = [threading.Thread(target=lambda: got.append(space['seed_volume']())) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        name = f'plugincheck-behaviour-{os.getpid()}-seed-{classification.folder_hash(cache)[:12]}'
        self.assertEqual(got, [name] * 8)
        self.assertEqual([c for c in docker.calls if c[:2] == ('volume', 'create')], [('volume', 'create', name)])
        self.assertEqual(handed, [(name + '-fill', {cache: '/cache'})])
        self.assertIn(('create', '--name', name + '-fill', '-v', f'{name}:/cache', 'ghcr.io/cometworks/magnetar:latest'), docker.calls)
        self.assertIn(('rm', '-f', '-v', name + '-fill'), docker.calls, 'the filler container goes, the volume stays')
        self.assertNotIn(('volume', 'rm', '-f', name), docker.calls)

    def test_named_by_the_cache_content(self):
        names = []
        for text in ('one', 'two'):
            space = self.functions(Recorder(), lambda *a: None, cache_folder(self, text))
            names.append(space['seed_volume']())
        self.assertNotEqual(names[0], names[1])
        self.assertTrue(all(re.fullmatch(rf'plugincheck-behaviour-{os.getpid()}-seed-[0-9a-f]{{12}}', n) for n in names))

    def test_a_fill_that_fails_falls_back_to_each_servers_own_tar_once(self):
        docker = Recorder()
        tries = []

        def hand_in(container, folder, files):
            tries.append(container)
            return 'docker cp - x:/ failed: gave no answer in 120s'
        space = self.functions(docker, hand_in, cache_folder(self))
        self.assertIsNone(space['seed_volume']())
        self.assertIsNone(space['seed_volume']())
        out = sys.stdout
        self.assertEqual(len(tries), 1, 'not tried again for every server')
        self.assertTrue(any(c[:3] == ('volume', 'rm', '-f') for c in docker.calls), 'the half-filled volume is removed')
        self.assertIn('gets it in its own tar', out.getvalue())


class StartTests(unittest.TestCase):
    def create_args(self, cached):
        docker = Recorder()
        space = lifted('start_magnetar', 'VOLUMES', 'MAGNETAR', 'CLUSTER_NODE', 'MEMORY_PER_SERVER_GIB', start_server=containers.start_server,
                       githubtoken=types.SimpleNamespace(for_container=lambda: ([], None)))
        folder = tempfile.mkdtemp(prefix='pc-seedvol-')
        self.addCleanup(shutil.rmtree, folder, True)
        os.makedirs(os.path.join(folder, 'seed', 'Instance'))
        os.makedirs(os.path.join(folder, 'source', 'Demo'))
        with mock.patch.object(containers, 'docker', docker), mock.patch('sys.stdout', io.StringIO()):
            space['start_magnetar']({}, os.path.join(folder, 'seed'), folder, 'plugincheck-behaviour-1-1', 'plugincheck-behaviour-1-1-data', '0-1', False, cached)
        return next(c for c in docker.calls if c[0] == 'create')

    def test_the_cache_volume_is_mounted_read_only_and_copied_in_before_the_seed(self):
        args = self.create_args('plugincheck-behaviour-1-seed-0123456789ab')
        self.assertIn('plugincheck-behaviour-1-seed-0123456789ab:/magnetar-cache:ro', args)
        script = args[-1]
        self.assertLess(script.index('cp -r /magnetar-cache/. /data/Magnetar/'), script.index('cp -r /seed/. /data/'))

    def test_without_a_cache_volume_the_server_starts_as_before(self):
        args = self.create_args(None)
        self.assertFalse(any('magnetar-cache' in a for a in args))
        self.assertEqual(args[-1], 'cp -r /seed/. /data/; exec tini -- magnetar-entrypoint')

    def test_the_seed_leaves_the_cache_out_when_the_volume_holds_it(self):
        source = ast.unparse(next(n for n in ast.parse(TEXT).body if isinstance(n, ast.FunctionDef) and n.name == 'seed_magnetar'))
        self.assertIn('cached = seed_volume() if magnetar_cache and (not legacy) else None', source)
        self.assertIn('if magnetar_cache and (not legacy) and (not cached):', source)
        self.assertIn('cached = seed_magnetar(build, seed, folder)', TEXT)
        self.assertIn("any((p.get('node') for p in batch)), cached)", ast.unparse(ast.parse(TEXT)))


class LifetimeTests(unittest.TestCase):
    """The cache volume and its filler go with the servers: at the clean end, the interrupt and the stall stop (remove_own_servers), and
    at the start-up sweep when the run that made them is gone."""

    class FakeDocker:
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

    def removal(self, docker, alive=lambda pid: True):
        return lifted('SERVER_NAME', 'END_SECONDS', 'CLEAN_SECONDS', 'ending', 'remove_servers', 'remove_own_servers', 'remove_abandoned_servers',
                      docker=docker, leases=types.SimpleNamespace(process_alive=alive))

    def test_the_clean_end_removes_the_cache_volume_and_a_leftover_filler(self):
        me, other = os.getpid(), os.getpid() + 1
        docker = self.FakeDocker([f'plugincheck-behaviour-{me}-1', f'plugincheck-behaviour-{me}-seed-0123456789ab-fill'],
                                 [f'plugincheck-behaviour-{me}-1-data', f'plugincheck-behaviour-{me}-seed-0123456789ab', f'plugincheck-behaviour-{other}-seed-ffffffffffff'])
        self.removal(docker)['remove_own_servers']()
        self.assertEqual(docker.containers, [])
        self.assertEqual(docker.volumes, [f'plugincheck-behaviour-{other}-seed-ffffffffffff'], "another run's cache volume is left alone")

    def test_the_sweep_removes_the_cache_volume_of_a_run_that_is_gone(self):
        dead, live = 111, 222
        docker = self.FakeDocker([], [f'plugincheck-behaviour-{dead}-seed-0123456789ab', f'plugincheck-behaviour-{live}-seed-0123456789ab', 'plugincheck-ds-magnetar'])
        self.removal(docker, lambda pid: pid != dead)['remove_abandoned_servers']()
        self.assertEqual(docker.volumes, [f'plugincheck-behaviour-{live}-seed-0123456789ab', 'plugincheck-ds-magnetar'])

    def test_the_name_pattern(self):
        pattern = lifted('SERVER_NAME')['SERVER_NAME']
        for name in ('plugincheck-behaviour-12-3', 'plugincheck-behaviour-12-3-data', 'plugincheck-behaviour-12-seed-0a1b2c3d4e5f',
                     'plugincheck-behaviour-12-seed-0a1b2c3d4e5f-fill'):
            self.assertTrue(pattern.fullmatch(name), name)
        for name in ('plugincheck-behaviour-libs-12', 'plugincheck-behaviour-12-seed-', 'plugincheck-behaviour-12-seed-xyz', 'plugincheck-ds-magnetar'):
            self.assertFalse(pattern.fullmatch(name), name)


if __name__ == '__main__':
    unittest.main()
