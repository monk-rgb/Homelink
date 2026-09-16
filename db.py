"""Database access that works with either SQLite (local dev) or PostgreSQL (hosting).

The rest of the application was written against the sqlite3 API: ``?`` placeholders,
dict-like rows, ``cursor.lastrowid`` and ``BEGIN IMMEDIATE``. Rather than rewrite
every call site, this module exposes the *same* API on top of both engines, so
moving to a hosted Postgres is a configuration change, not a code change.

Set ``DATABASE_URL`` to a Postgres connection string (Neon, Supabase, Render...) to
use Postgres. Leave it blank and the app keeps using the local SQLite file.

The translation is intentionally small and predictable:

* ``?`` placeholders become ``%s``.
* ``INSERT OR IGNORE`` becomes ``INSERT ... ON CONFLICT DO NOTHING``.
* ``BEGIN IMMEDIATE`` becomes ``BEGIN`` (Postgres takes row locks when rows are
  read ``FOR UPDATE``; see ``execute`` below).
* ``INTEGER PRIMARY KEY AUTOINCREMENT`` becomes ``BIGSERIAL PRIMARY KEY``.
* ``CURRENT_TIMESTAMP`` defaults still work: they are stored as UTC text so that
  the existing timestamp parsing keeps working unchanged.
* ``cursor.lastrowid`` is reconstructed with ``RETURNING id``.

Only the SQL actually used by this app is translated - it is not a general
SQLite-to-Postgres converter.
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any

# A psycopg driver is only required when Postgres is actually configured, so the
# local SQLite workflow keeps working without installing it.
_PG_DRIVER_ERROR = None
try:  # pragma: no cover - import availability depends on the environment
    import psycopg  # psycopg 3
    from psycopg.rows import dict_row as _pg_dict_row

    _PG_DRIVER = 'psycopg3'
except Exception:  # pragma: no cover
    try:
        import psycopg2  # psycopg 2 fallback
        from psycopg2.extras import RealDictCursor as _pg_dict_row

        psycopg = None
        _PG_DRIVER = 'psycopg2'
    except Exception as exc:  # pragma: no cover
        _PG_DRIVER = None
        _PG_DRIVER_ERROR = exc


def database_url() -> str:
    """Return the configured Postgres URL, or '' when using SQLite."""
    return (os.getenv('DATABASE_URL') or '').strip()


def using_postgres() -> bool:
    return bool(database_url())


def require_driver() -> None:
    """Raise a clear error when Postgres is requested but no driver is installed."""
    if using_postgres() and _PG_DRIVER is None:
        raise RuntimeError(
            'DATABASE_URL is set but no PostgreSQL driver is installed. '
            'Add "psycopg[binary]" (or psycopg2-binary) to requirements.txt. '
            f'Original import error: {_PG_DRIVER_ERROR!r}'
        )


# --- SQL translation -------------------------------------------------------

_AUTOINCREMENT = re.compile(
    r'INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT', re.IGNORECASE)
_INSERT_OR_IGNORE = re.compile(r'INSERT\s+OR\s+IGNORE\s+INTO', re.IGNORECASE)
_INSERT_OR_REPLACE = re.compile(r'INSERT\s+OR\s+REPLACE\s+INTO', re.IGNORECASE)
_BEGIN_IMMEDIATE = re.compile(r'^\s*BEGIN\s+IMMEDIATE\s*$', re.IGNORECASE)
_SQLITE_MASTER = re.compile(r'\bsqlite_master\b', re.IGNORECASE)
_RETURNING_ID = re.compile(r'\bRETURNING\b', re.IGNORECASE)
_INSERT_STMT = re.compile(r'^\s*(INSERT|REPLACE)\b', re.IGNORECASE)
# Only these statements can produce a row id we care about.
_NEEDS_ROWID = _INSERT_STMT
# PRAGMA/ATTACH have no Postgres equivalent and are only used to introspect
# SQLite; on Postgres they become harmless no-ops.
_PRAGMA = re.compile(r'^\s*PRAGMA\b', re.IGNORECASE)


def translate_sql(sql: str) -> str:
    """Rewrite SQLite SQL into Postgres SQL. Returns the input unchanged for SQLite."""
    if not using_postgres():
        return sql
    out = sql
    out = _AUTOINCREMENT.sub('BIGSERIAL PRIMARY KEY', out)
    out = _INSERT_OR_IGNORE.sub('INSERT INTO', out)
    if _INSERT_OR_REPLACE.search(out):
        # SQLite's INSERT OR REPLACE maps onto the modern upsert form; the app only
        # uses it for single-column conflict keys, which this covers.
        out = _INSERT_OR_REPLACE.sub('INSERT INTO', out)
        if 'ON CONFLICT' not in out.upper():
            out = out.rstrip().rstrip(';') + ' ON CONFLICT DO NOTHING'
    # A straight "?" -> "%s" swap is safe here: the app never stores a literal
    # question mark inside a SQL string, and placeholder characters are not used
    # in the translated DDL.
    out = out.replace('?', '%s')
    if _BEGIN_IMMEDIATE.match(out):
        return 'BEGIN'
    return out


def _needs_returning(sql: str) -> bool:
    """True when the statement should report the id of the row it inserted."""
    if not using_postgres():
        return False
    stripped = sql.strip()
    if not _NEEDS_ROWID.match(stripped):
        return False
    if _RETURNING_ID.search(stripped) or 'ON CONFLICT DO NOTHING' in stripped.upper():
        return False
    return 'SELECT' not in stripped.upper()


# --- Row objects -----------------------------------------------------------

class Row:
    """Dict-like row supporting both ``row['name']`` and ``row[0]`` access.

    sqlite3.Row allows integer indexing and mapping access; keeping both means the
    existing templates and view code do not need to change.
    """

    __slots__ = ('_keys', '_values', '_map')

    def __init__(self, keys, values):
        self._keys = tuple(keys)
        self._values = tuple(values)
        self._map = dict(zip(self._keys, self._values))

    def keys(self):
        return list(self._keys)

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        return self._map[key]

    def __contains__(self, key):
        return key in self._map

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def get(self, key, default=None):
        return self._map.get(key, default)

    def items(self):
        return self._map.items()

    def values(self):
        return self._map.values()

    def __repr__(self):  # pragma: no cover - debugging aid
        return f'Row({self._map!r})'


# --- Cursors ---------------------------------------------------------------

class Cursor:
    """Cursor wrapper normalising both engines' cursors to the sqlite3 behaviour."""

    def __init__(self, cursor, postgres: bool):
        self._cursor = cursor
        self._postgres = postgres
        self._lastrowid: Any = None
        self.rowcount = -1

    def _maybe_returning(self, sql: str) -> str:
        translated = translate_sql(sql)
        if self._postgres and _needs_returning(translated):
            translated = translated.rstrip().rstrip(';') + ' RETURNING id'
        return translated

    def _run(self, sql: str, params=()):
        translated = self._maybe_returning(sql)
        self._cursor.execute(translated, tuple(params or ()))
        return translated

    def execute(self, sql: str, params=()):
        if self._postgres and _PRAGMA.match(sql):
            # PRAGMA is SQLite-only introspection; treat it as an empty result set.
            self.rowcount = 0
            self._lastrowid = None
            return self
        translated = self._run(sql, params)
        self.rowcount = self._cursor.rowcount
        if self._postgres and _needs_returning(translated):
            try:
                row = self._cursor.fetchone()
            except Exception:
                row = None
            if row is not None:
                self._lastrowid = row[0] if not hasattr(row, 'get') else row.get('id')
        else:
            # sqlite3 exposes lastrowid on the driver cursor itself.
            self._lastrowid = getattr(self._cursor, 'lastrowid', None)
        return self

    def executemany(self, sql: str, seq_of_params):
        translated = translate_sql(sql)
        self._cursor.executemany(translated, [tuple(p) for p in seq_of_params])
        self.rowcount = self._cursor.rowcount
        if not self._postgres:
            self._lastrowid = getattr(self._cursor, 'lastrowid', None)
        return self

    def _wrap(self, row):
        if row is None:
            return None
        if self._postgres:
            # psycopg returns dict-like rows; normalise to our Row.
            keys = list(row.keys())
            return Row(keys, [row[k] for k in keys])
        return row  # sqlite3.Row already behaves correctly

    def fetchone(self):
        return self._wrap(self._cursor.fetchone())

    def fetchall(self):
        return [self._wrap(r) for r in self._cursor.fetchall()]

    def fetchmany(self, size=None):
        rows = self._cursor.fetchmany(size) if size else self._cursor.fetchmany()
        return [self._wrap(r) for r in rows]

    def __iter__(self):
        for row in self._cursor:
            yield self._wrap(row)

    @property
    def lastrowid(self):
        return self._lastrowid

    def close(self):
        try:
            self._cursor.close()
        except Exception:
            pass


