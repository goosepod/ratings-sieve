"""Additive AudioMuse blueprint and isolated development application."""

import hmac
import os

from flask import Blueprint, Flask, jsonify, request

from .service import AudioMuse, BackendError, FilterService, Navidrome

PLUGIN_ID = "sonic_sieve"
ALLOWED = {"item_id", "title", "artist", "n", "server", "eliminate_duplicates",
           "radius_similarity", "mood_similarity"}


def service_from_env():
    return FilterService(
        AudioMuse(os.environ["AM_PREF_AUDIOMUSE_URL"], os.environ["AM_PREF_SERVER"],
                  os.environ.get("AM_PREF_UPSTREAM_TOKEN", "")),
        Navidrome(os.environ["AM_PREF_NAVIDROME_URL"],
                  os.environ.get("AM_PREF_NAVIDROME_USER", ""),
                  os.environ.get("AM_PREF_NAVIDROME_PASSWORD", ""),
                  os.environ.get("AM_PREF_NAVIDROME_API_KEY", "")))


def create_blueprint(service=None, token=None):
    bp = Blueprint(PLUGIN_ID, __name__)

    @bp.get("/api/similar_tracks")
    def similar_tracks():
        secret = token if token is not None else os.environ.get("AM_PREF_TOKEN", "")
        if not secret:
            return jsonify(error="preference endpoint is not configured"), 503
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied.encode(), ("Bearer " + secret).encode()):
            return jsonify(error="authentication required"), 401
        try:
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
            backend = service or service_from_env()
            if params.get("server", backend.audiomuse.server) != backend.audiomuse.server:
                raise ValueError("server does not match configured preference account")
            params.pop("server", None)
            result = backend.similar(params, count)
            return jsonify(result)
        except ValueError as error:
            return jsonify(error=str(error)), 400
        except KeyError:
            return jsonify(error="preference endpoint is not configured"), 503
        except BackendError:
            return jsonify(error="preference backend unavailable"), 502

    return bp


def create_app(service=None, token=None):
    app = Flask(__name__)
    app.register_blueprint(create_blueprint(service, token))
    return app
