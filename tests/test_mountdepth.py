"""A run folder too deep for Docker Desktop's mounts is refused before it starts. Run with: python -I -m unittest discover -s tests"""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(HERE, '..', 'stages')))
import mountdepth


def run_dir(*folders):
    return os.path.join('C:\\', *folders, 'runs', 'Target', '20261109-120000')


class MountDepthTests(unittest.TestCase):
    def test_the_usual_place_is_fine(self):
        self.assertIsNone(mountdepth.problem(run_dir('Development', 'Development', 'Public', 'PluginCheck'), windows=True))

    def test_seventeen_parts_pass_and_eighteen_are_refused(self):
        """A mounted folder of 17 parts, the drive counted, reads as a user; 18 gives Input/output error. The deepest mount is
        <run>/behaviour/runs/<server>/seed, 4 below the run folder, which is 3 below PluginCheck."""
        seventeen = run_dir('a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i')
        self.assertEqual(len(mountdepth.deepest(seventeen).split(os.sep)), 17)
        self.assertIsNone(mountdepth.problem(seventeen, windows=True))
        eighteen = run_dir('a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j')
        said = mountdepth.problem(eighteen, windows=True)
        self.assertIn('18 levels deep', said)
        self.assertIn('Move PluginCheck at least 1 level higher', said)

    def test_a_copy_under_a_deep_temp_folder_is_refused(self):
        """A copy of PluginCheck several folders down in a user's temp folder: the run folder is 14 parts, so its server folders are 18."""
        deep = os.path.join('C:\\', 'Users', 'u', 'AppData', 'Local', 'Temp', 'work', 'x', 'y', 'z', 'copy',
                            'runs', 'AdvancedPBLimiter', '20261109-113615')
        self.assertIn('levels deep', mountdepth.problem(deep, windows=True))

    def test_only_on_windows(self):
        self.assertIsNone(mountdepth.problem(run_dir(*'abcdefghijklmn'), windows=False))

    def test_check_py_refuses_before_making_the_run_folder(self):
        source = open(os.path.join(HERE, '..', 'check.py'), encoding='utf-8').read()
        self.assertLess(source.index('too_deep = mountdepth.problem(run_dir)'), source.index('os.makedirs(run_dir)'))
        self.assertIn('    sys.exit(too_deep)', source)


if __name__ == '__main__':
    unittest.main()