# --- Connections -----------------------------------------------------------

class Connection:
    """A DB-API connection presenting the sqlite3 API regardless of the engine."""

    def __init__(self, raw, postgres: bool):
        self._raw = raw
        self._postgres = postgres

    def execute(self, sql: str, params=()):
        return Cursor(self._raw.cursor(), self._postgres).execute(sql, params)

    def executemany(self, sql: str, seq_of_params):
        return Cursor(self._raw.cursor(), self._postgres).executemany(sql, seq_of_params)

    def cursor(self):
        return Cursor(self._raw.cursor(), self._postgres)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        try:
            self._raw.rollback()
        except Exception:
            pass

    def close(self):
        try:
            self._raw.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()
        return False


def connect(sqlite_path: Path) -> Connection:
    """Open a connection to Postgres when configured, else to the SQLite file."""
    if using_postgres():
        require_driver()
        url = database_url()
        if _PG_DRIVER == 'psycopg3':
            raw = psycopg.connect(url, row_factory=_pg_dict_row, autocommit=False)
        else:  # psycopg2
            raw = psycopg2.connect(url, cursor_factory=_pg_dict_row)
        return Connection(raw, postgres=True)
    raw = sqlite3.connect(sqlite_path)
    raw.row_factory = sqlite3.Row
    return Connection(raw, postgres=False)


