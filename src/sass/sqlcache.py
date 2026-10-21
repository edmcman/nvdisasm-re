"""Sqlite result caches that parallel workers fill together."""
import fcntl, sqlite3


def open_cache(path, table, lock, timeout, prepare=None):
    """WAL keeps readers from blocking the writer and the timeout serializes writers. Switching a new
    database to WAL ignores the timeout (SQLITE_BUSY at once), so workers opening it together set it
    up one at a time under `lock`, after `prepare()`."""
    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        if prepare: prepare()
        db = sqlite3.connect(path, timeout=timeout)
        db.execute("pragma journal_mode=wal")
        db.execute(f"create table if not exists {table}")
    return db
