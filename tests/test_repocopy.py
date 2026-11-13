"""The folders a Quasar plugin's companions add to the code stages, without Docker: python -m unittest discover -s tests"""
import json, os, shutil, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'stages'))
import repocopy

TARGET = {'quasar': {'packageManifest': 'quasar-plugin.json'}}


class CompanionFolders(unittest.TestCase):
    def setUp(self):
        self.top = tempfile.mkdtemp()
        self.repo = os.path.join(self.top, 'repo')
        for folder in ('Companion', 'Shared', os.path.join('..', 'outside')):
            os.makedirs(os.path.join(self.repo, folder), exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.top)

    def write(self, path, text):
        with open(os.path.join(self.repo, path), 'w', encoding='utf-8') as out:
            out.write(text)

    def companion(self, project='Companion/Companion.csproj', compiled=()):
        self.write('quasar-plugin.json', json.dumps({'companionPlugins': [{'id': 'c', 'projectPath': project}]}))
        items = ''.join(f'<Compile Include="{c}" />' for c in compiled)
        self.write(project, f'<Project><ItemGroup>{items}</ItemGroup></Project>')

    def test_a_compiled_in_folder_inside_the_repository_is_added(self):
        self.companion(compiled=[r'..\Shared\*.cs'])
        self.assertEqual(repocopy.companion_folders(self.repo, TARGET), ['Companion', 'Shared'])

    def test_a_compiled_in_folder_outside_the_repository_is_left_out(self):
        self.companion(compiled=[r'..\..\outside\*.cs'])
        self.assertEqual(repocopy.companion_folders(self.repo, TARGET), ['Companion'])

    def test_a_companion_project_outside_the_repository_is_left_out(self):
        self.write('quasar-plugin.json', json.dumps({'companionPlugins': [{'id': 'c', 'projectPath': '../outside/Outside.csproj'}]}))
        with open(os.path.join(self.top, 'outside', 'Outside.csproj'), 'w', encoding='utf-8') as out:
            out.write('<Project />')
        self.assertEqual(repocopy.companion_folders(self.repo, TARGET), [])

    def test_a_project_that_cannot_be_read_adds_only_its_own_folder(self):
        self.write('quasar-plugin.json', json.dumps({'companionPlugins': [{'id': 'c', 'projectPath': 'Companion/Companion.csproj'}]}))
        self.write('Companion/Companion.csproj', '<Project><ItemGroup>')
        self.assertEqual(repocopy.companion_folders(self.repo, TARGET), ['Companion'])


if __name__ == '__main__':
    unittest.main()