# --- Error types -----------------------------------------------------------

def integrity_error() -> tuple:
    """The exception tuple to catch for a unique/constraint violation."""
    errors = []
    if _PG_DRIVER == 'psycopg3':
        try:
            import psycopg
            errors.append(psycopg.IntegrityError)
        except Exception:
            pass
    elif _PG_DRIVER == 'psycopg2':
        try:
            import psycopg2
            errors.append(psycopg2.IntegrityError)
        except Exception:
            pass
    errors.append(sqlite3.IntegrityError)
    return tuple(errors)


def operational_error() -> tuple:
    """The exception tuple to catch for a missing column/table during migration."""
    errors = [sqlite3.OperationalError]
    if _PG_DRIVER == 'psycopg3':
        try:
            import psycopg
            errors += [psycopg.errors.UndefinedColumn, psycopg.errors.DuplicateColumn,
                       psycopg.errors.UndefinedTable, psycopg.OperationalError]
        except Exception:
            pass
    elif _PG_DRIVER == 'psycopg2':
        try:
            import psycopg2
            errors += [psycopg2.errors.UndefinedColumn, psycopg2.errors.DuplicateColumn,
                       psycopg2.errors.UndefinedTable, psycopg2.OperationalError]
        except Exception:
            pass
    return tuple(errors)


def describe_backend() -> str:
    """Human-readable description of the active backend, for startup logging."""
    if not using_postgres():
        return 'SQLite (local file)'
    host = database_url().split('@')[-1].split('/')[0]
    return f'PostgreSQL ({_PG_DRIVER or "no driver"}) at {host}'
