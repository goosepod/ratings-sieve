from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from ratings_sieve.cache import CacheError, CacheScope, SQLPreferenceCache, SQLitePreferenceCache
from ratings_sieve.core import Preference
from ratings_sieve.service import AudioMuse, BackendError, BypassService, FilterService, Navidrome


class SQLiteCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "cache.sqlite3"
        self.cache = SQLitePreferenceCache(self.path)
        self.scope = CacheScope("server-one", "account-one")

    def test_persists_across_cache_instances_and_caches_unrated(self):
        self.cache.put_many(self.scope, {"a": (Preference(), 100), "b": (Preference(5, True), 100)})
        reopened = SQLitePreferenceCache(self.path)
        self.assertEqual(reopened.get_many(self.scope, ["a", "b", "missing"], 120, 60),
                         {"a": Preference(), "b": Preference(5, True)})

    def test_expiry_boundary_and_future_timestamps(self):
        self.cache.put_many(self.scope, {"a": (Preference(4), 100), "future": (Preference(1), 200)})
        self.assertEqual(self.cache.get_many(self.scope, ["a", "future"], 159, 60), {"a": Preference(4)})
        self.assertEqual(self.cache.get_many(self.scope, ["a"], 160, 60), {})
        self.assertEqual(self.cache.get_many(self.scope, ["future"], 190, 60), {})
        self.assertEqual(self.cache.get_many(self.scope, ["future"], 200, 60), {"future": Preference(1)})

    def test_account_and_server_isolation(self):
        other_account = CacheScope("server-one", "account-two")
        other_server = CacheScope("server-two", "account-one")
        self.cache.put_many(self.scope, {"a": (Preference(1), 100)})
        self.cache.put_many(other_account, {"a": (Preference(5, True), 100)})
        self.cache.put_many(other_server, {"a": (Preference(3), 100)})
        for scope, expected in ((self.scope, Preference(1)), (other_account, Preference(5, True)),
                                (other_server, Preference(3))):
            self.assertEqual(self.cache.get_many(scope, ["a"], 120, 60), {"a": expected})

    def test_cleared_values_replace_previous_values_and_older_writes_are_ignored(self):
        self.cache.put_many(self.scope, {"a": (Preference(5, True), 100)})
        self.cache.put_many(self.scope, {"a": (Preference(0, False), 110)})
        self.cache.put_many(self.scope, {"a": (Preference(2, True), 105)})
        self.assertEqual(self.cache.get_many(self.scope, ["a"], 120, 60), {"a": Preference()})

    def test_empty_and_disabled_cache_reads_do_not_create_a_database(self):
        self.assertEqual(self.cache.get_many(self.scope, [], 100, 60), {})
        self.assertEqual(self.cache.get_many(self.scope, ["a"], 100, 0), {})
        self.cache.put_many(self.scope, {})
        self.assertFalse(self.path.exists())

    def test_sql_parameters_handle_unusual_ids_and_accounts(self):
        key = "a'); DROP TABLE preferences; --"
        scope = CacheScope("server'", 'account"')
        self.cache.put_many(scope, {key: (Preference(4), 100)})
        self.assertEqual(self.cache.get_many(scope, [key], 100, 60), {key: Preference(4)})
        self.cache.migrate()

    def test_migration_is_idempotent_and_retains_existing_rows(self):
        self.cache.migrate()
        self.cache.put_many(self.scope, {"a": (Preference(2), 100)})
        self.cache.migrate()
        self.assertEqual(self.cache.get_many(self.scope, ["a"], 100, 60), {"a": Preference(2)})

    def test_batch_read_uses_one_select(self):
        statements = []

        def connect():
            db = sqlite3.connect(self.path)
            db.set_trace_callback(statements.append)
            return db

        cache = SQLPreferenceCache(connect, initialize=True)
        cache.put_many(self.scope, {str(i): (Preference(3), 100) for i in range(100)})
        statements.clear()
        result = cache.get_many(self.scope, [str(i) for i in range(100)], 120, 60)
        self.assertEqual(len(result), 100)
        self.assertEqual(sum(s.startswith("SELECT") for s in statements), 1)

    def test_concurrent_instances_keep_values_separate(self):
        self.cache.migrate()

        def call(index):
            cache = SQLitePreferenceCache(self.path)
            scope = CacheScope("server", str(index))
            expected = Preference(index % 6, bool(index % 2))
            cache.put_many(scope, {"same-track": (expected, 100)})
            return cache.get_many(scope, ["same-track"], 120, 60), expected

        with ThreadPoolExecutor(max_workers=8) as pool:
            for result, expected in pool.map(call, range(24)):
                self.assertEqual(result, {"same-track": expected})

    def test_cache_errors_are_sanitized(self):
        db = Mock()
        db.cursor.side_effect = RuntimeError("password=private")
        cache = SQLPreferenceCache(lambda: db)
        with self.assertRaises(CacheError) as error:
            cache.get_many(self.scope, ["a"], 100, 60)
        self.assertNotIn("private", str(error.exception))
        db.rollback.assert_called_once()
        db.close.assert_called_once()

    def test_database_constraints_reject_invalid_preferences_atomically(self):
        with self.assertRaises(CacheError):
            self.cache.put_many(self.scope, {"a": (Preference(3), 100), "b": (Preference(8), 100)})
        self.assertEqual(self.cache.get_many(self.scope, ["a", "b"], 100, 60), {})

    def test_bad_table_names_and_nonpersistent_paths_are_rejected(self):
        with self.assertRaises(ValueError):
            SQLPreferenceCache(lambda: None, "x; DROP TABLE x")
        with self.assertRaises(ValueError):
            SQLitePreferenceCache(":memory:")


class ReadThroughCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = SQLitePreferenceCache(Path(self.tmp.name) / "cache.sqlite3")
        self.now = 100
        self.am = AudioMuse("http://am", "Navidrome")
        self.am.candidates = Mock(return_value=[{"item_id": "a"}, {"item_id": "b"}])
        self.nd = Navidrome("http://nd", "account-one", "password")
        self.nd.preference = Mock(side_effect=lambda key, **kw: Preference(4 if key == "a" else 0))

    def service(self, **kwargs):
        return FilterService(self.am, self.nd, cache=self.cache, clock=lambda: self.now, **kwargs)

    def test_second_request_and_new_service_instance_use_persistent_cache(self):
        expected = self.service().similar({"item_id": "seed"}, 2)
        self.assertEqual(self.nd.preference.call_count, 2)
        self.nd.preference.reset_mock()
        self.now = 120
        self.assertEqual(self.service().similar({"item_id": "seed"}, 2), expected)
        self.nd.preference.assert_not_called()

    def test_only_missing_or_expired_items_are_fetched(self):
        self.cache.put_many(CacheScope.for_backend(self.am, self.nd), {"a": (Preference(4), 100)})
        self.now = 120
        self.service().similar({"item_id": "seed"}, 2)
        self.assertEqual([c.args[0] for c in self.nd.preference.call_args_list], ["b"])
        self.nd.preference.reset_mock()
        self.now = 165
        self.service().similar({"item_id": "seed"}, 2)
        self.assertEqual([c.args[0] for c in self.nd.preference.call_args_list], ["a"])

    def test_changed_ratings_and_removed_hearts_are_refreshed(self):
        self.nd.preference.side_effect = lambda key, **kw: Preference(5, True) if key == "a" else Preference(4)
        self.service().similar({"item_id": "seed"}, 2)
        self.now = 161
        self.nd.preference.side_effect = lambda key, **kw: Preference(1) if key == "a" else Preference(0)
        self.assertEqual(self.service().similar({"item_id": "seed"}, 2), [{"item_id": "b"}])
        stored = self.cache.get_many(CacheScope.for_backend(self.am, self.nd), ["a", "b"], self.now, 60)
        self.assertEqual(stored, {"a": Preference(1), "b": Preference()})

    def test_expired_data_and_failed_api_calls_are_not_used_as_fallback(self):
        self.service().similar({"item_id": "seed"}, 2)
        self.now = 160
        self.nd.preference.side_effect = BackendError("unavailable")
        with self.assertRaises(BackendError):
            self.service().similar({"item_id": "seed"}, 2)
        self.assertEqual(self.cache.get_many(CacheScope.for_backend(self.am, self.nd), ["a"], 160, 60), {})

    def test_api_failure_is_not_cached_as_unrated(self):
        self.nd.preference.side_effect = BackendError("failed")
        with self.assertRaises(BackendError):
            self.service().similar({"item_id": "seed"}, 2)
        self.assertEqual(self.cache.get_many(CacheScope.for_backend(self.am, self.nd), ["a", "b"], 100, 60), {})

    def test_partial_api_failure_preserves_only_successful_lookups(self):
        self.nd.preference.side_effect = [Preference(4), BackendError("failed")]
        with self.assertRaises(BackendError):
            self.service().similar({"item_id": "seed"}, 2)
        self.assertEqual(self.cache.get_many(CacheScope.for_backend(self.am, self.nd), ["a", "b"], 100, 60),
                         {"a": Preference(4)})
        self.nd.preference.reset_mock(side_effect=True)
        self.nd.preference.return_value = Preference()
        self.assertEqual(len(self.service().similar({"item_id": "seed"}, 2)), 2)
        self.assertEqual([c.args[0] for c in self.nd.preference.call_args_list], ["b"])

    def test_database_read_failure_falls_back_to_direct_api(self):
        with patch.object(self.cache, "get_many", side_effect=CacheError("private")):
            self.assertEqual(len(self.service().similar({"item_id": "seed"}, 2)), 2)
        self.assertEqual(self.nd.preference.call_count, 2)

    def test_database_write_failure_still_returns_fresh_api_values(self):
        with patch.object(self.cache, "put_many", side_effect=CacheError("private")):
            self.assertEqual(len(self.service().similar({"item_id": "seed"}, 2)), 2)

    def test_zero_ttl_does_not_read_or_write_database(self):
        with patch.object(self.cache, "get_many") as get, patch.object(self.cache, "put_many") as put:
            self.service(cache_ttl=0).similar({"item_id": "seed"}, 2)
            get.assert_not_called()
            put.assert_not_called()
        self.assertEqual(self.nd.preference.call_count, 2)

    def test_distinct_catalogue_selection_and_api_key_only_accounts(self):
        one = Navidrome("http://nd", api_key="one-secret")
        two = Navidrome("http://nd", api_key="two-secret")
        a = CacheScope.for_backend(self.am, one)
        b = CacheScope.for_backend(self.am, two)
        c = CacheScope.for_backend(AudioMuse("http://am", "Other"), one)
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertNotIn("one-secret", repr(a))
        self.assertNotIn("two-secret", repr(b))


class AudioMuseCacheTests(unittest.TestCase):
    def test_install_migration_borrows_host_connection_and_uses_namespaced_table(self):
        import sys
        import types
        from ratings_sieve.cache import audiomuse_cache, migrate_audiomuse

        api = types.ModuleType("plugin.api")
        db, cursor = Mock(), Mock()
        db.cursor.return_value = cursor
        api.get_db = Mock(return_value=db)
        api.table = Mock(return_value="plugin_ratings_sieve__preferences")
        with patch.dict(sys.modules, {"plugin": types.ModuleType("plugin"), "plugin.api": api}):
            migrate_audiomuse(db)
            api.table.assert_called_with("preferences")
            api.get_db.assert_not_called()
            db.commit.assert_called_once()
            db.close.assert_not_called()
            self.assertIn("plugin_ratings_sieve__preferences", cursor.execute.call_args.args[0])
            cache = audiomuse_cache()
            self.assertEqual(cache.placeholder, "%s")
            self.assertFalse(cache.close_connection)


if __name__ == "__main__":
    unittest.main()
