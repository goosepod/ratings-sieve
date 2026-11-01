"""AudioMuse settings storage, encrypted credentials, and admin form handling."""

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import uuid

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from flask import g, jsonify, make_response, render_template, request

from .accounts import Account, AccountRouter, ConfigurationError
from .cache import CacheError
from .core import Policy
from .service import AudioMuse, BackendError, BypassService, FilterService, Navidrome, validate_url

COOKIE = "ratings_sieve_csrf"
ID_PATTERN = re.compile(r"[a-f0-9]{32}")


class SettingsStorageError(ConfigurationError):
    pass


class SecretBox:
    def __init__(self, path):
        self.path = Path(path)

    def key(self, create=False):
        try:
            key = self.path.read_bytes()
        except FileNotFoundError:
            if not create:
                raise ConfigurationError("Saved credentials need the original encryption key or replacement credentials.") from None
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".ratings_sieve-key-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "wb") as output:
                    os.chmod(temporary, 0o600)
                    output.write(AESGCM.generate_key(bit_length=256))
                    output.flush()
                    os.fsync(output.fileno())
                try:
                    os.link(temporary, self.path)
                except FileExistsError:
                    pass
            finally:
                Path(temporary).unlink(missing_ok=True)
            key = self.path.read_bytes()
        if len(key) != 32:
            raise ConfigurationError("The encryption key is invalid; restore its backup.")
        return key

    def encrypt(self, purpose, value):
        nonce = os.urandom(12)
        encrypted = AESGCM(self.key(create=True)).encrypt(nonce, value.encode(), purpose.encode())
        return "aesgcm:v1:" + base64.urlsafe_b64encode(nonce + encrypted).decode()

    def decrypt(self, purpose, value):
        try:
            if not value.startswith("aesgcm:v1:"):
                raise ValueError()
            payload = base64.urlsafe_b64decode(value[len("aesgcm:v1:"):])
            return AESGCM(self.key()).decrypt(payload[:12], payload[12:], purpose.encode()).decode()
        except Exception:
            raise ConfigurationError("Saved credentials could not be decrypted; restore the key or replace them.") from None

    def csrf(self):
        nonce = secrets.token_urlsafe(32)
        signature = hmac.new(self.key(create=True), ("csrf:" + nonce).encode(), hashlib.sha256).hexdigest()
        return nonce + "." + signature

    def valid_csrf(self, value):
        try:
            nonce, signature = value.split(".")
            expected = hmac.new(self.key(), ("csrf:" + nonce).encode(), hashlib.sha256).hexdigest()
            return bool(nonce) and hmac.compare_digest(signature, expected)
        except (ValueError, ConfigurationError, OSError):
            return False


class AudioMuseSettings:
    """Use the public plugin facade, not core tables or shared credentials."""

    @property
    def box(self):
        from plugin.api import config
        return SecretBox(Path(config.PLUGINS_DIR) / ".ratings_sieve.key")

    def load(self):
        try:
            from plugin.api import get_setting
            return get_setting("configuration", None)
        except Exception:
            raise SettingsStorageError("Could not load Ratings Sieve settings from the database.") from None

    def save(self, configuration):
        try:
            from plugin.api import set_setting
            set_setting("configuration", configuration)
        except Exception:
            raise SettingsStorageError("Could not save Ratings Sieve settings to the database.") from None

    def servers(self):
        try:
            from plugin.api import list_servers
            return [s for s in list_servers() if s.get("server_type") == "navidrome" and s.get("enabled", True)]
        except Exception:
            raise SettingsStorageError("Could not load AudioMuse's music servers.") from None

    def default_upstream_token(self):
        from plugin.api import config
        return getattr(config, "API_TOKEN", "") or ""

    def is_admin(self):
        from plugin.api import config
        return not getattr(config, "AUTH_ENABLED", True) or getattr(g, "auth_role", None) == "admin"

    def render(self, body):
        from plugin.api import render_page
        return render_page(body, title="Ratings Sieve Settings")


def defaults():
    return {"version": 1, "revision": "", "audiomuse_url": "http://127.0.0.1:8000",
            "server_id": "", "cache_ttl": 60, "upstream_encrypted": "", "profiles": []}


