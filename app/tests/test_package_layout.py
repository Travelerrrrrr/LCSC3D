"""Guard namespace imports and source resources after application reorganization."""
import ast
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP))
from lcsc3d.core.assets import SOURCE_APP_DIR, RESOURCE_ROOT
from lcsc3d.core.app_paths import temporary_directory


class PackageLayoutTests(unittest.TestCase):
    def test_root_contains_only_bootstrap_python_files_and_packages_use_explicit_imports(self):
        self.assertEqual({p.name for p in APP.glob('*.py')},{'main.py','launcher.py'})
        package=APP/'lcsc3d'
        modules={p.stem for p in package.rglob('*.py') if p.name!='__init__.py'}
        for source in package.rglob('*.py'):
            for node in ast.walk(ast.parse(source.read_text('utf-8'))):
                if isinstance(node,ast.ImportFrom):
                    self.assertNotIn(node.module,modules,(source,node.module))
                elif isinstance(node,ast.Import):
                    for alias in node.names:self.assertNotIn(alias.name,modules,(source,alias.name))

    def test_source_resources_resolve_from_any_working_directory(self):
        self.assertEqual(SOURCE_APP_DIR,APP)
        self.assertEqual(RESOURCE_ROOT,APP)
        for path in ('viewer.html','vector_viewer.html','assets/material-overrides.qss',
                     'assets/app.ico','licenses/THIRD-PARTY.md'):
            self.assertTrue((RESOURCE_ROOT/path).is_file(),path)
        from lcsc3d.ui.settings_ui import SPONSORSHIP_DIRECTORY
        self.assertTrue((SPONSORSHIP_DIRECTORY/'alipay.png').is_file())
        self.assertTrue((SPONSORSHIP_DIRECTORY/'wechat.png').is_file())

    def test_source_entry_starts_outside_repository_without_flat_module_search_paths(self):
        with tempfile.TemporaryDirectory(dir=temporary_directory()) as folder:
            result=subprocess.run([sys.executable,'-B',str(APP/'main.py'),'--help'],cwd=folder,
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf-8','replace'))
        self.assertIn(b'usage:',result.stdout)
