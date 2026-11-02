import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from store import Store
import startup_memory


class StartupMemoryTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.close)
        self.store.add_message('legacy-session', 'user', 'private memory')

    def test_success_is_durable_across_store_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory)/'state.db')
            with closing(Store(path)) as store:
                store.add_message('legacy-session', 'user', 'private memory')
                self.assertTrue(startup_memory.migrate_once(store, 'account', lambda: True))
            with closing(Store(path)) as store:
                migrate = Mock(return_value=True)
                self.assertFalse(startup_memory.migrate_once(store, 'account', migrate))
                migrate.assert_not_called()

    def test_markers_are_isolated_by_account_and_version(self):
        migrate = Mock(return_value=True)
        self.assertTrue(startup_memory.migrate_once(self.store, 'first', migrate))
        self.assertFalse(startup_memory.migrate_once(self.store, 'first', migrate))
        self.assertTrue(startup_memory.migrate_once(self.store, 'second', migrate))
        with patch.object(startup_memory, 'MIGRATION_VERSION', startup_memory.MIGRATION_VERSION + 1):
            self.assertTrue(startup_memory.migrate_once(self.store, 'first', migrate))
        self.assertFalse(startup_memory.migrate_once(self.store, 'first', migrate))
        self.assertEqual(migrate.call_count, 3)

    def test_unmatched_but_completed_scan_is_cached(self):
        migrate = Mock(return_value=True)
        self.assertTrue(startup_memory.migrate_once(self.store, 'account', migrate))
        self.assertFalse(startup_memory.migrate_once(self.store, 'account', migrate))
        self.assertEqual(migrate.call_count, 1)

    def test_incomplete_or_interrupted_scan_never_marks_completed(self):
        for outcome in (False, None, 1):
            with self.subTest(outcome=outcome):
                self.assertFalse(startup_memory.migrate_once(self.store, 'account', lambda: outcome))
        self.assertTrue(startup_memory.migrate_once(self.store, 'account', lambda: True))

    def test_exception_or_stop_retries_instead_of_recording_completion(self):
        for error in (RuntimeError('scan failed'), InterruptedError('stopping'), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(type(error)):
                    startup_memory.migrate_once(self.store, 'account', Mock(side_effect=error))
        self.assertTrue(startup_memory.migrate_once(self.store, 'account', lambda: True))

    def test_no_conversation_messages_skips_scan_and_requires_version_bump_for_imports(self):
        self.store._write('DELETE FROM messages')
        self.store.add_message('__self__', 'assistant', 'private self memory')
        migrate = Mock(return_value=True)
        self.assertFalse(startup_memory.migrate_once(self.store, 'account', migrate))
        self.store.add_message('imported-session', 'user', 'later imported memory')
        self.assertFalse(startup_memory.migrate_once(self.store, 'account', migrate))
        migrate.assert_not_called()
        with patch.object(startup_memory, 'MIGRATION_VERSION', startup_memory.MIGRATION_VERSION + 1):
            self.assertTrue(startup_memory.migrate_once(self.store, 'account', migrate))

    def test_scan_can_use_store_without_locking_itself_out(self):
        def migrate():
            self.store.add_message('legacy-session', 'assistant', 'migrated')
            return True
        self.assertTrue(startup_memory.migrate_once(self.store, 'account', migrate))
        self.assertEqual(len(self.store.recent('legacy-session')), 2)

    def test_invalid_account_never_runs_scan(self):
        for account in ('', None, 123, 'x'*257):
            with self.subTest(account_type=type(account).__name__):
                migrate = Mock(return_value=True)
                with self.assertRaises(ValueError):
                    startup_memory.migrate_once(self.store, account, migrate)
                migrate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
