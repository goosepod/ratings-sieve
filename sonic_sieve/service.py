"""Read-only HTTP clients. Credentials stay in memory and out of exceptions."""

import hashlib
import json
import secrets
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .core import Policy, Preference, select, unique_candidates


class BackendError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("invalid backend URL")
    return url.rstrip("/")


class JsonHTTP:
    def get(self, url, params, headers=None, timeout=5):
        request = Request(url + "?" + urlencode(params), headers=headers or {})
        try:
            with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
                raw = response.read(8_000_001)
                if len(raw) > 8_000_000:
                    raise ValueError("response too large")
                return json.loads(raw)
        except Exception:
            raise BackendError("backend request failed") from None


class AudioMuse:
    def __init__(self, url, server, token="", http=None):
        self.url = validate_url(url)
        self.server = server
        self.headers = {"Authorization": "Bearer " + token} if token else {}
        self.http = http or JsonHTTP()

    def candidates(self, params, count):
        query = dict(params, n=count, server=self.server)
        result = self.http.get(self.url + "/api/similar_tracks", query, self.headers, timeout=10)
        if not isinstance(result, list) or not all(isinstance(t, dict) for t in result):
            raise BackendError("invalid similarity response")
        return result[:count]


class Navidrome:
    def __init__(self, url, username="", password="", api_key="", http=None):
        self.url = validate_url(url)
        if not api_key and not (username and password):
            raise ValueError("Navidrome account credentials required")
        self.username, self.password, self.api_key = username, password, api_key
        self.http = http or JsonHTTP()

    def preference(self, item_id, timeout=5):
        params = {"v": "1.16.1", "c": "sonic-sieve", "f": "json", "id": item_id}
        if self.api_key:
            params["apiKey"] = self.api_key
        else:
            salt = secrets.token_hex(16)
            params.update(u=self.username, s=salt,
                          t=hashlib.md5((self.password + salt).encode(), usedforsecurity=False).hexdigest())
        result = self.http.get(self.url + "/rest/getSong.view", params, timeout=timeout)
        try:
            envelope = result["subsonic-response"]
            if envelope["status"] != "ok":
                raise ValueError("API error")
            song = envelope["song"]
            if song["id"] != item_id:
                raise ValueError("wrong song")
            return Preference.from_song(song)
        except (KeyError, TypeError, ValueError):
            raise BackendError("invalid preference response") from None


class FilterService:
    def __init__(self, audiomuse, navidrome, policy=Policy(), budget_seconds=20):
        self.audiomuse, self.navidrome, self.policy = audiomuse, navidrome, policy
        self.budget_seconds = budget_seconds

    def similar(self, params, count):
        if type(count) is not int or not 1 <= count <= 100:
            raise ValueError("n must be 1..100")
        deadline = time.monotonic() + self.budget_seconds
        raw = self.audiomuse.candidates(params, min(count * self.policy.candidate_multiplier, 400))
        try:
            candidates = unique_candidates(raw, params.get("item_id"))
        except ValueError:
            raise BackendError("invalid similarity candidate") from None
        preferences = {}
        for track in candidates:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BackendError("preference lookup budget exceeded")
            preferences[track["item_id"]] = self.navidrome.preference(
                track["item_id"], timeout=min(5, remaining))
        if time.monotonic() > deadline:
            raise BackendError("preference lookup budget exceeded")
        return select(candidates, preferences, count, self.policy)
