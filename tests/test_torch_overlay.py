"""A Torch variant with an overlay runs a zip built from the working source with the overlay laid over it.
Run with: python -I -m unittest discover -s tests"""
import ast, os, shutil, sys, tempfile, types, unittest, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
STAGES = os.path.normpath(os.path.join(HERE, '..', 'stages'))
for path in (os.path.join(STAGES, 'behaviour'), STAGES):
    if path not in sys.path:
        sys.path.insert(0, path)
import classification, extraplugins

SOURCE = os.path.join(STAGES, 'behaviour.py')
CSPROJ = '''<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><AssemblyName>Demo Plugin</AssemblyName></PropertyGroup>
  <ItemGroup><Reference Include="Torch"><HintPath>..\\TorchBinaries\\Torch.dll</HintPath></Reference></ItemGroup>
</Project>
'''
MANIFEST = '<?xml version="1.0"?><PluginManifest><Name>Demo Plugin</Name><Guid>11111111-2222-3333-4444-555555555555</Guid></PluginManifest>'


class VariantTests(unittest.TestCase):
    def builds(self):
        return [{'name': 'working-magnetar', 'host': 'magnetar', 'published': False, 'source': 'src'},
                {'name': 'working-torch', 'host': 'torch', 'published': False, 'zip': 'plugin.zip'},
                {'name': 'published-torch', 'host': 'torch', 'published': True, 'zip': 'old.zip'}]

    def test_a_torch_variant_with_an_overlay_is_kept_for_its_own_zip(self):
        options = {'variants': [{'name': 'own', 'hosts': ['magnetar', 'torch'], 'overlay': 'over', 'scenarios': ['=!demo info']}]}
        made = extraplugins.variant_builds(self.builds(), options, 'work', lambda base, overlay, name: f'{name}-copy')
        by = {b['name']: b for b in made}
        self.assertEqual(sorted(by), ['working-magnetar~own', 'working-torch~own'])
        self.assertEqual(by['working-torch~own']['overlay'], 'over')
        self.assertNotIn('source', by['working-torch~own'])
        self.assertEqual(by['working-magnetar~own']['source'], 'working-magnetar~own-copy')
        self.assertNotIn('overlay', by['working-magnetar~own'])

    def test_a_magnetar_build_with_no_source_is_still_left_out(self):
        options = {'variants': [{'name': 'own', 'hosts': ['magnetar'], 'overlay': 'over'}]}
        builds = [{'name': 'working-magnetar', 'host': 'magnetar', 'published': False}]
        self.assertEqual(extraplugins.variant_builds(builds, options, 'work', lambda *a: 'x'), [])


class TorchZipTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix='pc-torchzip-')
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.source = os.path.join(self.folder, 'source')
        os.makedirs(os.path.join(self.source, 'TorchPlugin'))
        open(os.path.join(self.source, 'TorchPlugin', 'Demo.csproj'), 'w').write(CSPROJ)
        open(os.path.join(self.source, 'TorchPlugin', 'manifest.xml'), 'w').write(MANIFEST)
        open(os.path.join(self.source, 'TorchPlugin', 'Plugin.cs'), 'w').write('class Plugin { }')
        self.calls = []

    def docker(self, *args):
        self.calls.append(args)
        build = next(a[:-len(':/work/build')] for a in args if isinstance(a, str) and a.endswith(':/work/build'))
        os.makedirs(os.path.join(build, 'out'), exist_ok=True)
        for dll in ('Demo Plugin.dll', '0Harmony.dll'):
            open(os.path.join(build, 'out', dll), 'wb').write(b'MZ')
        open(os.path.join(build, 'build.log'), 'w').write('')
        return types.SimpleNamespace(returncode=0, stdout='', stderr='')

    def test_the_zip_holds_the_plugin_its_packages_and_its_manifest(self):
        zip_path, guid, name = extraplugins.torch_zip(self.source, 'TorchPlugin', os.path.join(self.folder, 'out'), self.docker, 'sdk', 'libs', 'ds')
        self.assertEqual((guid, name), ('11111111-2222-3333-4444-555555555555', 'Demo Plugin'))
        self.assertEqual(sorted(zipfile.ZipFile(zip_path).namelist()), ['0Harmony.dll', 'Demo Plugin.dll', 'manifest.xml'])
        self.assertEqual(len(self.calls), 1)

    def test_it_is_built_once(self):
        out = os.path.join(self.folder, 'out')
        first = extraplugins.torch_zip(self.source, 'TorchPlugin', out, self.docker, 'sdk', 'libs', 'ds')
        self.assertEqual(extraplugins.torch_zip(self.source, 'TorchPlugin', out, self.docker, 'sdk', 'libs', 'ds'), first)
        self.assertEqual(len(self.calls), 1)

    def test_a_failed_build_says_why(self):
        def failing(*args):
            build = next(a[:-len(':/work/build')] for a in args if isinstance(a, str) and a.endswith(':/work/build'))
            open(os.path.join(build, 'build.log'), 'w').write('Plugin.cs(1,1): error CS1002: ; expected\n')
            return types.SimpleNamespace(returncode=1, stdout='', stderr='')
        with self.assertRaisesRegex(RuntimeError, 'error CS1002'):
            extraplugins.torch_zip(self.source, 'TorchPlugin', os.path.join(self.folder, 'out'), failing, 'sdk', 'libs', 'ds')


