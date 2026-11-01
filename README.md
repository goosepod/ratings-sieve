# Ratings Sieve

Ratings Sieve is an AudioMuse AI plugin that uses your Navidrome star ratings
and hearts to personalize similar-track recommendations. AudioMuse finds songs
by sound; Ratings Sieve filters and orders those candidates using your preferences.

Each person can have separate settings, and filtering can be enabled for one
account and disabled for another. Ratings and hearts are cached in a database to
reduce repeated requests to Navidrome.

## What it does

By default, Ratings Sieve:

- Excludes tracks rated **1 or 2 stars**.
- Includes tracks rated **3–5 stars** and tracks you have **not rated**.
- Moves eligible **hearted tracks first**, preserving AudioMuse's similarity order
  within the hearted and non-hearted groups. A heart never overrides an exclusion.
- Requests **four times** the desired number of candidates to allow for filtering.

For example, asking for 10 recommendations requests up to 40 candidates and
returns up to 10 eligible tracks. If fewer qualify, the result is shorter.
Ratings Sieve reads your existing Navidrome preferences; it does not change them.

## Getting started

You need an AudioMuse installation with analyzed music and a configured Navidrome
server, plus credentials for the Navidrome account whose preferences you want to
use. The plugin has been tested with AudioMuse AI 3.7.0 and Navidrome 0.64.2.
Standalone use requires Python 3.11 or newer.

Ratings Sieve currently provides an endpoint that clients or integrations call
explicitly. **It does not automatically replace Navidrome's Sonic Similar results.**
The existing AudioMuse Navidrome plugin does not send the listener's identity;
automatic per-listener use needs an integration that selects the correct person's
Ratings Sieve token.

### Run a local trial

The following commands use Bash:

```sh
git clone https://github.com/goosepod/ratings-sieve.git
cd ratings-sieve
python3 -m venv .venv
.venv/bin/python -m pip install -e .

export AM_PREF_AUDIOMUSE_URL='http://127.0.0.1:5435'
export AM_PREF_SERVER='Navidrome'
export AM_PREF_NAVIDROME_URL='http://127.0.0.1:4533'
export AM_PREF_NAVIDROME_USER='your-navidrome-username'
export AM_PREF_NAVIDROME_PASSWORD='your-navidrome-password'
export AM_PREF_TOKEN="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"

.venv/bin/python -m flask --app ratings_sieve.web:create_app run --host 127.0.0.1 --port 5436
```

Replace the URLs and credentials with your own. `AM_PREF_SERVER` must match the
server name configured in AudioMuse. If AudioMuse requires an API token, also set
`AM_PREF_UPSTREAM_TOKEN` to that token. To use a Navidrome API key instead of a
password, set `AM_PREF_NAVIDROME_API_KEY`.

On Windows, use `.venv\Scripts\python.exe` instead of `.venv/bin/python`; in
PowerShell, set environment variables with `$env:VARIABLE_NAME = 'value'`.
See [.env.example](.env.example) for all environment variables. The application
does not load that file or a `.env` file automatically. Keep real credentials in
private environment configuration.

With the application running, send a request from another shell using the same
token value:

```sh
curl --get 'http://127.0.0.1:5436/api/similar_tracks' \
  --header "Authorization: Bearer $AM_PREF_TOKEN" \
  --data-urlencode 'item_id=YOUR_NAVIDROME_TRACK_ID' \
  --data-urlencode 'n=10'
```

The response is a JSON array of recommended tracks in preference order, retaining
AudioMuse's track metadata and distance values. You can use `title` and `artist`
instead of `item_id`. `n` accepts 1–100 and defaults to 50. The endpoint also accepts
`eliminate_duplicates`, `radius_similarity`, and `mood_similarity` as `true` or `false`.

### Use it inside AudioMuse

Build the plugin archive from the checkout:

```sh
.venv/bin/python tools/build_plugin.py
```

