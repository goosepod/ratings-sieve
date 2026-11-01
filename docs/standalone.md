# Standalone development

Use Python 3.11 or newer. From the repository, create a virtual environment and
install with `python -m pip install -e .`. On Windows, use
`.venv\Scripts\python.exe`; the examples below use Bash and `.venv/bin/python`.

Set these variables in the process environment, using your own URLs and account:

```sh
export AM_PREF_AUDIOMUSE_URL='http://127.0.0.1:5435'
export AM_PREF_SERVER='Navidrome'
export AM_PREF_NAVIDROME_URL='http://127.0.0.1:4533'
export AM_PREF_NAVIDROME_USER='your-navidrome-username'
export AM_PREF_NAVIDROME_PASSWORD='your-navidrome-password'
export AM_PREF_TOKEN="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
.venv/bin/python -m flask --app ratings_sieve.web:create_app run --host 127.0.0.1 --port 5436
```

Set `AM_PREF_UPSTREAM_TOKEN` if AudioMuse needs an API token. Set
`AM_PREF_NAVIDROME_API_KEY` instead of a password to use a Navidrome API key.
The application does not automatically load `.env`.

Call `/api/similar_tracks?item_id=TRACK_ID&n=10` with
`Authorization: Bearer <AM_PREF_TOKEN>`. The standalone application does not expose
the AudioMuse admin settings page.

For several accounts, copy `profiles.example.json` to a private `profiles.json`,
set `AM_PREF_PROFILES_FILE` to its absolute path, and set the environment variables
named by each profile. Use actual Navidrome usernames and unique account tokens.
Enabled profiles need exactly one of `password_env` or `api_key_env`; disabled
profiles need neither. Omitted `enabled` defaults to false. Request with the
person's token in Authorization. In multi-user mode, `AM_PREF_TOKEN` becomes an
optional unfiltered bypass token; unset it to accept profile tokens only.

Policy fields are `minimum_rating` (1–5), `include_unrated`, `boost_hearts`, and
`candidate_multiplier` (1–10). `below_threshold` supports only `exclude`.
Profile changes apply to subsequent requests; process environment changes need a restart.

Standalone caching uses SQLite at `instance/ratings-sieve-cache.sqlite3`, or the
path in `RATINGS_SIEVE_CACHE_DB`. `RATINGS_SIEVE_CACHE_TTL_SECONDS` accepts 0–86400,
default 60. Keep private configuration, database files, and secrets out of Git.
The exact filenames `profiles.json` and `profiles.local.json` are ignored.

Run tests with `.venv/bin/python -m unittest discover -s tests -v`.
