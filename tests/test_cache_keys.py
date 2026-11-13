"""A kept result's key holds what shapes a server, and nothing else. Run with: python -I -m unittest discover -s tests

A wrong split reuses stale results, so these check which code is hashed, that a change to it changes the hash, and that a change to the rest
does not.
"""
import ast, hashlib, json, os, shutil, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
STAGES = os.path.normpath(os.path.join(HERE, '..', 'stages'))
for path in (os.path.join(STAGES, 'behaviour'), STAGES):
    if path not in sys.path:
        sys.path.insert(0, path)
import serverhash

SOURCE = os.path.join(STAGES, 'behaviour.py')
SERVER = {'serve', 'scenario_file', 'seed_magnetar', 'start_magnetar', 'start_legacy', 'seed_torch', 'start_torch', 'dedicated_config',
          'remember_magnetar_cache', 'copy_world_files', 'build_torch_probe', 'START_TIMEOUT', 'MEMORY_PER_SERVER_GIB', 'CLUSTER_NODE', 'VOLUMES',
          'MAGNETAR', 'TORCH', 'LEGACY', 'PROBE', 'PROBE_ID', 'TORCH_PROBE_ID', 'NO_CRASH_REPORTS'}
ORCHESTRATION = {'generate', 'command_lines', 'argument', 'runs_on', 'fits', 'compare', 'take', 'differences', 'disagreements', 'replies_of', 'touched',
                 'changes', 'undeclared', 'run_server', 'run_with_retry', 'run_and_remember', 'run_and_remember_discovery', 'remembered', 'job_key',
                 'kept', 'discover', 'note_arguments', 'capacity', 'keep_lending', 'clock', 'IDLE_SECONDS', 'COMPARED', 'REPEATS', 'CORES_PER_SERVER'}


def source():
    return open(SOURCE, encoding='utf-8').read()


def functions(tree):
    return {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}


class SplitTests(unittest.TestCase):
    def test_the_server_code_is_hashed_and_the_orchestration_is_not(self):
        reached = set(serverhash.code(source()))
        self.assertEqual(SERVER - reached, set())
        self.assertEqual(ORCHESTRATION & reached, set())

    def test_a_name_bound_only_in_a_nested_scope_is_still_read(self):
        """The outer function reads the module's LIMIT; the lambda and the comprehension bind their own LIMIT and step."""
        tree = ast.parse("def f(step):\n    later = lambda LIMIT: LIMIT\n    picked = [LIMIT for LIMIT in range(3)]\n    return LIMIT + step + later(1)\n")
        self.assertIn('LIMIT', serverhash.used(tree.body[0]))
        self.assertNotIn('step', serverhash.used(tree.body[0]))
        self.assertNotIn('later', serverhash.used(tree.body[0]))

    def test_data_is_never_code(self):
        names = serverhash.defined(ast.parse(source()))
        for name in serverhash.DATA:
            with self.subTest(name=name):
                self.assertIn(name, names, 'not a name behaviour.py defines')
                self.assertFalse(any(isinstance(node, (ast.FunctionDef, ast.ClassDef)) for node in names[name]))

    def test_everything_a_hashed_function_reads_is_hashed_or_data(self):
        text = source()
        tree = ast.parse(text)
        names = serverhash.defined(tree)
        reached = serverhash.code(text)
        for name in reached:
            for node in names[name]:
                for read in serverhash.used(node):
                    if read in names:
                        self.assertTrue(read in reached or read in serverhash.DATA, f'{name} reads {read}, which is neither hashed nor DATA')

    def test_the_path_to_a_server_calls_nothing_else_of_behaviour_py(self):
        """run_and_remember, run_with_retry and run_server stand between a job and serve(). If one of them calls more of behaviour.py, that
        code may change what a server gets, and this split has to be looked at again."""
        tree = ast.parse(source())
        defs = functions(tree)
        calls = lambda name: {n.func.id for n in ast.walk(defs[name]) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in defs}
        self.assertEqual(calls('run_and_remember'), {'run_with_retry', 'job_key'})
        self.assertEqual(calls('run_with_retry'), {'run_server'})
        self.assertEqual(calls('run_server'), {'serve', 'status'})
        self.assertEqual(calls('run_and_remember_discovery'), {'run_with_retry', 'job_key'})