def selected_server(store, server_id):
    for server in store.servers():
        if server.get("server_id") == server_id:
            return server
    raise ConfigurationError("Select an available Navidrome server configured in AudioMuse.")


def router_from_settings(configuration, store, cache_factory):
    try:
        if not isinstance(configuration, dict) or configuration.get("version") != 1:
            raise ValueError()
        server = selected_server(store, configuration["server_id"])
        upstream = (store.box.decrypt("upstream", configuration["upstream_encrypted"])
                    if configuration.get("upstream_encrypted") else store.default_upstream_token())
        am = AudioMuse(configuration["audiomuse_url"], server["name"], upstream)
        ttl = configuration["cache_ttl"]
        if type(ttl) is not int or not 0 <= ttl <= 86400:
            raise ValueError()
        cache = cache_factory() if ttl else None
        accounts = []
        for profile in configuration["profiles"]:
            policy = Policy(**profile["policy"])
            if type(profile["enabled"]) is not bool:
                raise ValueError()
            if profile["enabled"]:
                credential = store.box.decrypt(profile["id"] + ":credential", profile["credential_encrypted"])
                nd = Navidrome(server["creds"]["url"], profile["username"],
                               credential if profile["credential_type"] == "password" else "",
                               credential if profile["credential_type"] == "api_key" else "")
                backend = FilterService(am, nd, policy, cache=cache, cache_ttl=ttl)
            else:
                backend = BypassService(am)
            if not re.fullmatch(r"[a-f0-9]{64}", profile["token_hash"]):
                raise ValueError()
            accounts.append(Account(profile["username"], profile["token_hash"], backend, token_hashed=True))
        return AccountRouter(accounts)
    except (KeyError, TypeError, ValueError, ConfigurationError, OSError):
        raise ConfigurationError("Saved Ratings Sieve settings are unavailable or invalid.") from None


def _integer(form, name, minimum, maximum):
    try:
        value = int(form[name])
        if not minimum <= value <= maximum:
            raise ValueError()
        return value
    except (KeyError, ValueError):
        raise ValueError("Enter a valid rating, candidate multiplier, and cache lifetime.") from None


def configuration_from_form(form, old, store):
    if form.get("revision", "") != old.get("revision", ""):
        raise ValueError("Settings changed in another tab. Refresh before saving.")
    ids = form.getlist("profile_id")
    if not 1 <= len(ids) <= 50 or len(set(ids)) != len(ids) or any(not ID_PATTERN.fullmatch(i) for i in ids):
        raise ValueError("Add at least one account; a maximum of 50 accounts is supported.")
    server = selected_server(store, form.get("server_id", ""))
    result = defaults()
    result.update(revision=uuid.uuid4().hex, server_id=server["server_id"],
                  audiomuse_url=validate_url(form.get("audiomuse_url", "")),
                  cache_ttl=_integer(form, "cache_ttl", 0, 86400))
    result["upstream_encrypted"] = old.get("upstream_encrypted", "")
    if form.get("use_default_upstream") == "on":
        result["upstream_encrypted"] = ""
    elif form.get("upstream_token"):
        result["upstream_encrypted"] = store.box.encrypt("upstream", form["upstream_token"])
    previous = {p["id"]: p for p in old.get("profiles", [])}
    issued, names = {}, set()
    for profile_id in ids:
        prefix = "p." + profile_id + "."
        username = form.get(prefix + "username", "").strip()
        if not username or len(username) > 128 or username.casefold() in names:
            raise ValueError("Each account needs a unique Navidrome username, up to 128 characters.")
        names.add(username.casefold())
        prior = previous.get(profile_id, {})
        credential_type = form.get(prefix + "credential_type", "password")
        if credential_type not in {"password", "api_key"}:
            raise ValueError("Choose a password or API key for the account.")
        encrypted = prior.get("credential_encrypted", "")
        if credential_type != prior.get("credential_type", credential_type):
            encrypted = ""
        credential = form.get(prefix + "credential", "")
        if credential:
            encrypted = store.box.encrypt(profile_id + ":credential", credential)
        enabled = form.get(prefix + "enabled") == "on"
        if enabled and not encrypted:
            raise ValueError("Enabled accounts need a Navidrome password or API key.")
        policy = Policy(minimum_rating=_integer(form, prefix + "minimum_rating", 1, 5),
                        include_unrated=form.get(prefix + "include_unrated") == "on",
                        boost_hearts=form.get(prefix + "boost_hearts") == "on",
                        candidate_multiplier=_integer(form, prefix + "candidate_multiplier", 1, 10))
        token_hash = prior.get("token_hash", "")
        if not token_hash or form.get(prefix + "rotate_token") == "on":
            token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            issued[username] = token
        result["profiles"].append({"id": profile_id, "username": username, "enabled": enabled,
            "credential_type": credential_type, "credential_encrypted": encrypted,
            "token_hash": token_hash, "policy": {
                "minimum_rating": policy.minimum_rating, "include_unrated": policy.include_unrated,
                "boost_hearts": policy.boost_hearts, "candidate_multiplier": policy.candidate_multiplier}})
    # Validate saved credentials and policy before committing any configuration.
    router_from_settings(result, store, lambda: None)
    return result, issued


