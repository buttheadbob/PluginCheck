"""A server's container.log keeps what its entrypoint printed on stderr. Run with: python -I -m unittest discover -s tests"""
import ast, os, types, unittest

SOURCE = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'stages', 'behaviour.py'))


def lifted(name):
    """One function out of behaviour.py, which runs a whole stage when imported."""
    tree = ast.parse(open(SOURCE, encoding='utf-8').read())
    return [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]


class ContainerLogTests(unittest.TestCase):
    def test_error_output_is_kept_and_comes_last(self):
        """Docker writes what the container printed on stderr to its own stderr. Before, only stdout was kept, so an entrypoint that stopped
        with an error showed no reason in the server's Last lines."""
        nodes = lifted('container_log')
        self.assertTrue(nodes, 'behaviour.py has no container_log')

        def docker(*args):
            self.assertEqual(args, ('logs', 'plugincheck-behaviour-1-1'))
            return types.SimpleNamespace(stdout='steamcmd done\n', stderr="cp: cannot stat '/seed/.': Input/output error\n", returncode=0)

        namespace = {'docker': docker}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), SOURCE, 'exec'), namespace)
        said = namespace['container_log']('plugincheck-behaviour-1-1')
        self.assertTrue(said.endswith("cp: cannot stat '/seed/.': Input/output error\n"), said)
        self.assertTrue(said.startswith('steamcmd done'), said)

    def test_serve_writes_it(self):
        serve = ast.unparse(lifted('serve')[0])
        self.assertIn("'container.log'), 'w', encoding='utf-8').write(container_log(container))", serve)
        self.assertNotIn("docker('logs', container).stdout", serve)


if __name__ == '__main__':
    unittest.main()
