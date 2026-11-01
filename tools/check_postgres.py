"""Explicit integration check against a disposable PostgreSQL database only.

Set RATINGS_SIEVE_TEST_POSTGRES_DSN. Never point this at production: this check
creates and drops its own temporary table. Requires psycopg2 (AudioMuse includes it).
"""

import os
import importlib.util
from pathlib import Path
import sys
import types
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg2

from ratings_sieve.cache import CacheError, CacheScope, SQLPreferenceCache
from ratings_sieve.core import Preference


def main():
    dsn = os.environ["RATINGS_SIEVE_TEST_POSTGRES_DSN"]
    table = "ratings_sieve_test_" + uuid.uuid4().hex
    cache = SQLPreferenceCache(lambda: psycopg2.connect(dsn), table, "%s")
    scope, other = CacheScope("server", "one"), CacheScope("server", "two")
    try:
        cache.migrate()
        cache.put_many(scope, {"same": (Preference(5, True), 100), "unrated": (Preference(), 100)})
        cache.put_many(other, {"same": (Preference(1), 100)})
        assert cache.get_many(scope, ["same", "unrated"], 120, 60) == {
            "same": Preference(5, True), "unrated": Preference()}
        assert cache.get_many(other, ["same"], 120, 60) == {"same": Preference(1)}
        assert cache.get_many(scope, ["same"], 160, 60) == {}
        cache.put_many(scope, {"same": (Preference(), 170)})
        cache.put_many(scope, {"same": (Preference(5, True), 165)})
        assert cache.get_many(scope, ["same"], 180, 60) == {"same": Preference()}
        cache.migrate()
        assert cache.get_many(scope, ["same"], 180, 60) == {"same": Preference()}
        try:
            cache.put_many(scope, {"good": (Preference(3), 180), "bad": (Preference(7), 180)})
        except CacheError:
            pass
        else:
            raise AssertionError("constraint violation was accepted")
        assert cache.get_many(scope, ["good", "bad"], 180, 60) == {}
        print("PostgreSQL integration passed: migration, persistence, isolation, TTL, clearing, ordering, rollback")
        if os.environ.get("RATINGS_SIEVE_TEST_AUDIOMUSE_FACADE") == "1":
            check_audiomuse_facade(dsn)
    finally:
        with psycopg2.connect(dsn) as db:
            with db.cursor() as cursor:
                cursor.execute(f"DROP TABLE IF EXISTS {table}")


def check_audiomuse_facade(dsn):
    """Exercise the actual AudioMuse facade in an isolated AudioMuse image."""
    from unittest.mock import patch
    import plugin.api as api

    namespace = types.ModuleType("audiomuse_plugins")
    namespace.__path__ = []
    sys.modules[namespace.__name__] = namespace
    package_dir = Path(__file__).resolve().parents[1] / "ratings_sieve"
    name = "audiomuse_plugins.ratings_sieve"
    spec = importlib.util.spec_from_file_location(name, package_dir / "__init__.py",
                                                submodule_search_locations=[str(package_dir)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    spec.loader.exec_module(package)
    context = api.PluginContext("ratings_sieve", "web")
    package.register(context)
    db = psycopg2.connect(dsn)
    try:
        assert context.blueprint.name == "ratings_sieve"
        assert len(context.install_hooks) == 1
        context.install_hooks[0](db)
        host_cache = sys.modules[name + ".cache"]
        with patch.object(api, "get_db", return_value=db):
            store = host_cache.audiomuse_cache()
            assert store.table_name == "plugin_ratings_sieve__preferences"
            scope = host_cache.CacheScope("facade-server", "facade-user")
            pref = sys.modules[name + ".core"].Preference(4, True)
            store.put_many(scope, {"song": (pref, 100)})
            assert store.get_many(scope, ["song"], 120, 60) == {"song": pref}
            assert not db.closed
        print("Actual AudioMuse PluginContext, install hook, namespacing and borrowed connection passed")
    finally:
        db.close()


if __name__ == "__main__":
    main()