def register_settings(bp, store, cache_factory):
    @bp.route("/settings", methods=["GET", "POST"])
    def settings():
        if not store.is_admin():
            return "Administrator access required.", 403
        configuration, issued, message, error, status = defaults(), {}, "", "", 200
        try:
            saved = store.load()
            configuration = saved if saved is not None else defaults()
            if not isinstance(configuration, dict):
                raise ConfigurationError("Saved settings are invalid; restore a valid configuration.")
            # Never silently replace an encryption key after settings were saved.
            store.box.key(create=saved is None)
            if request.method == "POST":
                supplied = request.form.get("csrf_token", "")
                cookie = request.cookies.get(COOKIE, "")
                if not supplied or not hmac.compare_digest(supplied.encode(), cookie.encode()) or not store.box.valid_csrf(supplied):
                    return "The form expired. Refresh the settings page and try again.", 403
                action = request.form.get("action", "save")
                if action == "clear_cache":
                    count = cache_factory().clear()
                    message = f"Cleared {count} cached preferences. They will refresh on the next request."
                elif action in {"save", "test"}:
                    updated, new_tokens = configuration_from_form(request.form, configuration, store)
                    if action == "test":
                        router = router_from_settings(updated, store, lambda: None)
                        router.accounts[0].service.audiomuse.check_connection()
                        tested = 0
                        for account in router.accounts:
                            if isinstance(account.service, FilterService):
                                account.service.navidrome.check_connection()
                                tested += 1
                        message = f"AudioMuse and {tested} enabled Navidrome account(s) connected successfully. No settings were saved."
                    else:
                        store.save(updated)
                        configuration, issued = updated, new_tokens
                        message = "Settings saved. They apply to subsequent recommendation requests."
                else:
                    raise ValueError("Unknown settings action.")
                if request.headers.get("X-Requested-With") == "RatingsSieve" and action in {"test", "clear_cache"}:
                    response = jsonify(message=message)
                    response.headers["Cache-Control"] = "no-store"
                    return response
            csrf = store.box.csrf()
            body = render_template("ratings_sieve/settings.html", configuration=configuration,
                servers=store.servers(), issued=issued, message=message, error=error, csrf=csrf)
        except SettingsStorageError as exc:
            error, status = str(exc), 503
        except (ConfigurationError, ValueError) as exc:
            error, status = str(exc), 400
        except (CacheError, BackendError, OSError):
            error, status = "Could not complete the action. Check the connection or encryption-key storage and try again.", 503
        if error and request.headers.get("X-Requested-With") == "RatingsSieve":
            response = jsonify(error=error)
            response.status_code = status
            response.headers["Cache-Control"] = "no-store"
            return response
        if error:
            # Render only saved settings. Submitted credentials are never echoed.
            try:
                store.box.key(create=configuration == defaults())
                csrf = store.box.csrf()
                body = render_template("ratings_sieve/settings.html", configuration=configuration,
                    servers=store.servers(), issued={}, message="", error=error, csrf=csrf)
            except Exception:
                return "Settings are unavailable. Check database and encryption-key storage.", 503
        response = make_response(store.render(body), status)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "same-origin"
        response.set_cookie(COOKIE, csrf, httponly=True, secure=request.is_secure, samesite="Strict", path=request.path)
        return response
