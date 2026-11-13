"""A plugin repository can junction or symlink a game or Torch install into its tree. Copies and walks of its source leave those
folders out, so the install's files, its logs included, never reach a run. Run with: python -I -m unittest discover -s tests"""
import ast, json, os, shutil, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
STAGES = os.path.normpath(os.path.join(HERE, '..', 'stages'))
for path in (os.path.join(STAGES, 'behaviour'), STAGES):
    if path not in sys.path:
        sys.path.insert(0, path)
import classification, repocopy

NO_WINDOW = 0x08000000
LOG = 'Torch.log'


@unittest.skipUnless(os.name == 'nt', 'junctions are a Windows feature')
class JunctionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix='pc-junction-')
        self.addCleanup(self.clean)
        # The install a repository links to, with a log in it.
        self.install = os.path.join(self.folder, 'install')
        os.makedirs(os.path.join(self.install, 'Logs'))
        open(os.path.join(self.install, 'Logs', LOG), 'w').write('2026-01-01 00:00:00 [ERROR] player 76561190000000099 Exception\n')
        self.repo = os.path.join(self.folder, 'repo')
        os.makedirs(os.path.join(self.repo, 'Plugin'))
        open(os.path.join(self.repo, 'Plugin', 'Plugin.cs'), 'w').write('class Plugin { }')
        # One under a name the stages already skip, and one under a name nothing knows.
        self.links = [os.path.join(self.repo, 'TorchBinaries'), os.path.join(self.repo, 'Plugin', 'Linked')]
        for link in self.links:
            made = subprocess.run(['cmd', '/c', 'mklink', '/J', link, self.install], capture_output=True, text=True, creationflags=NO_WINDOW)
            self.assertEqual(made.returncode, 0, made.stdout + made.stderr)

    def clean(self):
        # Removing a junction removes the link, never what it points at.
        for link in self.links:
            if os.path.lexists(link):
                os.rmdir(link)
        shutil.rmtree(self.folder, True)

    def logs_in(self, folder):
        return [os.path.join(d, f) for d, _, fs in os.walk(folder) for f in fs if f == LOG]

    def test_a_junction_is_linked(self):
        self.assertTrue(all(repocopy.linked(link) for link in self.links))
        self.assertFalse(repocopy.linked(os.path.join(self.repo, 'Plugin')))

    def test_the_repository_copy_leaves_it_out(self):
        copy = os.path.join(self.folder, 'copy')
        shutil.copytree(self.repo, copy, ignore=repocopy.ignore(self.repo))
        self.assertTrue(os.path.exists(os.path.join(copy, 'Plugin', 'Plugin.cs')))
        self.assertEqual(self.logs_in(copy), [])

    def test_the_torch_overlay_copy_leaves_it_out(self):
        """The copy behaviour.py makes of the whole repository for a Torch variant with an overlay, run as written there."""
        source = open(os.path.join(STAGES, 'behaviour.py'), encoding='utf-8').read()
        call = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call) and ast.get_source_segment(source, n.func) == 'shutil.copytree'
                    and ast.get_source_segment(source, n.args[0]) == 'repo' and "overlay_source" in ast.get_source_segment(source, n.args[1]))
        build = {'overlay_source': os.path.join(self.folder, 'overlay-source')}
        eval(compile(ast.Expression(call), 'behaviour.py', 'eval'), {'shutil': shutil, 'repocopy': repocopy, 'repo': self.repo, 'build': build})
        self.assertTrue(os.path.exists(os.path.join(build['overlay_source'], 'Plugin', 'Plugin.cs')))
        self.assertEqual(self.logs_in(build['overlay_source']), [])

    def test_the_walk_and_the_hash_leave_it_out(self):
        self.assertEqual([os.path.join(d, f) for d, _, fs in repocopy.walk(self.repo) for f in fs], [os.path.join(self.repo, 'Plugin', 'Plugin.cs')])
        plain = os.path.join(self.folder, 'plain')
        os.makedirs(os.path.join(plain, 'Plugin'))
        shutil.copy(os.path.join(self.repo, 'Plugin', 'Plugin.cs'), os.path.join(plain, 'Plugin'))
        self.assertEqual(classification.folder_hash(self.repo), classification.folder_hash(plain))

    def test_logcheck_does_not_read_it(self):
        run = os.path.join(self.folder, 'run')
        os.makedirs(run)
        link = os.path.join(run, 'Linked')
        self.links.append(link)
        made = subprocess.run(['cmd', '/c', 'mklink', '/J', link, self.install], capture_output=True, text=True, creationflags=NO_WINDOW)
        self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
        target = os.path.join(self.folder, 'target.json')
        json.dump({'name': 'Demo', 'repo': self.repo}, open(target, 'w'))
        result = os.path.join(self.folder, 'logcheck.json')
        subprocess.run([sys.executable, '-I', '-X', 'utf8', os.path.join(STAGES, 'logcheck.py'), target, run, result], capture_output=True, creationflags=NO_WINDOW)
        found = json.load(open(result, encoding='utf-8'))
        self.assertEqual((found['logs'], found['lines']), (0, []))


if __name__ == '__main__':
    unittest.main()
