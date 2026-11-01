import copy
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

from flask import Flask
from werkzeug.datastructures import MultiDict

from ratings_sieve.accounts import ConfigurationError
from ratings_sieve.cache import CacheScope, SQLitePreferenceCache
from ratings_sieve.core import Preference
from ratings_sieve.service import AudioMuse, BackendError, Navidrome
from ratings_sieve.settings import SecretBox, defaults, router_from_settings
from ratings_sieve.web import create_blueprint


class Inputs(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.inputs = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            self.inputs.append(dict(attrs))

    def value(self, name):
        return next(i.get("value", "") for i in self.inputs if i.get("name") == name)

    def tokens(self):
        return {i["aria-label"].removeprefix("Request token for "): i["value"]
                for i in self.inputs if i.get("aria-label", "").startswith("Request token for ")}


class MemorySettings:
    def __init__(self, directory):
        self.configuration = None
        self.box = SecretBox(Path(directory) / "keys" / ".ratings_sieve.key")
        self.admin = True
        self.save_count = 0

    def load(self):
        return copy.deepcopy(self.configuration)

    def save(self, config):
        self.configuration = copy.deepcopy(config)
        self.save_count += 1

    def servers(self):
        return [{"server_id": "navidrome-one", "name": "Navidrome", "server_type": "navidrome",
                 "creds": {"url": "http://nd", "user": "analysis-user", "password": "DO-NOT-REUSE"}}]

    def is_admin(self):
        return self.admin

    def default_upstream_token(self):
        return "host-api-secret"

    def render(self, body):
        return body


class SettingsPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MemorySettings(self.tmp.name)
        self.cache = SQLitePreferenceCache(Path(self.tmp.name) / "cache.sqlite3")
        self.app = Flask("settings-tests")
        self.app.register_blueprint(create_blueprint(settings_store=self.store,
            cache_factory=lambda: self.cache), url_prefix="/plugins/ratings_sieve")
        self.client = self.app.test_client()
        self.url = "/plugins/ratings_sieve/settings"
        self.first, self.second = uuid.uuid4().hex, uuid.uuid4().hex

    def form(self, two=False, action="save"):
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        values = Inputs(page.text)
        data = MultiDict({"csrf_token": values.value("csrf_token"),
            "revision": values.value("revision"), "server_id": "navidrome-one",
            "audiomuse_url": "http://am", "cache_ttl": "60", "action": action})
        for identifier, name, enabled in ((self.first, "listener_one", True),
                                         (self.second, "listener_two", False))[:2 if two else 1]:
            data.add("profile_id", identifier)
            prefix = "p." + identifier + "."
            data.update({prefix + "username": name, prefix + "credential_type": "password",
                prefix + "credential": "private-password" if enabled else "",
                prefix + "minimum_rating": "3", prefix + "candidate_multiplier": "4",
                prefix + "include_unrated": "on", prefix + "boost_hearts": "on"})
            if enabled:
                data[prefix + "enabled"] = "on"
        return data

    def save(self, two=False):
        result = self.client.post(self.url, data=self.form(two))
        self.assertEqual(result.status_code, 200, result.text)
        return result, Inputs(result.text).tokens()

    @staticmethod
    def http(url, params, *args, **kwargs):
        if url.endswith("/api/servers"):
            return {"servers": [{"name": "Navidrome"}]}
        if url.endswith("/api/health"):
            return {"status": "ok"}
        if url.endswith("/rest/ping.view"):
            return {"subsonic-response": {"status": "ok"}}
        if url.startswith("http://am/"):
            return [{"item_id": "a"}, {"item_id": "b"}]
        return {"subsonic-response": {"status": "ok", "song": {
            "id": params["id"], "userRating": 1 if params["id"] == "a" else 4}}}

    def test_page_contains_forms_and_never_exposes_server_credentials(self):
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        for control in ("Navidrome server", "Cache lifetime", "Add account", "Minimum rating",
                        "Test connections", "Clear cached ratings", "Enable filtering"):
            self.assertIn(control, page.text)
        for secret in ("DO-NOT-REUSE", "host-api-secret"):
            self.assertNotIn(secret, page.text)
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertIn("HttpOnly", page.headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", page.headers["Set-Cookie"])

    def test_save_encrypts_credentials_and_stores_only_token_hashes(self):
        result, tokens = self.save(two=True)
        config = self.store.configuration
        self.assertEqual(self.store.save_count, 1)
        self.assertNotIn("private-password", json.dumps(config))
        self.assertNotIn("private-password", result.text)
        encrypted = config["profiles"][0]["credential_encrypted"]
        self.assertEqual(self.store.box.decrypt(self.first + ":credential", encrypted), "private-password")
        self.assertEqual(config["profiles"][0]["token_hash"], hashlib.sha256(tokens["listener_one"].encode()).hexdigest())
        self.assertNotIn(tokens["listener_one"], json.dumps(config))
        self.assertFalse(config["profiles"][1]["enabled"])
        page = self.client.get(self.url)
        self.assertNotIn(tokens["listener_one"], page.text)
        self.assertNotIn(encrypted, page.text)
        self.assertEqual(Inputs(page.text).tokens(), {})
        self.assertTrue(all(i.get("value", "") == "" for i in Inputs(page.text).inputs if i.get("type") == "password"))

    def test_blank_credentials_keep_the_previous_secret_and_token(self):
        self.save()
        before = copy.deepcopy(self.store.configuration)
        data = self.form()
        data["p." + self.first + ".credential"] = ""
        response = self.client.post(self.url, data=data)
        self.assertEqual(response.status_code, 200)
        after = self.store.configuration
        self.assertEqual(after["profiles"][0]["credential_encrypted"], before["profiles"][0]["credential_encrypted"])
        self.assertEqual(after["profiles"][0]["token_hash"], before["profiles"][0]["token_hash"])
        self.assertEqual(Inputs(response.text).tokens(), {})

    def test_saved_settings_drive_requests_without_environment_or_json(self):
        _, tokens = self.save(two=True)
        with patch.dict("os.environ", {}, clear=True), patch("ratings_sieve.service.JsonHTTP.get", side_effect=self.http) as http:
            response = self.client.get("/plugins/ratings_sieve/api/similar_tracks?item_id=seed&n=2",
                                      headers={"Authorization": "Bearer " + tokens["listener_one"]})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json, [{"item_id": "b"}])
            nd_calls = [call for call in http.call_args_list if call.args[0].startswith("http://nd/")]
            self.assertTrue(all(call.args[1]["u"] == "listener_one" for call in nd_calls))
            self.assertEqual(http.call_args_list[0].args[2], {"Authorization": "Bearer host-api-secret"})
            http.reset_mock()
            disabled = self.client.get("/plugins/ratings_sieve/api/similar_tracks?item_id=seed&n=2",
                                      headers={"Authorization": "Bearer " + tokens["listener_two"]})
            self.assertEqual(disabled.json, [{"item_id": "a"}, {"item_id": "b"}])
            self.assertEqual(http.call_count, 1)

    def test_rotated_token_is_shown_once_and_revokes_old_token(self):
        _, old = self.save()
        data = self.form()
        data["p." + self.first + ".credential"] = ""
        data["p." + self.first + ".rotate_token"] = "on"
        response = self.client.post(self.url, data=data)
        new = Inputs(response.text).tokens()
        self.assertNotEqual(new["listener_one"], old["listener_one"])
        api = "/plugins/ratings_sieve/api/similar_tracks?item_id=seed"
        self.assertEqual(self.client.get(api, headers={"Authorization": "Bearer " + old["listener_one"]}).status_code, 401)
        with patch("ratings_sieve.service.JsonHTTP.get", side_effect=self.http):
            self.assertEqual(self.client.get(api, headers={"Authorization": "Bearer " + new["listener_one"]}).status_code, 200)

    def test_removing_profile_revokes_its_token(self):
        _, tokens = self.save(two=True)
        data = self.form(two=False)
        self.assertEqual(self.client.post(self.url, data=data).status_code, 200)
        response = self.client.get("/plugins/ratings_sieve/api/similar_tracks?item_id=seed",
                                  headers={"Authorization": "Bearer " + tokens["listener_two"]})
        self.assertEqual(response.status_code, 401)

    def test_non_admin_is_denied_for_every_settings_action(self):
        self.store.admin = False
        self.assertEqual(self.client.get(self.url).status_code, 403)
        for action in ("save", "test", "clear_cache"):
            self.assertEqual(self.client.post(self.url, data={"action": action}).status_code, 403)
        self.assertIsNone(self.store.configuration)

    def test_csrf_is_required_signed_and_cookie_bound(self):
        for value in ("", "forged.signature", "λ"):
            data = self.form()
            data["csrf_token"] = value
            self.assertEqual(self.client.post(self.url, data=data).status_code, 403)
        data = self.form()
        fresh = self.app.test_client()
        self.assertEqual(fresh.post(self.url, data=data).status_code, 403)
        self.assertIsNone(self.store.configuration)

    def test_invalid_forms_leave_saved_configuration_unchanged_and_hide_secrets(self):
        self.save(two=True)
        saved = copy.deepcopy(self.store.configuration)
        for field, value in (("cache_ttl", "-1"), ("server_id", "unknown"),
                             ("audiomuse_url", "http://user:secret@am"),
                             ("p." + self.first + ".minimum_rating", "6"),
                             ("p." + self.second + ".username", "LISTENER_ONE"),
                             ("p." + self.first + ".credential_type", "bad")):
            data = self.form(two=True)
            data[field] = value
            result = self.client.post(self.url, data=data)
            self.assertEqual(result.status_code, 400, (field, result.text))
            self.assertNotIn("private-password", result.text)
            self.assertEqual(self.store.configuration, saved)

    def test_changing_credential_type_requires_a_new_value(self):
        self.save()
        data = self.form()
        data["p." + self.first + ".credential_type"] = "api_key"
        data["p." + self.first + ".credential"] = ""
        self.assertEqual(self.client.post(self.url, data=data).status_code, 400)
        data = self.form()
        data["p." + self.first + ".credential_type"] = "api_key"
        data["p." + self.first + ".credential"] = "private-api-key"
        self.assertEqual(self.client.post(self.url, data=data).status_code, 200)
        router = router_from_settings(self.store.load(), self.store, lambda: None)
        self.assertEqual(router.accounts[0].service.navidrome.api_key, "private-api-key")

    def test_connection_test_uses_unsaved_values_without_saving_or_revealing_tokens(self):
        data = self.form(action="test")
        with patch("ratings_sieve.service.JsonHTTP.get", side_effect=self.http) as http:
            response = self.client.post(self.url, data=data, headers={"X-Requested-With": "RatingsSieve"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("connected successfully", response.json["message"])
        self.assertIsNone(self.store.configuration)
        self.assertEqual(self.store.save_count, 0)
        self.assertNotIn("private-password", response.text)
        self.assertTrue(any(call.args[0].endswith("/rest/ping.view") for call in http.call_args_list))

    def test_failed_connection_reports_error_without_persisting_changes(self):
        data = self.form(action="test")
        with patch("ratings_sieve.service.JsonHTTP.get", side_effect=BackendError("private-url")):
            result = self.client.post(self.url, data=data, headers={"X-Requested-With": "RatingsSieve"})
        self.assertEqual(result.status_code, 503)
        self.assertIn("AudioMuse connection failed", result.json["error"])
        self.assertNotIn("private-url", result.text)
        self.assertIsNone(self.store.configuration)

    def test_failed_navidrome_connection_identifies_the_account(self):
        data = self.form(action="test")

        def http(url, params, *args, **kwargs):
            if url.endswith("/rest/ping.view"):
                raise BackendError("sensitive URL or credential")
            return self.http(url, params, *args, **kwargs)

        with patch("ratings_sieve.service.JsonHTTP.get", side_effect=http):
            result = self.client.post(self.url, data=data, headers={"X-Requested-With": "RatingsSieve"})
        self.assertEqual(result.status_code, 503)
        self.assertIn("Navidrome connection failed for listener_one", result.json["error"])
        self.assertNotIn("sensitive", result.text)
        self.assertIsNone(self.store.configuration)

    def test_clear_cache_only_deletes_preferences_and_does_not_save_form_changes(self):
        self.save()
        saved = copy.deepcopy(self.store.configuration)
        scope = CacheScope("server", "user")
        self.cache.put_many(scope, {"song": (Preference(4), 100)})
        data = self.form(action="clear_cache")
        data["cache_ttl"] = "999"
        result = self.client.post(self.url, data=data, headers={"X-Requested-With": "RatingsSieve"})
        self.assertEqual(result.status_code, 200)
        self.assertIn("Cleared 1", result.json["message"])
        self.assertEqual(self.cache.get_many(scope, ["song"], 100, 60), {})
        self.assertEqual(self.store.configuration, saved)

    def test_custom_upstream_secret_is_encrypted_retained_and_can_be_reset(self):
        data = self.form()
        data["upstream_token"] = "custom-upstream-secret"
        self.assertEqual(self.client.post(self.url, data=data).status_code, 200)
        self.assertNotIn("custom-upstream-secret", json.dumps(self.store.configuration))
        router = router_from_settings(self.store.load(), self.store, lambda: None)
        self.assertEqual(router.accounts[0].service.audiomuse.headers, {"Authorization": "Bearer custom-upstream-secret"})
        data = self.form()
        data["use_default_upstream"] = "on"
        self.assertEqual(self.client.post(self.url, data=data).status_code, 200)
        self.assertEqual(self.store.configuration["upstream_encrypted"], "")

    def test_stale_form_is_rejected_instead_of_overwriting_newer_settings(self):
        stale = self.form()
        self.save()
        # A newly signed token keeps the test focused on the configuration revision.
        latest = self.form()
        stale["csrf_token"] = latest["csrf_token"]
        result = self.client.post(self.url, data=stale)
        self.assertEqual(result.status_code, 400)
        self.assertIn("another tab", result.text)
        self.assertEqual(self.store.save_count, 1)

    def test_missing_key_fails_closed_and_is_not_silently_replaced(self):
        _, tokens = self.save()
        self.store.box.path.unlink()
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 503)
        self.assertFalse(self.store.box.path.exists())
        with patch.dict("os.environ", {"AM_PREF_TOKEN": "legacy"}):
            api = self.client.get("/plugins/ratings_sieve/api/similar_tracks?item_id=seed",
                                 headers={"Authorization": "Bearer " + tokens["listener_one"]})
        self.assertEqual(api.status_code, 503)

    def test_profile_names_are_html_escaped(self):
        data = self.form()
        data["p." + self.first + ".username"] = '<script>alert("x")</script>'
        response = self.client.post(self.url, data=data)
        self.assertNotIn('<script>alert("x")</script>', response.text)
        self.assertIn("&lt;script&gt;", response.text)

    def test_static_assets_are_served_and_page_points_to_them(self):
        page = self.client.get(self.url)
        for filename in ("settings.js", "settings.css"):
            url = "/plugins/ratings_sieve/static/" + filename
            self.assertIn(url, page.text)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            response.close()


class EncryptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.box = SecretBox(Path(self.tmp.name) / ".key")

    def test_encryption_is_randomized_purpose_bound_and_persistent(self):
        one = self.box.encrypt("profile-one", "private")
        two = self.box.encrypt("profile-one", "private")
        self.assertNotEqual(one, two)
        reopened = SecretBox(self.box.path)
        self.assertEqual(reopened.decrypt("profile-one", one), "private")
        with self.assertRaises(ConfigurationError):
            self.box.decrypt("profile-two", one)
        self.assertNotIn("private", one)

    def test_corrupt_ciphertext_and_keys_fail_closed(self):
        encrypted = self.box.encrypt("account", "private")
        with self.assertRaises(ConfigurationError):
            self.box.decrypt("account", encrypted[:-5])
        self.box.path.write_bytes(b"bad-key")
        with self.assertRaises(ConfigurationError):
            self.box.decrypt("account", encrypted)

    def test_concurrent_key_creation_never_overwrites_the_key(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as pool:
            values = list(pool.map(lambda _: self.box.key(create=True), range(16)))
        self.assertEqual(len(set(values)), 1)
        self.assertEqual(len(values[0]), 32)


class ConnectionAdapterTests(unittest.TestCase):
    def test_navidrome_ping_uses_account_authentication(self):
        http = Mock()
        http.get.return_value = {"subsonic-response": {"status": "ok"}}
        Navidrome("http://nd", "person", "secret", http=http).check_connection()
        self.assertEqual(http.get.call_args.args[1]["u"], "person")
        self.assertNotIn("secret", str(http.get.call_args))
        http.get.return_value = {"subsonic-response": {"status": "failed"}}
        with self.assertRaises(BackendError):
            Navidrome("http://nd", api_key="key", http=http).check_connection()

    def test_audiomuse_health_uses_upstream_authentication(self):
        http = Mock()
        http.get.side_effect = [{"status": "ok"}, {"servers": [{"name": "Navidrome"}]}]
        AudioMuse("http://am", "Navidrome", "token", http).check_connection()
        self.assertEqual(http.get.call_args.args[2], {"Authorization": "Bearer token"})

    def test_audiomuse_connection_uses_real_http_routes_and_checks_auth(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading

        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?")[0]
                seen.append(path)
                if path == "/api/health":
                    payload, status = {"status": "ok"}, 200
                elif path == "/api/servers":
                    payload, status = {"servers": [{"name": "Navidrome"}]}, 200
                    if self.headers.get("Authorization") != "Bearer valid":
                        payload, status = {"error": "unauthorized"}, 401
                else:
                    payload, status = {"error": "not found"}, 404
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = "http://127.0.0.1:" + str(server.server_port)
            AudioMuse(url, "Navidrome", "valid").check_connection()
            self.assertEqual(seen, ["/api/health", "/api/servers"])
            with self.assertRaises(BackendError):
                AudioMuse(url, "Navidrome", "invalid").check_connection()
            with self.assertRaises(BackendError):
                AudioMuse(url, "Missing server", "valid").check_connection()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
