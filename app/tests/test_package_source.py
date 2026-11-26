import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = Path('scripts/package_source.py')
sys.path.insert(0, str(ROOT / 'scripts'))
from verify_release_assets import verify_source_zip


def git_environment(root):
    # Apply the same Git config to subprocesses launched inside packaging and verification.
    return dict(os.environ, GIT_CEILING_DIRECTORIES=str(root.parent),
                GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='core.autocrlf', GIT_CONFIG_VALUE_0='false')


class PackageSourceTests(unittest.TestCase):
    def temporary_directory(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name).resolve()

    def copy_script(self):
        root = self.temporary_directory()
        (root / SCRIPT).parent.mkdir()
        shutil.copyfile(ROOT / SCRIPT, root / SCRIPT)
        return root

    def git(self, root, *args):
        return subprocess.check_output(['git', '-c', 'core.autocrlf=false', '-C', str(root), *args],
                                       env=git_environment(root))

    def create_repository(self):
        root = self.copy_script()
        self.git(root, 'init', '-q')
        self.git(root, 'config', 'user.name', 'Package source test')
        self.git(root, 'config', 'user.email', 'package-test@example.invalid')
        files = {'.gitignore': b'ignored.tmp\n', 'app/README.md': b'# Guide\n',
                 'new-root-config.toml': b'enabled = true\n', 'nested/新 文件.bin': b'\x00\xff\r\n'}
        for name, data in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (root / 'untracked.txt').touch()
        (root / 'ignored.tmp').touch()
        self.git(root, 'add', '--', SCRIPT.as_posix(), *files)
        self.git(root, '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=' + str(root / 'no-hooks'),
                 'commit', '-qm', 'Source ZIP fixture')
        return root

    def package(self, root, output):
        return subprocess.run([sys.executable, str(root / SCRIPT), '--output-dir', str(output)],
                              cwd=root, env=git_environment(root), capture_output=True)

    def package_archive(self, root, output):
        result = self.package(root, output)
        self.assertEqual(result.returncode, 0, result.stderr)
        return output / 'LCSC3D.zip'

    def assert_tracked_names(self, root, names):
        paths = self.git(root, 'ls-files', '-z').decode('utf-8').split('\0')
        expected = {'LCSC3D/' + path for path in paths if path}
        self.assertEqual(set(names), expected)
        self.assertEqual(names, sorted(expected))

    def verify_archive(self, root, path):
        commit = self.git(root, 'rev-parse', 'HEAD').decode().strip()
        with patch.dict(os.environ, git_environment(root)):
            return verify_source_zip(path, commit, repository=root)

    def test_source_zip_matches_tracked_files(self):
        path = self.package_archive(ROOT, self.temporary_directory())
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            self.assert_tracked_names(ROOT, names)
            self.assertIn('LCSC3D/.mcp.json', names)

    def test_new_root_file_is_packaged(self):
        root = self.create_repository()
        path = self.package_archive(root, root / 'work')
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            self.assert_tracked_names(root, names)
            self.assertIn('LCSC3D/new-root-config.toml', names)
            self.assertNotIn('LCSC3D/untracked.txt', names)
            self.assertNotIn('LCSC3D/ignored.tmp', names)
            for name in names:
                self.assertEqual(archive.read(name), (root / name.removeprefix('LCSC3D/')).read_bytes(), name)
        self.assertEqual(self.verify_archive(root, path), len(names))

    def test_missing_tracked_file_fails_strict_check(self):
        root = self.create_repository()
        path = self.package_archive(root, root / 'work')
        with zipfile.ZipFile(path) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        del files['LCSC3D/new-root-config.toml']
        with zipfile.ZipFile(path, 'w') as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        with self.assertRaises(ValueError) as error:
            self.verify_archive(root, path)
        self.assertEqual(str(error.exception), 'Source ZIP does not contain every tracked file')

    def test_non_git_directory_fails_clearly(self):
        root = self.copy_script()
        result = self.package(root, root / 'work')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'git ls-files', result.stderr)
