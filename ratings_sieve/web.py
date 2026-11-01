"""Additive AudioMuse blueprint and isolated development application."""

import os

from flask import Blueprint, Flask, jsonify, request

from .accounts import Account, AccountRouter, AuthenticationError, ConfigurationError, router_from_env
from .service import BackendError

PLUGIN_ID = "ratings_sieve"
ALLOWED = {"item_id", "title", "artist", "n", "server", "eliminate_duplicates",
           "radius_similarity", "mood_similarity"}


def create_blueprint(service=None, token=None, router=None, cache_factory=None, settings_store=None):
    bp = Blueprint(PLUGIN_ID, __name__, template_folder="templates", static_folder="static")
    if settings_store is not None:
        from .settings import register_settings
        register_settings(bp, settings_store, cache_factory)

    @bp.get("/api/similar_tracks")
    def similar_tracks():
        try:
            if service is not None:
                secret = token if token is not None else os.environ.get("AM_PREF_TOKEN", "")
                if not secret:
                    raise ConfigurationError("preference endpoint is not configured")
                routes = AccountRouter([Account("legacy", secret, service)])
            else:
                saved = settings_store.load() if settings_store is not None else None
                if saved is not None:
                    from .settings import router_from_settings
                    routes = router_from_settings(saved, settings_store, cache_factory)
                else:
                    routes = router if router is not None else router_from_env(cache_factory=cache_factory)
            backend = routes.resolve(request.headers.get("Authorization", ""),
                                     request.headers.get("X-Ratings-Sieve-Token"))
            if any(key in request.headers for key in ("X-Ratings-Sieve-User", "X-Ratings-Sieve-Username")):
                raise ValueError("username headers are not supported; use an account token")
            if set(request.args) - ALLOWED or any(len(request.args.getlist(k)) != 1 for k in request.args):
                raise ValueError("unknown or repeated parameter")
            params = request.args.to_dict()
            count = int(params.pop("n", "50"))
            if not 1 <= count <= 100:
                raise ValueError("n must be 1..100")
            if not params.get("item_id") and not (params.get("title") and params.get("artist")):
                raise ValueError("item_id or title and artist required")
            for key in ("eliminate_duplicates", "radius_similarity", "mood_similarity"):
                if key in params and params[key] not in {"true", "false"}:
                    raise ValueError("invalid boolean parameter")
            if params.get("server", backend.audiomuse.server) != backend.audiomuse.server:
                raise ValueError("server does not match configured preference account")
            params.pop("server", None)
            result = backend.similar(params, count)
            return jsonify(result)
        except ValueError as error:
            return jsonify(error=str(error)), 400
        except AuthenticationError:
            return jsonify(error="authentication required"), 401
        except ConfigurationError:
            return jsonify(error="preference endpoint is not configured"), 503
        except BackendError:
            return jsonify(error="preference backend unavailable"), 502

    return bp


def create_app(service=None, token=None, router=None):
    app = Flask(__name__)
    app.register_blueprint(create_blueprint(service, token, router))
    return app
