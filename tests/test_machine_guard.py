"""No test run touches a lease folder the checkout uses for real (its machine-dir.txt folder or its own machine folder): python -I -m unittest discover -s tests

Every test module, and the replay harness, imports scratchmachine first, which points PLUGINCHECK_MACHINE_DIR at a scratch folder and makes
the checkout's real lease folders refuse to open from the test process (tests/scratchmachine.py).
"""
import ast, glob, json, os, shutil, subprocess, sys, tempfile, unittest
import scratchmachine  # noqa: F401  (before anything of PluginCheck's: keeps every test off the real lease folders)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(ROOT, 'stages'))
import leases  # noqa: E402

NO_WINDOW = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
# Stands in for Docker in lease.py: `docker ps` is recorded and fails, as with no Docker at all.
SPIED_LEASE_PY = r'''
import json, os, runpy, subprocess, sys
_run = subprocess.run


def run(args, *rest, **options):
    if args and args[0] == 'docker':
        with open(os.environ['DOCKER_SPY_CALLS'], 'a', encoding='utf-8') as f:
            f.write(json.dumps(list(args)) + '\n')
        return subprocess.CompletedProcess(args, 1, '', '')
    return _run(args, *rest, **options)


subprocess.run = run
sys.argv = [sys.argv[1], 'status']
runpy.run_path(sys.argv[0], run_name='__main__')
'''


class MachineGuardTests(unittest.TestCase):
    def test_every_test_module_and_the_replay_harness_import_the_guard_first(self):
        """Only standard library imports may come before it: anything of PluginCheck's could read PLUGINCHECK_MACHINE_DIR before it is set."""
        files = sorted(glob.glob(os.path.join(HERE, 'test_*.py'))) + [os.path.join(HERE, 'replay.py')]
        self.assertGreater(len(files), 20)
        for path in files:
            with self.subTest(os.path.basename(path)):
                with open(path, encoding='utf-8') as f:
                    tree = ast.parse(f.read())
                before = []
                for node in tree.body:
                    if isinstance(node, ast.Import) and any(a.name == 'scratchmachine' for a in node.names):
                        break
                    if isinstance(node, ast.Import):
                        before += [a.name.split('.')[0] for a in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        before.append((node.module or '').split('.')[0])
                    elif not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)):
                        self.fail(f'{os.path.basename(path)}: {ast.unparse(node)[:80]} comes before import scratchmachine')
                else:
                    self.fail(f'{os.path.basename(path)} does not import scratchmachine')
                self.assertEqual([m for m in before if m not in sys.stdlib_module_names], [])

    def test_processes_a_test_starts_inherit_the_guard(self):
        self.assertRegex(os.environ['PLUGINCHECK_RUN_QUEUE'], rf'^pc-test-{os.getpid()}-')
        listed = os.environ[leases.FORBIDDEN_ENV].split(os.pathsep)
        self.assertEqual(listed, scratchmachine.FORBIDDEN)
        with self.assertRaisesRegex(ValueError, 'is refused here'):
            leases.machine_folder({**os.environ, 'PLUGINCHECK_MACHINE_DIR': os.path.join(ROOT, 'machine')})

    def test_this_process_uses_a_scratch_lease_folder(self):
        self.assertEqual(os.environ['PLUGINCHECK_MACHINE_DIR'], scratchmachine.FOLDER)
        self.assertEqual(leases.FOLDER, scratchmachine.FOLDER)
        self.assertFalse(scratchmachine.forbidden(leases.FOLDER))
        self.assertEqual(os.path.commonpath([scratchmachine.FOLDER, tempfile.gettempdir()]), tempfile.gettempdir())
        self.assertEqual(leases.Store().folder, scratchmachine.FOLDER)
        self.assertEqual(leases.Owner(['0-1']).folder, scratchmachine.FOLDER)

    def test_the_checkouts_real_folders_are_forbidden(self):
        """Its own machine folder, and the folder its machine-dir.txt names when it has a valid one."""
        self.assertTrue(scratchmachine.forbidden(os.path.join(ROOT, 'machine')))
        try:
            named = leases.machine_folder({})
        except ValueError:
            named = None
        if named:
            self.assertTrue(scratchmachine.forbidden(named))

    def test_a_real_lease_folder_refuses_to_open_from_a_test(self):
        before = list(scratchmachine.touched)
        real = os.path.join(ROOT, 'machine')
        try:
            with self.assertRaisesRegex(AssertionError, 'a folder this checkout uses for real'):
                with leases.Store(real):
                    pass
            self.assertEqual(scratchmachine.touched, before + [real])
        finally:
            # This test's own touch was refused on purpose; any other fails the run at exit.
            scratchmachine.touched[:] = before

    def test_no_test_so_far_has_touched_a_real_lease_folder(self):
        self.assertEqual(scratchmachine.touched, [])

    def test_a_process_a_test_starts_uses_the_scratch_folder_too(self):
        folder = tempfile.mkdtemp(prefix='pc-guard-')
        calls = os.path.join(folder, 'docker-calls')
        script = os.path.join(folder, 'spied.py')
        with open(script, 'w', encoding='utf-8') as f:
            f.write(SPIED_LEASE_PY)
        try:
            done = subprocess.run([sys.executable, '-I', script, os.path.join(ROOT, 'lease.py')], capture_output=True, text=True, timeout=60,
                                  env={**os.environ, 'DOCKER_SPY_CALLS': calls}, stdin=subprocess.DEVNULL, **NO_WINDOW)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn('pluginCheckRunning', done.stdout)
            self.assertTrue(os.path.isfile(os.path.join(scratchmachine.FOLDER, 'leases.json')), 'lease.py wrote into the scratch folder')
            with open(calls, encoding='utf-8') as f:
                self.assertEqual([json.loads(line)[:2] for line in f], [['docker', 'ps']], 'the stand-in for Docker took the one docker call')
        finally:
            shutil.rmtree(folder, True)


if __name__ == '__main__':
    unittest.main()
