import unittest
from unittest.mock import Mock, patch

from ratings_sieve.core import Policy, Preference, select, unique_candidates
from ratings_sieve.service import AudioMuse, BackendError, FilterService, Navidrome
from ratings_sieve.web import create_app


def track(item_id):
    return {"item_id": item_id, "distance": 0.3, "title": "Example"}


class PolicyTests(unittest.TestCase):
    def test_defaults_and_threshold_including_hearts(self):
        items = [track(str(i)) for i in range(6)]
        prefs = {str(i): Preference(i, i == 2) for i in range(6)}
        self.assertEqual([t["item_id"] for t in select(items, prefs, 10)], ["0", "3", "4", "5"])
        self.assertEqual(Policy().candidate_multiplier, 4)

    def test_hearts_promoted_stably_without_changing_distance(self):
        items = [track("a"), track("b"), track("c"), track("d")]
        prefs = {key: Preference(3, key in {"b", "d"}) for key in "abcd"}
        result = select(items, prefs, 3)
        self.assertEqual([t["item_id"] for t in result], ["b", "d", "a"])
        self.assertEqual(result[0], items[1])
        self.assertEqual(items[0]["item_id"], "a")

    def test_options(self):
        items = [track("a"), track("b")]
        prefs = {"a": Preference(0, True), "b": Preference(5)}
        self.assertEqual(select(items, prefs, 5, Policy(include_unrated=False)), [items[1]])
        self.assertEqual(select(items, prefs, 5, Policy(boost_hearts=False)), items)

    def test_missing_lookup_is_not_unrated(self):
        with self.assertRaises(KeyError):
            select([track("a")], {}, 1)

    def test_invalid_policy(self):
        for kwargs in ({"minimum_rating": 0}, {"candidate_multiplier": 0},
                       {"minimum_rating": True}, {"below_threshold": "boost"},
                       {"include_unrated": "false"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Policy(**kwargs)

    def test_deduplicate_and_remove_seed(self):
        self.assertEqual(unique_candidates([track("seed"), track("a"), track("a")], "seed"), [track("a")])


class AdapterTests(unittest.TestCase):
    def test_song_preferences_and_token_auth(self):
        http = Mock()
        http.get.return_value = {"subsonic-response": {"status": "ok", "song": {
            "id": "a", "userRating": 2, "starred": "2026-01-01T00:00:00Z"}}}
        client = Navidrome("http://navidrome:4533", "listener", "secret", http=http)
        self.assertEqual(client.preference("a"), Preference(2, True))
        params = http.get.call_args.args[1]
        self.assertNotIn("p", params)
        self.assertNotIn("secret", str(params))
        self.assertEqual(params["u"], "listener")

    def test_unrated_absence_and_zero(self):
        self.assertEqual(Preference.from_song({}), Preference())
        self.assertEqual(Preference.from_song({"userRating": 0}), Preference())
        for value in (None, "3", -1, 6, True):
            with self.assertRaises(ValueError):
                Preference.from_song({"userRating": value})

    def test_api_failures_do_not_become_unrated(self):
        http = Mock()
        for payload in ({"subsonic-response": {"status": "failed"}}, {},
                        {"subsonic-response": {"status": "ok", "song": {"id": "wrong"}}}):
            http.get.return_value = payload
            with self.assertRaises(BackendError):
                Navidrome("http://nd", api_key="secret", http=http).preference("a")

    def test_server_scope_and_upstream_shape(self):
        http = Mock()
        http.get.return_value = [track("a")]
        client = AudioMuse("http://am", "Navidrome", http=http)
        client.candidates({"item_id": "seed", "eliminate_duplicates": "true"}, 12)
        self.assertEqual(http.get.call_args.args[1]["server"], "Navidrome")
        self.assertEqual(http.get.call_args.args[1]["n"], 12)
        http.get.return_value = {"error": "failure"}
        with self.assertRaises(BackendError):
            client.candidates({}, 12)

    def test_unsafe_urls_rejected(self):
        for url in ("file:///data", "http://user:pass@host", "http://host?token=secret"):
            with self.assertRaises(ValueError):
                AudioMuse(url, "Navidrome")


class ServiceTests(unittest.TestCase):
    def test_oversample_filter_and_shortfall(self):
        am, nd = Mock(), Mock()
        am.candidates.return_value = [track("seed"), track("a"), track("a"), track("b")]
        nd.preference.side_effect = [Preference(1, True), Preference(0)]
        result = FilterService(am, nd).similar({"item_id": "seed"}, 3)
        am.candidates.assert_called_once_with({"item_id": "seed"}, 12)
        self.assertEqual(result, [track("b")])
        self.assertEqual(nd.preference.call_count, 2)

    def test_lookup_failure_fails_request(self):
        am, nd = Mock(), Mock()
        am.candidates.return_value = [track("a")]
        nd.preference.side_effect = BackendError("failed")
        with self.assertRaises(BackendError):
            FilterService(am, nd).similar({"item_id": "seed"}, 1)

    def test_invalid_candidate_is_backend_failure(self):
        am, nd = Mock(), Mock()
        am.candidates.return_value = [{"title": "missing id"}]
        with self.assertRaises(BackendError):
            FilterService(am, nd).similar({"item_id": "seed"}, 1)
        nd.preference.assert_not_called()

    def test_deadline_and_count_limit(self):
        am, nd = Mock(), Mock()
        am.candidates.return_value = [track("a")]
        with self.assertRaises(BackendError):
            FilterService(am, nd, budget_seconds=0).similar({"item_id": "seed"}, 1)
        nd.preference.assert_not_called()
        for count in (0, 101, -1, True):
            with self.assertRaises(ValueError):
                FilterService(am, nd).similar({}, count)


class WebTests(unittest.TestCase):
    def setUp(self):
        self.service = Mock()
        self.service.audiomuse.server = "Navidrome"
        self.service.similar.return_value = [track("a")]
        self.client = create_app(self.service, "secret").test_client()
        self.headers = {"Authorization": "Bearer secret"}

    def test_compatible_json_contract(self):
        response = self.client.get("/api/similar_tracks?item_id=seed&n=3", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, [track("a")])
        self.service.similar.assert_called_once_with({"item_id": "seed"}, 3)

    def test_auth_required(self):
        self.assertEqual(self.client.get("/api/similar_tracks?item_id=seed").status_code, 401)
        self.service.similar.assert_not_called()

    def test_invalid_parameters(self):
        for query in ("item_id=seed&n=0", "item_id=seed&n=101", "item_id=seed&n=bad",
                      "item_id=seed&server=Other", "item_id=seed&user=someone", "n=3",
                      "item_id=a&item_id=b", "item_id=seed&boost_hearts=false",
                      "item_id=seed&mood_similarity=1"):
            with self.subTest(query=query):
                self.assertEqual(self.client.get("/api/similar_tracks?" + query, headers=self.headers).status_code, 400)
        self.service.similar.assert_not_called()

    def test_backend_error_redacted(self):
        self.service.similar.side_effect = BackendError("secret URL")
        response = self.client.get("/api/similar_tracks?item_id=seed", headers=self.headers)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("secret", response.text)

    def test_disabled_without_token(self):
        client = create_app(self.service, "").test_client()
        self.assertEqual(client.get("/api/similar_tracks?item_id=seed").status_code, 503)

    def test_audio_muse_registration(self):
        from ratings_sieve import register
        from flask import Flask
        ctx = Mock()
        register(ctx)
        ctx.on_install.assert_called_once()
        bp = ctx.add_blueprint.call_args.args[0]
        self.assertEqual(bp.name, "ratings_sieve")
        app = Flask("plugin-test")
        app.register_blueprint(bp, url_prefix="/plugins/ratings_sieve")
        with patch.dict("os.environ", {"AM_PREF_TOKEN": ""}):
            self.assertEqual(app.test_client().get("/plugins/ratings_sieve/api/similar_tracks").status_code, 503)


if __name__ == "__main__":
    unittest.main()
