"""Publication rejects stale evidence, corrupt assets and unrelated source bytes."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import prepare_release_notes as notes
from verify_release_assets import REQUIRED_CASES, verify


class ReleaseAssetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo, self.assets = self.root / 'repo', self.root / 'assets'
        self.repo.mkdir(); self.assets.mkdir()
        self.git('init', '-q', '--initial-branch=main')
        self.git('config', 'user.name', 'Release test')
        self.git('config', 'user.email', 'release-test@example.invalid')
        self.git('config', 'core.autocrlf', 'false')
        self.files = {'.gitattributes': b'*.py text eol=lf\n*.txt text\n*.pdf binary\n',
                      'app/lcsc3d/ui/main.py': b"VERSION = '2.4.4'\n",
                      'docs/example.txt': b'first\nsecond\n',
                      'app/tests/sample.pdf': b'%PDF-1.4\noriginal\n%%EOF\n'}
        for name, value in self.files.items():
            target = self.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(value)
        self.git('add', '.')
        self.git('-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=' + str(self.root / 'no-hooks'),
                 'commit', '-qm', 'Fixture')
        self.commit = self.git('rev-parse', 'HEAD').decode().strip()
        (self.assets / 'LCSC3D.exe').write_bytes(b'MZ' + b'fixture executable')
        self.package()
        self.evidence = dict(success=True, errors=[], version='2.4.4', source_commit=self.commit,
                             cases=[dict(name=name) for name in sorted(REQUIRED_CASES)])
        self.pdf = dict.fromkeys(('pdf_only', 'missing_report_exact', 'missing_notice', 'mixed_exports',
                                 'cancel_restores_pdf_and_report', 'cancel_progress_zero',
                                 'retry_and_stale_report_cleanup', 'diagnostics_verified'), True)
        self.pdf.update(success=True, frozen=True, version='2.4.4')
        self.progress = dict(success=True, frozen=True, version='2.4.4', cancel_progress_animated_to_zero=True,
                             cancel_removes_new_and_restores_previous=True, retry_after_cancel=True,
                             logs_packaged_and_writable_after_clear=True, cancel_progress_values=[6000, 3000, 900, 0])
        self.write_evidence()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.PIPE)

    def package(self, files=None):
        with zipfile.ZipFile(self.assets / 'LCSC3D.zip', 'w') as archive:
            for name, value in (self.files if files is None else files).items():
                archive.writestr('LCSC3D/' + name, value)
        lines = []
        for name in ('LCSC3D.exe', 'LCSC3D.zip'):
            lines.append(hashlib.sha256((self.assets / name).read_bytes()).hexdigest() + '  ' + name)
        (self.assets / 'SHA256SUMS.txt').write_text('\n'.join(lines) + '\n', 'ascii')

    def write_evidence(self):
        for name, value in (('release-verification', self.evidence), ('pdf-verification', self.pdf),
                            ('progress-verification', self.progress)):
            (self.assets / (name + '.json')).write_text(json.dumps(value), 'utf-8')

    def verify(self):
        return verify(self.assets, '2.4.4', self.commit, repository=self.repo)

    def test_complete_verified_assets_accept_git_text_line_endings(self):
        self.package({**self.files, 'docs/example.txt': b'first\r\nsecond\r\n'})
        result = self.verify()
        self.assertTrue(result['success'])
        self.assertEqual(result['source_files'], len(self.files))

    def test_binary_pdf_line_endings_must_remain_exact(self):
        self.package({**self.files, 'app/tests/sample.pdf': self.files['app/tests/sample.pdf'].replace(b'\n', b'\r\n')})
        with self.assertRaisesRegex(ValueError, 'Source differs'):
            self.verify()

    def test_tampered_executable_and_duplicate_manifest_are_rejected(self):
        (self.assets / 'LCSC3D.exe').write_bytes(b'MZchanged')
        with self.assertRaisesRegex(ValueError, 'Checksum mismatch'):
            self.verify()
        self.package()
        path = self.assets / 'SHA256SUMS.txt'
        content = path.read_text('ascii')
        path.write_text(content + content.splitlines()[0] + '\n', 'ascii')
        with self.assertRaisesRegex(ValueError, 'Duplicate checksum'):
            self.verify()

    def test_wrong_commit_or_version_cannot_reuse_an_old_verification(self):
        for field, value in (('source_commit', '0' * 40), ('version', '2.4.3')):
            with self.subTest(field=field):
                original = self.evidence[field]
                self.evidence[field] = value
                self.write_evidence()
                with self.assertRaisesRegex(ValueError, 'does not match'):
                    self.verify()
                self.evidence[field] = original

    def test_failed_or_incomplete_native_acceptance_is_rejected(self):
        self.evidence['cases'].pop()
        self.write_evidence()
        with self.assertRaisesRegex(ValueError, 'Missing acceptance'):
            self.verify()
        self.evidence['cases'] = [dict(name=name) for name in REQUIRED_CASES]
        self.pdf['missing_report_exact'] = False
        self.write_evidence()
        with self.assertRaisesRegex(ValueError, 'Incomplete frozen'):
            self.verify()

    def test_jump_to_zero_or_rebounding_progress_is_not_an_animation_proof(self):
        for values in ([6000, 0], [6000, 3000, 4500, 0], [6000, 3000, 900, 100]):
            with self.subTest(values=values):
                self.progress['cancel_progress_values'] = values
                self.write_evidence()
                with self.assertRaisesRegex(ValueError, 'animation was not verified'):
                    self.verify()

    def test_missing_extra_and_modified_source_files_are_rejected_even_with_valid_hashes(self):
        for files in ({key: value for key, value in self.files.items() if key != 'docs/example.txt'},
                      {**self.files, 'secret.txt': b'unrelated'},
                      {**self.files, 'app/lcsc3d/ui/main.py': b"VERSION = '9.9.9'\n"}):
            with self.subTest(names=sorted(files)):
                self.package(files)
                with self.assertRaises(ValueError):
                    self.verify()

    def test_tag_message_keeps_exact_title_and_complete_cause_fix_and_pdf_text(self):
        content = '# LCSC3D 2.4.4\n\n## Cause and fix\n- Deferred UI initialization.\n\n## PDF downloads\n- Missing model/brand report.\n'
        self.assertEqual(notes.tag_message(content, 'v2.4.4'), content.removeprefix('# '))
        with self.assertRaises(ValueError):
            notes.tag_message(content, 'v2.4.3')
        source = self.repo / 'notes.md'
        source.write_text(content + '\n![screenshot](screenshot.png)\n', 'utf-8')
        with patch.object(notes, 'ROOT', self.repo), self.assertRaisesRegex(ValueError, 'text-only'):
            notes.prepare(source, 'v2.4.4')
