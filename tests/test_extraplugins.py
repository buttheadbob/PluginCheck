"""Unpacking another plugin's source, without Docker or a download: python -m unittest discover -s tests"""
import io, os, shutil, sys, tempfile, unittest, zipfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'stages', 'behaviour'))
import extraplugins


class Fetch(unittest.TestCase):
    def setUp(self):
        self.top = tempfile.mkdtemp()
        self.cache = os.path.join(self.top, 'cache')

    def tearDown(self):
        shutil.rmtree(self.top)

    def fetch(self, members):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            for name in members:
                archive.writestr(name, 'x')
        with mock.patch.object(extraplugins.urllib.request, 'urlopen', return_value=io.BytesIO(data.getvalue())):
            return extraplugins.fetch('owner/name', 'abc123', self.cache)

    def test_files_are_written_under_the_cache_folder(self):
        folder = self.fetch(['name-abc123/Plugin/Plugin.cs'])
        self.assertTrue(os.path.isfile(os.path.join(folder, 'Plugin', 'Plugin.cs')))

    def test_a_member_with_an_absolute_or_drive_path_is_not_written(self):
        # An absolute path, with its drive on Windows.
        outside = os.path.join(self.top, 'outside.txt').replace(os.sep, '/')
        folder = self.fetch(['name-abc123/Plugin.cs', 'name-abc123/' + outside])
        self.assertFalse(os.path.exists(outside))
        written = [os.path.relpath(os.path.join(d, f), folder) for d, _, fs in os.walk(folder) for f in fs]
        for path in written:
            self.assertFalse(os.path.isabs(path) or path.startswith('..'), path)
        self.assertIn('Plugin.cs', written)


if __name__ == '__main__':
    unittest.main()
