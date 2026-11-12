import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from supplytree.parser import inspect
from supplytree.journal import Journal
from supplytree.transport import capture
from scripts.materialize import main as materialize
from scripts.source_server import running


class BaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        materialize()

    def test_capture_journal_is_content_addressed(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory)
            try:
                first = journal.save(b'synthetic', 'source-1')
                second = journal.save(b'synthetic', 'source-1')
                self.assertEqual(first, second)
                self.assertEqual(Path(first['path']).read_bytes(), b'synthetic')
                self.assertEqual(journal.database.execute('SELECT COUNT(*) FROM captures').fetchone()[0], 1)
            finally:
                journal.close()

    def test_remote_transport_is_rejected_before_io(self):
        with self.assertRaises(ValueError):
            capture('https://example.invalid/private')

    def test_cli_help(self):
        result = subprocess.run([sys.executable, '-m', 'supplytree', '--help'], capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn(b'trees', result.stdout)

    def test_tree_pins_commit_and_preserves_newline_path(self):
        from scripts.git_source import generate
        with tempfile.TemporaryDirectory() as directory:
            source = generate(Path(directory) / 'source')
            result = inspect(source['repository'], source['initial'])
            self.assertEqual(result['commit'], source['initial'])
            self.assertTrue(any('\n' in item['path'] for item in result['entries']))
            moved = inspect(source['repository'])
            self.assertNotEqual(moved['commit'], source['initial'])
            self.assertEqual(len(moved['entries']), len(result['entries']))

    def test_moved_blob_keeps_oid(self):
        from scripts.git_source import generate
        with tempfile.TemporaryDirectory() as directory:
            source = generate(Path(directory) / 'source')
            before = {item['oid'] for item in inspect(source['repository'], source['initial'])['entries']}
            after = {item['oid'] for item in inspect(source['repository'], source['moved'])['entries']}
            self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
