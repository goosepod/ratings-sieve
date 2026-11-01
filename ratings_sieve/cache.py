"""Persistent, account-scoped preference storage for AudioMuse and standalone use."""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .core import Preference


class CacheError(Exception):
    pass


@dataclass(frozen=True)
class CacheScope:
    server: str
    account: str

    @classmethod
    def for_backend(cls, audiomuse, navidrome):
        # Include catalogue selection and provider URL: IDs are not globally unique.
        server = json.dumps([audiomuse.url, audiomuse.server, navidrome.url], separators=(",", ":"))
        account = navidrome.username or "api-key:" + hashlib.sha256(navidrome.api_key.encode()).hexdigest()
        return cls(server, account)


class SQLPreferenceCache:
    def __init__(self, connect, table_name="preferences", placeholder="?", close_connection=True,
                 initialize=False):
        if not re.fullmatch(r"[a-z][a-z0-9_]*", table_name):
            raise ValueError("invalid cache table name")
        if placeholder not in {"?", "%s"}:
            raise ValueError("unsupported SQL parameter style")
        self.connect, self.table_name, self.placeholder = connect, table_name, placeholder
        self.close_connection, self.initialize = close_connection, initialize

    def _schema(self, cursor):
        cursor.execute(f"""CREATE TABLE IF NOT EXISTS {self.table_name} (
            server_key TEXT NOT NULL,
            account_key TEXT NOT NULL,
            track_id TEXT NOT NULL,
            rating INTEGER NOT NULL CHECK (rating BETWEEN 0 AND 5),
            heart BOOLEAN NOT NULL,
            fetched_at DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (server_key, account_key, track_id)
        )""")

    @contextmanager
    def _transaction(self, connection=None):
        db, cursor = connection, None
        try:
            if db is None:
                db = self.connect()
            cursor = db.cursor()
            if self.initialize:
                self._schema(cursor)
            yield cursor
            db.commit()
        except Exception:
            if db is not None:
                try:
                    db.rollback()
                except Exception:
                    pass
            raise CacheError("preference cache unavailable") from None
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            if db is not None and connection is None and self.close_connection:
                try:
                    db.close()
                except Exception:
                    pass

    def migrate(self, connection=None):
        with self._transaction(connection) as cursor:
            self._schema(cursor)

    def get_many(self, scope, item_ids, now, ttl):
        if not item_ids or ttl <= 0:
            return {}
        p = self.placeholder
        marks = ",".join([p] * len(item_ids))
        with self._transaction() as cursor:
            cursor.execute(f"""SELECT track_id, rating, heart FROM {self.table_name}
                WHERE server_key={p} AND account_key={p}
                AND fetched_at>{p} AND fetched_at<={p} AND track_id IN ({marks})""",
                (scope.server, scope.account, now - ttl, now, *item_ids))
            return {row[0]: Preference.from_song({"userRating": row[1], "starred": bool(row[2])})
                    for row in cursor.fetchall()}

    def put_many(self, scope, entries):
        """Entries map IDs to (validated Preference, lookup-start timestamp)."""
        if not entries:
            return
        p = self.placeholder
        with self._transaction() as cursor:
            cursor.executemany(f"""INSERT INTO {self.table_name}
                (server_key, account_key, track_id, rating, heart, fetched_at)
                VALUES ({','.join([p] * 6)})
                ON CONFLICT (server_key, account_key, track_id) DO UPDATE SET
                    rating=excluded.rating, heart=excluded.heart, fetched_at=excluded.fetched_at
                WHERE {self.table_name}.fetched_at <= excluded.fetched_at""",
                [(scope.server, scope.account, item_id, pref.rating, pref.heart, fetched_at)
                 for item_id, (pref, fetched_at) in entries.items()])


class SQLitePreferenceCache(SQLPreferenceCache):
    def __init__(self, path):
        path = Path(path).expanduser()
        if str(path) == ":memory:":
            raise ValueError("standalone cache requires a persistent file path")

        def connect():
            path.parent.mkdir(parents=True, exist_ok=True)
            return sqlite3.connect(str(path), timeout=2)

        super().__init__(connect, initialize=True)


def audiomuse_cache():
    # Import only in the plugin runtime and use the sanctioned facade.
    from plugin.api import get_db, table

    return SQLPreferenceCache(get_db, table("preferences"), "%s", close_connection=False)


def migrate_audiomuse(connection):
    audiomuse_cache().migrate(connection)