class ProjectTests(unittest.TestCase):
    """The generated project for a Torch plugin's folder."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix='pc-torchproj-')
        self.addCleanup(shutil.rmtree, self.folder, True)
        plugin = os.path.join(self.folder, 'source', 'TorchPlugin')
        os.makedirs(os.path.join(plugin, 'UI'))
        open(os.path.join(plugin, 'Demo.csproj'), 'w').write('''<Project>
  <ItemGroup>
    <Reference Include="Sandbox.Game"><HintPath>..\\GameBinaries\\Sandbox.Game.dll</HintPath></Reference>
    <Reference Include="Torch"><HintPath>..\\TorchBinaries\\Torch.dll</HintPath></Reference>
  </ItemGroup>
</Project>
''')
        open(os.path.join(plugin, 'UI', 'Page.xaml'), 'w').write('<?xml version="1.0"?>\n<UserControl x:Class="Demo.UI.Page" '
                                                                 'xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"><Button x:Name="Go"/></UserControl>')
        self.libs = os.path.join(self.folder, 'libs')
        os.makedirs(self.libs)
        open(os.path.join(self.libs, 'Torch.dll'), 'wb').write(b'MZ')

    def project(self, torch_libs):
        out = os.path.join(self.folder, 'out')
        extraplugins.torch_project(os.path.join(self.folder, 'source'), 'TorchPlugin', out, torch_libs)
        return open(os.path.join(out, 'Extra.csproj')).read(), open(os.path.join(out, 'Pages.g.cs')).read()

    def test_a_reference_goes_where_its_file_is(self):
        """A game assembly referenced from a folder of another name (here GameBinaries) is found in the game's folder, and one Torch has
        is taken from Torch's."""
        project, _ = self.project(self.libs)
        self.assertIn('<HintPath>/ds/DedicatedServer64/Sandbox.Game.dll</HintPath>', project)
        self.assertIn('<HintPath>/work/torch/Torch.dll</HintPath>', project)

    def test_a_page_stand_in_has_its_root_elements_base_type(self):
        """Code-behind often leaves the base type to the XAML compiler, so the stand-in names it, or members such as DataContext are
        missing."""
        _, pages = self.project(self.libs)
        self.assertIn('partial class Page : System.Windows.Controls.UserControl', pages)
        self.assertIn('internal System.Windows.Controls.Button Go = null;', pages)


class BehaviourTests(unittest.TestCase):
    def test_a_built_zip_is_keyed_by_its_source(self):
        """A zip built in the run differs byte for byte each time, so the overlaid source decides whether a kept result still fits."""
        tree = ast.parse(open(SOURCE, encoding='utf-8').read())
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'base_hash']
        namespace = {'classification': classification, 'hashlib': __import__('hashlib')}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), SOURCE, 'exec'), namespace)
        folder = tempfile.mkdtemp(prefix='pc-torchzip-')
        self.addCleanup(shutil.rmtree, folder, True)
        open(os.path.join(folder, 'a.cs'), 'w').write('x')
        build = {'published': False, 'overlay_source': folder, 'zip': os.path.join(folder, 'missing.zip')}
        self.assertEqual(namespace['base_hash'](build), classification.folder_hash(folder))

    def test_the_zip_is_built_while_the_torch_libraries_are_there(self):
        source = open(SOURCE, encoding='utf-8').read()
        built = source.index("build['zip'] = extraplugins.torch_zip(build['overlay_source']")
        self.assertLess(source.index('probe_zip, problem = build_torch_probe()'), built)
        self.assertLess(built, source.index("shutil.rmtree(os.path.join(work, 'torch-libs'), ignore_errors=True)"))
        self.assertLess(built, source.index('fingerprints = {b[\'name\']: build_hash(b) for b in builds}'))


if __name__ == '__main__':
    unittest.main()