class HashTests(unittest.TestCase):
    """A copy of the stages folder, changed one place at a time."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix='pc-hash-')
        stages = os.path.join(self.folder, 'stages')
        shutil.copytree(STAGES, stages, ignore=shutil.ignore_patterns('__pycache__', 'bin', 'obj'))
        self.stages = stages
        self.before = serverhash.tool_hash(stages)

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def change(self, relative, old, new):
        path = os.path.join(self.stages, relative)
        text = open(path, encoding='utf-8').read()
        self.assertEqual(text.count(old), 1, old)
        open(path, 'w', encoding='utf-8', newline='').write(text.replace(old, new))
        return serverhash.tool_hash(self.stages)

    def test_the_hash_is_the_same_twice(self):
        self.assertEqual(serverhash.tool_hash(self.stages), self.before)

    def test_orchestration_changes_keep_the_hash(self):
        for relative, old, new in (('behaviour.py', 'IDLE_SECONDS = 120', 'IDLE_SECONDS = 121'),
                                   ('behaviour.py', '    """Where two records of the same scenario disagree at the end, grid by grid."""',
                                    '    """Where two records of the same scenario disagree at the end, grid by grid (changed)."""'),
                                   ('behaviour.py', "pool_threads = workers + max(2, workers // 4)", "pool_threads = workers + 3"),
                                   (os.path.join('behaviour', 'timing.py'), "POINTS = ('up', 'stopped', 'killed', 'freed', 'cleaned')", "POINTS = ('up', 'stopped')")):
            with self.subTest(old=old):
                self.assertEqual(self.change(relative, old, new), self.before)

    def test_server_changes_change_the_hash(self):
        for relative, old, new in (('behaviour.py', 'START_TIMEOUT = 20 * 60', 'START_TIMEOUT = 21 * 60'),
                                   ('behaviour.py', 'MEMORY_PER_SERVER_GIB = 1.8', 'MEMORY_PER_SERVER_GIB = 2.0'),
                                   ('behaviour.py', "        minimum, quiet = (2, 3) if plan.get('readonly')", "        minimum, quiet = (3, 3) if plan.get('readonly')"),
                                   ('behaviour.py', "lines = sorted({re.sub(r'^\\S+ \\S+\\s*', '', l).strip()[:200]", "lines = sorted({re.sub(r'^\\S+ \\S+\\s*', '', l).strip()[:300]"),
                                   ('behaviour.py', '  <EnableAnalytics>false</EnableAnalytics>', '  <EnableAnalytics>false</EnableAnalytics>\n'),
                                   (os.path.join('behaviour', 'fixture.py'), 'import datetime, os, re, shutil', 'import datetime, os, re, shutil  # changed'),
                                   ('startlog.py', 'import datetime, json, re, time, urllib.request', 'import datetime, json, re, time, urllib.request  # changed'),
                                   (os.path.join('behaviour', 'probe', 'src', 'Probe.cs'), 'using System.Linq;', 'using System.Linq; // changed')):
            with self.subTest(old=old):
                changed = self.change(relative, old, new)
                self.assertNotEqual(changed, self.before)
                self.change(relative, new, old)


class KeyTests(unittest.TestCase):
    """job_key and scenario_file lifted out of behaviour.py."""

    def key(self, batch, options=None, image='sha256:aaa'):
        tree = ast.parse(source())
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ('job_key', 'scenario_file')]
        namespace = {'hashlib': hashlib, 'json': json, 'options': options or {}, 'fingerprints': {'b': 'f'}, 'tools': 't', 'games': {'magnetar': 'g'},
                     'images': {'magnetar': image}}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), SOURCE, 'exec'), namespace)
        return namespace['job_key']({'name': 'b', 'host': 'magnetar', 'plugin': 'Demo'}, batch, 0)

    def test_what_the_reading_sets_in_the_scenario_is_in_the_key(self):
        plan = {'name': '!demo info', 'command': '!demo info', 'caller': '', 'players': False}
        self.assertNotEqual(self.key([{**plan, 'readonly': True}]), self.key([{**plan, 'readonly': False}]))
        self.assertEqual(self.key([{**plan, 'readonly': True}]), self.key([{**plan, 'readonly': True}]))

    def test_a_plan_name_alone_is_not_in_the_key(self):
        plan = {'command': '!demo info', 'caller': '', 'players': False, 'readonly': False}
        self.assertEqual(self.key([{**plan, 'name': 'a'}]), self.key([{**plan, 'name': 'b'}]))

    def test_the_image_a_server_runs_from_is_in_the_key(self):
        batch = [{'name': 'x', 'command': '!demo info'}]
        self.assertNotEqual(self.key(batch, image='sha256:aaa'), self.key(batch, image='sha256:bbb'))

    def test_image_hash_asks_docker_for_each_host_image(self):
        tree = ast.parse(source())
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'image_hash']
        nodes += [n for n in tree.body if isinstance(n, ast.Assign) and any(getattr(t, 'id', '') in ('MAGNETAR', 'LEGACY', 'TORCH') for t in n.targets)]
        asked = []

        def docker(*args):
            asked.append(args)
            return type('Out', (), {'stdout': '' if args[-1].startswith('plugincheck-magnetar-legacy') else f'sha256:{len(asked)}\n'})()
        namespace = {'docker': docker, 'os': os}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), SOURCE, 'exec'), namespace)
        self.assertEqual(namespace['image_hash']('magnetar'), 'sha256:1')
        self.assertEqual(asked[0], ('image', 'inspect', '-f', '{{.Id}}', 'ghcr.io/cometworks/magnetar:latest'))
        self.assertEqual(namespace['image_hash']('torch'), 'sha256:2')
        self.assertEqual(namespace['image_hash']('legacy'), 'unknown')

    def test_the_target_settle_and_world_options_are(self):
        batch = [{'name': 'x', 'command': '!demo info'}]
        self.assertNotEqual(self.key(batch, {'settleSeconds': 70}), self.key(batch))
        self.assertNotEqual(self.key(batch, {'programmableBlocks': True}), self.key(batch))


if __name__ == '__main__':
    unittest.main()
