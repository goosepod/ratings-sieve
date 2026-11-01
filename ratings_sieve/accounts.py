"""Account-bound routing. An identity is selected only by a verified secret."""

from dataclasses import dataclass, field
import hmac
import json
import os
from pathlib import Path

from .core import Policy
from .service import AudioMuse, BypassService, FilterService, Navidrome
from .cache import SQLitePreferenceCache


class AuthenticationError(Exception):
    pass


class ConfigurationError(Exception):
    pass


@dataclass(frozen=True)
class Account:
    name: str
    token: str = field(repr=False)
    service: object = field(repr=False)


class AccountRouter:
    def __init__(self, accounts, bypass=None, bypass_token=""):
        self.accounts = tuple(accounts)
        self.bypass = bypass
        self.bypass_token = bypass_token
        names, tokens = set(), set()
        for account in self.accounts:
            if (not account.name or not account.token or account.name in names
                    or account.token in tokens or account.token == bypass_token):
                raise ConfigurationError("invalid account configuration")
            names.add(account.name)
            tokens.add(account.token)
        if not self.accounts and not (bypass is not None and bypass_token):
            raise ConfigurationError("no authenticated routes configured")

    def resolve(self, authorization, account_token=None):
        # A dedicated header permits host AudioMuse authentication to use its
        # own Bearer token. Never fall back if that header is present but invalid.
        supplied = account_token if account_token is not None else (
            authorization[7:] if authorization.startswith("Bearer ") else "")
        selected = None
        for account in self.accounts:
            if hmac.compare_digest(supplied.encode(), account.token.encode()):
                selected = account.service
        if selected is not None:
            return selected
        if (account_token is None and self.bypass is not None and self.bypass_token
                and hmac.compare_digest(supplied.encode(), self.bypass_token.encode())):
            return self.bypass
        raise AuthenticationError("authentication required")


def _secret(env, key):
    if not isinstance(key, str) or not key or not env.get(key):
        raise ConfigurationError("missing account secret")
    return env[key]


def _load_json(path):
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ConfigurationError("duplicate configuration key")
            result[key] = value
        return result

    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique_keys)


def router_from_env(env=None, cache_factory=None):
    """Reload the profile file each request so edits and token revocation apply."""
    env = dict(os.environ if env is None else env)
    try:
        cache_ttl = int(env.get("RATINGS_SIEVE_CACHE_TTL_SECONDS", "60"))
        if not 0 <= cache_ttl <= 86400:
            raise ConfigurationError("invalid cache TTL")
        am = AudioMuse(env["AM_PREF_AUDIOMUSE_URL"], env["AM_PREF_SERVER"],
                       env.get("AM_PREF_UPSTREAM_TOKEN", ""))
        cache = (cache_factory() if cache_factory is not None else SQLitePreferenceCache(
            env.get("RATINGS_SIEVE_CACHE_DB", "instance/ratings-sieve-cache.sqlite3"))) if cache_ttl else None
        if "AM_PREF_PROFILES_FILE" not in env:
            # Preserve the explicitly configured single-account mode.
            token = _secret(env, "AM_PREF_TOKEN")
            nd = Navidrome(env["AM_PREF_NAVIDROME_URL"],
                           env.get("AM_PREF_NAVIDROME_USER", ""),
                           env.get("AM_PREF_NAVIDROME_PASSWORD", ""),
                           env.get("AM_PREF_NAVIDROME_API_KEY", ""))
            return AccountRouter([Account("legacy", token, FilterService(am, nd,
                cache=cache, cache_ttl=cache_ttl))])

        config = _load_json(env["AM_PREF_PROFILES_FILE"])
        if (not isinstance(config, dict) or set(config) != {"profiles"}
                or not isinstance(config["profiles"], list)):
            raise ConfigurationError("invalid profile document")
        accounts = []
        for profile in config["profiles"]:
            if (not isinstance(profile, dict)
                    or set(profile) - {"username", "token_env", "enabled", "policy",
                                       "password_env", "api_key_env"}
                    or not isinstance(profile.get("username"), str)
                    or not profile["username"].strip()
                    or type(profile.get("enabled", False)) is not bool):
                raise ConfigurationError("invalid profile")
            token = _secret(env, profile.get("token_env"))
            policy_data = profile.get("policy", {})
            if not isinstance(policy_data, dict):
                raise ConfigurationError("invalid policy")
            policy = Policy(**policy_data)
            if profile.get("enabled", False):
                password_key, api_key = profile.get("password_env"), profile.get("api_key_env")
                if bool(password_key) == bool(api_key):
                    raise ConfigurationError("one credential source required")
                nd = Navidrome(env["AM_PREF_NAVIDROME_URL"], profile["username"],
                               _secret(env, password_key) if password_key else "",
                               _secret(env, api_key) if api_key else "")
                backend = FilterService(am, nd, policy, cache=cache, cache_ttl=cache_ttl)
            else:
                backend = BypassService(am)
            accounts.append(Account(profile["username"], token, backend))
        return AccountRouter(accounts, BypassService(am), env.get("AM_PREF_TOKEN", ""))
    except (OSError, KeyError, TypeError, ValueError, ConfigurationError):
        raise ConfigurationError("preference endpoint is not configured") from None