The archive is written to `dist/ratings_sieve-<version>.zip`. This repository
currently supplies development packaging; it does not yet publish an installable
plugin catalogue. To install it, follow AudioMuse's
[local plugin catalogue instructions](https://neptunehub.github.io/AudioMuse-AI/PLUGIN/#test-your-plugin-locally),
using the archive and [plugin.json](plugin.json).

Set the configuration in AudioMuse's environment. The installed endpoint is:

```text
/plugins/ratings_sieve/api/similar_tracks
```

Set `AM_PREF_AUDIOMUSE_URL` to the original AudioMuse API base URL, such as
`http://127.0.0.1:8000` when it is reachable inside the same container. Do not point
it back to the Ratings Sieve endpoint. The Navidrome URL must also be reachable
from the AudioMuse container.

When AudioMuse authentication is enabled, a trusted server-side integration sends
its AudioMuse API token in `Authorization` and the person's Ratings Sieve token in
`X-Ratings-Sieve-Token`. Set `AM_PREF_UPSTREAM_TOKEN` for Ratings Sieve's calls to
AudioMuse. Keep the AudioMuse API token on the trusted server, since it grants
administrative access. Standalone use only needs the person's Bearer token.

Changing the existing Navidrome plugin's shared AudioMuse base URL can break Artist
Radio and other operations. Route Similar Song requests specifically to Ratings
Sieve; automatic integration with the existing plugin remains to be implemented.

## Separate settings for each person

Copy [profiles.example.json](profiles.example.json) to a private `profiles.json`,
and set `AM_PREF_PROFILES_FILE` to its absolute path. Use actual Navidrome usernames.
For example:

```json
{
  "profiles": [
    {
      "username": "listener_one",
      "enabled": true,
      "token_env": "RATINGS_SIEVE_LISTENER_ONE_TOKEN",
      "password_env": "RATINGS_SIEVE_LISTENER_ONE_PASSWORD",
      "policy": {
        "minimum_rating": 3,
        "include_unrated": true,
        "boost_hearts": true,
        "candidate_multiplier": 4
      }
    },
    {
      "username": "listener_two",
      "enabled": false,
      "token_env": "RATINGS_SIEVE_LISTENER_TWO_TOKEN"
    }
  ]
}
```

Set the environment variables named by each profile. Each person needs a unique,
strong random request token. An enabled profile also needs that person's Navidrome
password through `password_env`, or API key through `api_key_env`; configure exactly
one credential source. The JSON contains environment-variable names, not secrets.

The example enables filtering for `listener_one` and leaves it off for
`listener_two`. Disabled profiles return ordinary AudioMuse recommendations,
without heart boosting, oversampling, or preference database/API lookups. They
need a request token but no Navidrome credentials. Omitting `enabled` defaults to off.

Send requests with `Authorization: Bearer <that person's token>` for standalone
use, or the `X-Ratings-Sieve-Token` header described above for an authenticated
AudioMuse installation. The token selects the account; supplying a username does
not select or authenticate a profile.

Policy fields you omit use the defaults. `minimum_rating` accepts 1–5,
`candidate_multiplier` accepts 1–10, and the pool is capped at 400 tracks.
`below_threshold` currently supports only `exclude`.

Profile changes take effect on subsequent requests. Environment-variable changes
require restarting the process. In multi-user mode, the single-account Navidrome
credentials are ignored. `AM_PREF_TOKEN`, if set, becomes a separate unfiltered
bypass token for a trusted integration; unset it if you only want profile tokens.
Unknown or missing tokens are rejected. Keep private profile files out of Git;
`profiles.json` and `profiles.local.json` are already ignored.

## Database caching

Installed in AudioMuse, Ratings Sieve creates its own preferences table in
AudioMuse's PostgreSQL database. Standalone use stores the cache in SQLite at
`instance/ratings-sieve-cache.sqlite3`, or the path set by `RATINGS_SIEVE_CACHE_DB`.
Keep that file on persistent storage when running standalone in a container.

Cached values are separated by server, account, and track. Fresh preferences are
read together; only missing or expired values are fetched from Navidrome. The
cache survives restarts and stores ratings, heart flags, and timestamps—not
passwords or request tokens.

Set `RATINGS_SIEVE_CACHE_TTL_SECONDS` to control freshness:

| Value | Behavior |
| --- | --- |
| `60` | Default: reuse cached preferences for up to one minute. |
| `0` | Query Navidrome directly without cache reads or writes. |
| `1`–`86400` | Set a cache lifetime in seconds, up to one day. |

Rating changes, cleared ratings, and removed hearts become visible on the next
successful refresh after expiry. Filtering-policy changes use cached preferences
immediately. If the database is unavailable, Ratings Sieve queries Navidrome
directly. If a required Navidrome lookup fails, the request returns an error
rather than treating the track as unrated or using expired data.

## Current limits

Settings are configured through environment variables and profile files; there is
no settings screen yet. Preference cache misses are fetched sequentially, so large
cold requests can hit the lookup time limit. Expired cache rows are retained for
refresh; automatic pruning and a clear-cache control are not yet available.

See [compatibility notes](docs/compatibility.md) for implementation and validation
details.
