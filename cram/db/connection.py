import sqlite3
from pathlib import Path
from typing import Union
from contextlib import contextmanager

def get_connection(db_path: Union[str, Path], timeout: float = 30.0) -> sqlite3.Connection:
    """
    Creates and returns a SQLite connection configured with:
    - WAL journal mode for high concurrency
    - Foreign keys enforced
    - Row factory enabled for dictionary-like access
    - Busy timeout configured to handle concurrent transactions
    """
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    return conn

@contextmanager
def get_db(db_path: Union[str, Path]):
    """Context manager for SQLite connections with automatic commit/rollback."""
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
