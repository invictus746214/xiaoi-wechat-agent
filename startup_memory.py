"""Persist completed legacy-memory scans; bump the version after legacy imports."""

MIGRATION_VERSION = 1


def migrate_once(store, account, migrate_callable):
    """Run in the UI executor; return True only for a newly completed scan.

    The callback must return exactly True after completing its scan. False/None,
    interruptions and exceptions leave it eligible for retry. Empty memory needs
    no UI scan and receives the same durable completion marker.
    """
    if not isinstance(account, str) or not 1 <= len(account) <= 256:
        raise ValueError('startup_memory_account_required')
    key = (account, MIGRATION_VERSION)
    store._write('CREATE TABLE IF NOT EXISTS startup_memory_migrations('
                 'account TEXT NOT NULL,version INTEGER NOT NULL,PRIMARY KEY(account,version))')
    if store._rows('SELECT 1 FROM startup_memory_migrations WHERE account=? AND version=?', key):
        return False
    has_memory = bool(store._rows("SELECT 1 FROM messages WHERE session_key!='__self__' LIMIT 1"))
    if has_memory and migrate_callable() is not True:
        return False
    store._write('INSERT OR IGNORE INTO startup_memory_migrations(account,version) VALUES(?,?)', key)
    return has_memory
