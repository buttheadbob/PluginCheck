"""Every working folder a stage makes in the run folder is removed when the run ends. Run with: python -I -m unittest discover -s tests"""
import ast, glob, os, re, unittest

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
# Kept on purpose: logs, which a person or an AI reads after the run.
KEPT = {'torch-logs'}


def cleaned():
    source = open(os.path.join(ROOT, 'check.py'), encoding='utf-8').read()
    found = re.search(r"for folder in \(([^)]*)\):\n\s+shutil\.rmtree\(os\.path\.join\(run_dir, folder\)", source)
    return set(ast.literal_eval('(' + found.group(1) + ')'))


def made():
    """The folder names stages join onto run_dir: os.path.join(run_dir, '<name>') with no file extension."""
    folders = {}
    for path in glob.glob(os.path.join(ROOT, 'stages', '*.py')) + glob.glob(os.path.join(ROOT, 'stages', 'behaviour', '*.py')):
        for node in ast.walk(ast.parse(open(path, encoding='utf-8').read())):
            if (isinstance(node, ast.Call) and ast.unparse(node.func) == 'os.path.join' and len(node.args) >= 2
                    and ast.unparse(node.args[0]) == 'run_dir' and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)
                    and '.' not in node.args[1].value):
                folders.setdefault(node.args[1].value, set()).add(os.path.basename(path))
    return folders


class CleanupTests(unittest.TestCase):
    def test_every_working_folder_is_removed(self):
        """A folder missing from the list, such as storage's source copies and libraries, stays in every run folder."""
        left = {name: sorted(files) for name, files in made().items() if name not in cleaned() | KEPT}
        self.assertEqual(left, {})

    def test_storage_in_particular(self):
        self.assertIn('storage', made())
        self.assertIn('storage', cleaned())


if __name__ == '__main__':
    unittest.main()
