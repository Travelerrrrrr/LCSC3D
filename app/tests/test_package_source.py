from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile


class PackageSourceTests(unittest.TestCase):
    def test_source_zip_contains_mcp_config(self):
        source = Path(__file__).resolve().parents[2] / 'scripts' / 'package_source.py'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / 'scripts' / source.name
            script.parent.mkdir()
            shutil.copyfile(source, script)
            (root / 'app').mkdir()
            for filename in ('README.md', 'README.en.md', 'LICENSE', 'CONTRIBUTING.md',
                             'AGENTS.md', 'GLOSSARY.md', '.gitignore', '.gitattributes',
                             'app/README.md', '.mcp.json'):
                (root / filename).touch()

            subprocess.run([sys.executable, str(script)], cwd=root, check=True)

            with zipfile.ZipFile(root / 'outputs' / 'LCSC3D.zip') as archive:
                self.assertIn('LCSC3D/.mcp.json', archive.namelist())
