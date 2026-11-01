from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from sonic_sieve.accounts import (
    Account, AccountRouter, AuthenticationError, ConfigurationError, router_from_env,
)
from sonic_sieve.core import Policy, Preference
from sonic_sieve.service import BackendError, BypassService, FilterService
from sonic_sieve.web import create_app


def tracks():
    return [{"item_id": key, "distance": distance, "title": key}
            for key, distance in (("a", .1), ("b", .2), ("c", .3))]


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.am = Mock(server="Navidrome")
        self.am.candidates.side_effect = lambda params, count: tracks()[:count]
        self.first, self.second = Mock(), Mock()
        self.first.preference.side_effect = lambda key, **kw: {
            "a": Preference(1, True), "b": Preference(4), "c": Preference(0, True)}[key]
        self.second.preference.side_effect = lambda key, **kw: {
            "a": Preference(5), "b": Preference(1, True), "c": Preference(0)}[key]
        self.router = AccountRouter([
            Account("listener_one", "first-token", FilterService(self.am, self.first)),
            Account("listener_two", "second-token", FilterService(self.am, self.second,
                    Policy(minimum_rating=5, include_unrated=False))),
            Account("disabled", "disabled-token", BypassService(self.am)),
        ], BypassService(self.am), "ordinary-token")
        self.app = create_app(router=self.router)

    def request(self, token, **kwargs):
        with self.app.test_client() as client:
            return client.get("/api/similar_tracks?item_id=seed&n=3",
                              headers={"Authorization": "Bearer " + token, **kwargs})

    def test_each_account_has_separate_preferences_and_policy(self):
        self.assertEqual([t["item_id"] for t in self.request("first-token").json], ["c", "b"])
        self.second.preference.assert_not_called()
        self.assertEqual([t["item_id"] for t in self.request("second-token").json], ["a"])
        self.assertEqual(self.first.preference.call_count, 3)

    def test_disabled_account_returns_unchanged_core_results(self):
        result = self.request("disabled-token")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json, tracks())
        self.am.candidates.assert_called_once_with({"item_id": "seed"}, 3)
        self.first.preference.assert_not_called()
        self.second.preference.assert_not_called()

    def test_unknown_listener_with_valid_bypass_token_uses_no_account(self):
        self.assertEqual(self.request("ordinary-token").json, tracks())
        self.first.preference.assert_not_called()
        self.second.preference.assert_not_called()

    def test_invalid_or_missing_identity_never_inherits_another_profile(self):
        for token in ("", "unknown-token", "first-token-extra"):
            self.assertEqual(self.request(token).status_code, 401)
        self.assertEqual(self.app.test_client().get(
            "/api/similar_tracks?item_id=seed").status_code, 401)
        self.am.candidates.assert_not_called()
        self.first.preference.assert_not_called()
        self.second.preference.assert_not_called()

    def test_cannot_select_other_account_by_query_or_username_header(self):
        client = self.app.test_client()
        for selector in ("user", "username", "profile", "account"):
            response = client.get("/api/similar_tracks?item_id=seed&" + selector + "=listener_two",
                                  headers={"Authorization": "Bearer first-token"})
            self.assertEqual(response.status_code, 400)
        for header in ("X-Sonic-Sieve-User", "X-Sonic-Sieve-Username"):
            self.assertEqual(self.request("first-token", **{header: "listener_two"}).status_code, 400)
        self.second.preference.assert_not_called()

    def test_dedicated_token_header_supports_outer_host_authentication(self):
        result = self.request("outer-host-api-token", **{"X-Sonic-Sieve-Token": "first-token"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual([t["item_id"] for t in result.json], ["c", "b"])
        self.second.preference.assert_not_called()

    def test_bad_explicit_token_cannot_fall_back_to_valid_bearer(self):
        for token in ("ordinary-token", "first-token"):
            for explicit in ("", "unknown", "ordinary-token"):
                self.assertEqual(self.request(token, **{"X-Sonic-Sieve-Token": explicit}).status_code, 401)
        self.am.candidates.assert_not_called()

    def test_one_account_backend_failure_never_falls_back_to_another(self):
        self.first.preference.side_effect = BackendError("private failure")
        response = self.request("first-token")
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("private", response.text)
        self.second.preference.assert_not_called()
        self.assertEqual([t["item_id"] for t in self.request("second-token").json], ["a"])
        self.assertEqual(self.request("disabled-token").json, tracks())

    def test_concurrent_requests_keep_accounts_separate(self):
        tokens = ["first-token", "second-token", "disabled-token"] * 12
        expected = {"first-token": ["c", "b"], "second-token": ["a"], "disabled-token": ["a", "b", "c"]}

        def call(token):
            result = self.request(token)
            return result.status_code, [t["item_id"] for t in result.json]

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(call, tokens))
        for token, (status, ids) in zip(tokens, results):
            self.assertEqual(status, 200)
            self.assertEqual(ids, expected[token])

    def test_disabled_service_preserves_duplicates_seed_metadata_and_order(self):
        original = tracks() + [{"item_id": "seed", "extra": {"value": 7}}, tracks()[0]]
        self.am.candidates.side_effect = None
        self.am.candidates.return_value = original
        self.assertIs(BypassService(self.am).similar({"item_id": "seed"}, 5), original)

    def test_profile_secrets_are_not_in_representations(self):
        self.assertNotIn("first-token", repr(self.router.accounts[0]))

    def test_injected_legacy_service_still_accepts_environment_token(self):
        service = FilterService(self.am, self.first)
        with patch.dict("os.environ", {"AM_PREF_TOKEN": "legacy-secret"}):
            response = create_app(service=service).test_client().get(
                "/api/similar_tracks?item_id=seed&n=3",
                headers={"Authorization": "Bearer legacy-secret"})
        self.assertEqual(response.status_code, 200)

    def test_ambiguous_names_and_tokens_are_rejected(self):
        for accounts, bypass in (([Account("a", "same", None), Account("b", "same", None)], ""),
                                  ([Account("a", "one", None), Account("a", "two", None)], ""),
                                  ([Account("a", "", None)], ""),
                                  ([Account("a", "same", None)], "same")):
            with self.subTest(accounts=accounts), self.assertRaises(ConfigurationError):
                AccountRouter(accounts, self.am, bypass)


class ProfileConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "profiles.json"
        self.env = {
            "AM_PREF_AUDIOMUSE_URL": "http://am", "AM_PREF_SERVER": "Navidrome",
            "AM_PREF_NAVIDROME_URL": "http://nd", "AM_PREF_PROFILES_FILE": str(self.path),
            "FIRST_TOKEN": "first-secret", "SECOND_TOKEN": "second-secret",
            "FIRST_PASSWORD": "first-password", "SECOND_PASSWORD": "second-password",
            "AM_PREF_TOKEN": "ordinary-token",
        }
        self.profiles = [
            {"username": "listener_one", "enabled": True,
             "token_env": "FIRST_TOKEN", "password_env": "FIRST_PASSWORD"},
            {"username": "listener_two", "enabled": False, "token_env": "SECOND_TOKEN"},
        ]
        self.write()

    def write(self):
        self.path.write_text(json.dumps({"profiles": self.profiles}), encoding="utf-8")

    def test_credentials_bind_to_the_correct_navidrome_account(self):
        self.profiles[1].update(enabled=True, password_env="SECOND_PASSWORD",
                                policy={"minimum_rating": 5, "boost_hearts": False})
        self.write()
        router = router_from_env(self.env)
        first = router.resolve("Bearer first-secret")
        second = router.resolve("Bearer second-secret")
        self.assertEqual((first.navidrome.username, first.navidrome.password),
                         ("listener_one", "first-password"))
        self.assertEqual((second.navidrome.username, second.navidrome.password),
                         ("listener_two", "second-password"))
        self.assertEqual(first.policy, Policy())
        self.assertEqual(second.policy.minimum_rating, 5)
        self.assertFalse(second.policy.boost_hearts)

    def test_disabled_profile_needs_no_navidrome_credentials_or_url(self):
        self.profiles = [self.profiles[1]]
        self.write()
        self.env.pop("AM_PREF_NAVIDROME_URL")
        self.assertIsInstance(router_from_env(self.env).resolve("Bearer second-secret"), BypassService)

    def test_new_profiles_default_to_disabled(self):
        self.profiles[0].pop("enabled")
        self.write()
        self.assertIsInstance(router_from_env(self.env).resolve("Bearer first-secret"), BypassService)

    def test_token_revocation_and_policy_edits_reload_without_state_leakage(self):
        with patch.dict("os.environ", self.env, clear=True):
            with patch("sonic_sieve.service.JsonHTTP.get", side_effect=self.http):
                client = create_app().test_client()
                headers = {"Authorization": "Bearer first-secret"}
                url = "/api/similar_tracks?item_id=seed&n=3"
                self.assertEqual([t["item_id"] for t in client.get(url, headers=headers).json], ["c", "b"])
                self.profiles[0]["policy"] = {"include_unrated": False}
                self.write()
                self.assertEqual([t["item_id"] for t in client.get(url, headers=headers).json], ["b"])
                self.profiles[0]["enabled"] = False
                self.write()
                self.assertEqual(client.get(url, headers=headers).json, tracks())
                self.profiles[0]["token_env"] = "SECOND_TOKEN"
                self.profiles = self.profiles[:1]
                self.write()
                self.assertEqual(client.get(url, headers=headers).status_code, 401)

    @staticmethod
    def http(url, params, *args, **kwargs):
        if url.startswith("http://am/"):
            return tracks()
        ratings = {"a": 1, "b": 4, "c": 0} if params.get("u") == "listener_one" else {
            "a": 5, "b": 1, "c": 4}
        song = {"id": params["id"], "userRating": ratings[params["id"]]}
        if params.get("u") == "listener_one" and params["id"] == "c":
            song["starred"] = "2026-01-01T00:00:00Z"
        return {"subsonic-response": {"status": "ok", "song": song}}

    def test_end_to_end_profile_selection_calls_navidrome_as_each_account(self):
        self.profiles[1].update(enabled=True, password_env="SECOND_PASSWORD")
        self.write()
        with patch.dict("os.environ", self.env, clear=True):
            with patch("sonic_sieve.service.JsonHTTP.get", side_effect=self.http) as http:
                client = create_app().test_client()
                for token, expected, username in (("first-secret", ["c", "b"], "listener_one"),
                                                   ("second-secret", ["a", "c"], "listener_two")):
                    http.reset_mock()
                    response = client.get("/api/similar_tracks?item_id=seed&n=3",
                                           headers={"Authorization": "Bearer " + token})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual([t["item_id"] for t in response.json], expected)
                    calls = [call for call in http.call_args_list if call.args[0].startswith("http://nd/")]
                    self.assertEqual(len(calls), 3)
                    self.assertTrue(all(call.args[1]["u"] == username for call in calls))
                    self.assertTrue(all("p" not in call.args[1] for call in calls))

    def test_enabled_api_key_profile(self):
        self.profiles[0].pop("password_env")
        self.profiles[0]["api_key_env"] = "FIRST_KEY"
        self.env["FIRST_KEY"] = "api-key"
        self.write()
        backend = router_from_env(self.env).resolve("Bearer first-secret")
        self.assertEqual(backend.navidrome.api_key, "api-key")
        self.assertEqual(backend.navidrome.password, "")

    def test_example_profile_file_is_valid_with_example_environment_bindings(self):
        example = Path(__file__).resolve().parents[1] / "profiles.example.json"
        self.env.update(AM_PREF_PROFILES_FILE=str(example),
                        SONIC_SIEVE_LISTENER_ONE_TOKEN="example-first-token",
                        SONIC_SIEVE_LISTENER_ONE_PASSWORD="example-password",
                        SONIC_SIEVE_LISTENER_TWO_TOKEN="example-second-token")
        router = router_from_env(self.env)
        self.assertIsInstance(router.resolve("Bearer example-first-token"), FilterService)
        self.assertIsInstance(router.resolve("Bearer example-second-token"), BypassService)

    def test_missing_profile_file_never_falls_back_to_legacy_credentials(self):
        self.path.unlink()
        self.env.update(AM_PREF_NAVIDROME_USER="legacy", AM_PREF_NAVIDROME_PASSWORD="private")
        with patch.dict("os.environ", self.env, clear=True):
            result = create_app().test_client().get("/api/similar_tracks?item_id=seed",
                         headers={"Authorization": "Bearer ordinary-token"})
        self.assertEqual(result.status_code, 503)
        self.assertNotIn("private", result.text)
        self.assertNotIn(str(self.path), result.text)

    def test_invalid_profile_configuration_fails_closed(self):
        invalid = [
            {"enabled": "false"}, {"enabled": None}, {"username": ""},
            {"policy": {"minimum_rating": 7}}, {"policy": {"boost_hearts": "true"}},
            {"policy": {"unknown": True}}, {"policy": []},
            {"token_env": "MISSING"}, {"password_env": "MISSING"},
            {"password_env": ""}, {"api_key_env": "FIRST_PASSWORD"},
            {"extra": "ignored?"},
        ]
        original = dict(self.profiles[0])
        for change in invalid:
            self.profiles[0] = dict(original, **change)
            self.write()
            with self.subTest(change=change), self.assertRaises(ConfigurationError):
                router_from_env(self.env)

    def test_invalid_json_and_duplicate_keys_fail_closed(self):
        for content in ('{', 'null', '[]', '{"profiles": {}}', '{"profiles": [], "profiles": []}'):
            self.path.write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaises(ConfigurationError):
                router_from_env(self.env)

    def test_legacy_single_account_configuration_is_preserved(self):
        self.env.pop("AM_PREF_PROFILES_FILE")
        self.env.update(AM_PREF_NAVIDROME_USER="legacy", AM_PREF_NAVIDROME_PASSWORD="password")
        router = router_from_env(self.env)
        backend = router.resolve("Bearer ordinary-token")
        self.assertIsInstance(backend, FilterService)
        self.assertEqual(backend.navidrome.username, "legacy")
        with self.assertRaises(AuthenticationError):
            router.resolve("Bearer first-secret")

    def test_per_account_tokens_work_without_any_shared_bypass_token(self):
        self.env.pop("AM_PREF_TOKEN")
        router = router_from_env(self.env)
        self.assertIsInstance(router.resolve("Bearer first-secret"), FilterService)
        with self.assertRaises(AuthenticationError):
            router.resolve("")


if __name__ == "__main__":
    unittest.main()
