# Ratings Sieve

Preference-aware Sonic Similar filtering for AudioMuse AI and Navidrome.
Repository: https://github.com/goosepod/ratings-sieve

Development prototype; no automatic installation or production configuration changes. This is an opt-in endpoint, not an interceptor.

The initial policy uses minimum **3 stars**, **exclude** below threshold,
**include unrated**, **boost hearts**, and a **4x candidate pool**.
Missing `userRating` or zero means unrated. A heart never overrides exclusion.
Boosting means stable promotion of eligible hearts before other candidates,
preserving sonic order within each group and leaving distance/metadata untouched.
Multi-user profiles can override these defaults and independently enable or
disable filtering. The original single-account environment configuration is
still supported when no profile file is selected.

## Verified integration and scope

AudioMuse 3.7.0 supports `register(ctx)` and `ctx.add_blueprint()` mounted at
`/plugins/<plugin_id>`. It exposes no supported similarity response filter hook.
This plugin therefore adds:

`GET /plugins/ratings_sieve/api/similar_tracks`

The response is the same JSON array shape as core `/api/similar_tracks`.
The plugin calls that original endpoint over HTTP with `n * 4` and a fixed
`server=Navidrome`, then checks cached preferences and calls Navidrome `getSong`
only for missing or expired entries.
It does not read or write Navidrome SQLite during normal operation.

The existing Navidrome AudioMuse plugin v11 appends `/api/similar_tracks` to its
configured `apiUrl`, so a future test can point `apiUrl` to the plugin prefix.
Its request does **not** carry listener identity. Ratings Sieve now supports separate
account tokens and policies, but the existing Navidrome plugin cannot automatically
choose the appropriate account token. A client or trusted integration must send
the token bound to the authenticated listener. Do not redirect a shared Navidrome
installation until that listener-aware integration is in place.

Other AudioMuse routes are deliberately absent. Changing the plugin base URL can
break Artist Radio, Find Sonic Path, Lyrics, Hyperbolic and DCLAP modes. An initial
trial should call this endpoint directly in isolation. A later full integration
needs explicit forwarding for the other operations or a Navidrome plugin change
that selects a separate URL just for Similar Song.

## Local development

```sh
cd ~/Projects/ratings-sieve
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python tools/build_plugin.py
```

For an isolated loopback trial, set the variables in `.env.example` in your shell.
The application does not automatically load `.env`. Use your own Navidrome account
or API key; no credentials have been copied into this repository. Then:

```sh
.venv/bin/flask --app ratings_sieve.web:create_app run --host 127.0.0.1 --port 5436
```

The standalone development route is `/api/similar_tracks`. In single-account mode,
set an Authorization header to `Bearer <AM_PREF_TOKEN>` when calling it. Missing
configuration disables it (503). For separate accounts, use the profile setup below.
Credentials are account-bound server configuration; arbitrary user/server selectors
are rejected. Password authentication uses OpenSubsonic salted token authentication.
Use trusted networking or HTTPS for remote backends; redirects are rejected.

Inside AudioMuse, use `AM_PREF_AUDIOMUSE_URL=http://127.0.0.1:8000` for the original
core API, never this plugin's prefix (which would recurse). Both URLs are required
explicitly; they must address the same catalogue/library mapping. If host AudioMuse
authentication is enabled, its application API token must also satisfy the host
guard; set `AM_PREF_TOKEN` to that token for this initial plugin version, and set
`AM_PREF_UPSTREAM_TOKEN` to it for requests to core. A standalone trial can use a
separate inbound token. Secrets belong in private environment configuration.

## Separate accounts and independent on/off settings

1. Copy `profiles.example.json` to a private `profiles.json` and replace its
   usernames with the actual Navidrome usernames. Set `AM_PREF_PROFILES_FILE` to
   the absolute path to that file. The exact filenames `profiles.json` and
   `profiles.local.json` are Git-ignored; keep any differently named private files
   outside the checkout.
2. Set the token and credential environment variables referenced by each profile.
   Generate a different strong random request token for each person. Tokens and
   passwords/API keys are looked up by environment-variable name, not stored in
   the JSON. An enabled profile needs exactly one of `password_env` or
   `api_key_env`; the credentials must belong to that profile's account.
3. Set `enabled: true` for your account. Set `enabled: false` for an account that
   should receive ordinary AudioMuse recommendations. Omitted `enabled` defaults
   to false. Disabled accounts need a request token but no Navidrome credentials.
4. Call the endpoint with `Authorization: Bearer <that person's token>`. Ratings
   Sieve selects the profile from the verified token. No username query parameter
   or username header can select another profile.

For example, the supplied JSON enables `listener_one` with the default 3-star
threshold and disables `listener_two`. You can enable both with different policies:

```json
{
  "username": "listener_two",
  "enabled": true,
  "token_env": "RATINGS_SIEVE_LISTENER_TWO_TOKEN",
  "password_env": "RATINGS_SIEVE_LISTENER_TWO_PASSWORD",
  "policy": {"minimum_rating": 4, "include_unrated": false, "boost_hearts": false}
}
```

That is one profile entry within the document's `profiles` array. Missing policy
fields retain the original defaults. All profiles use the explicitly configured
AudioMuse server and Navidrome URL; neither can be overridden by a request.

Disabled profiles call AudioMuse with the requested count, return its track order
and metadata unchanged, and perform no oversampling or preference reads. Disabling
one account never changes another. Cached preferences are isolated by account;
recommendation result lists are not cached.

In multi-user mode, `AM_PREF_TOKEN` is an **optional unfiltered bypass token** for
a trusted integration that has no configured listener profile. It never selects
a person's preferences. Missing/unknown credentials return 401; they do not use
that fallback. An unconfigured listener can receive ordinary recommendations
through the trusted integration's bypass route, or be given a disabled profile.
The legacy single-account Navidrome credentials are ignored in multi-user mode.

When installed inside AudioMuse with host authentication enabled, a trusted
server-side integration must send the host API token in `Authorization` and the
person's token in `X-Ratings-Sieve-Token`. The latter identifies the Ratings Sieve
profile. Invalid or empty explicit profile headers return 401, even when the
Bearer token would permit bypass. Keep the host API token on the trusted server;
it grants AudioMuse administrative access and must not be distributed to listeners.
For standalone use, the per-person Bearer token is sufficient.

Profile documents are read for each request, so policy changes, toggles, and token
reference changes take effect on subsequent requests without restarting. Save
edits atomically; invalid JSON, duplicate usernames/tokens, missing secrets or
invalid policy values return 503 rather than silently reverting to one account.
An in-flight request finishes using the settings it already loaded. Changes to
process environment variables still require restarting that development process.
This version has no settings UI.

## Persistent database cache

When installed in AudioMuse, Ratings Sieve uses AudioMuse's PostgreSQL database
through `plugin.api`. The install hook creates its own namespaced table,
`plugin_ratings_sieve__preferences`, without modifying core tables. Database rows
survive application/container restarts and plugin upgrades as long as AudioMuse's
database is retained. Normal AudioMuse uninstall behavior controls whether plugin
tables are kept or removed. No companion Navidrome plugin is required.

The cache stores a server/catalogue identity, an account identity, track ID,
rating (including zero), heart flag, and fetch timestamp. Account names are used
when supplied; a legacy API-key-only account uses a SHA-256 key fingerprint as
its identity. Credentials and request tokens are never stored in the table.
Treat this database as private user preference data.

For each recommendation request:

1. Read all fresh candidate preferences in one database query.
2. Fetch missing or expired values from Navidrome for the selected account.
3. Save the successfully fetched values in one transaction and apply the policy.

`RATINGS_SIEVE_CACHE_TTL_SECONDS` defaults to **60** seconds. Valid values are
0..86400; 0 disables cache reads and writes. A rating or heart change may take
until the entry expires to affect recommendations. Clearing a rating and removing
a heart are saved on the next successful refresh. Changing filtering settings
uses existing fresh preferences immediately and does not require a cache reset.
Concurrent older fetches cannot overwrite entries with newer fetch timestamps.

Unrated/unhearted responses are cached, but API errors are not. Expired entries
are never used as a fallback: a failed Navidrome refresh still returns 502. Cache
read/write failures fall back to direct API values, so cache downtime does not
change the rating policy. Disabled accounts bypass database reads and writes.
If a later lookup fails or times out, earlier successful lookups are still saved
so subsequent attempts can reuse that work; the failed request still returns 502.
The cache retains expired rows for future refresh; automatic pruning and an admin
clear-cache control are not implemented in this version.

Standalone development uses SQLite instead of AudioMuse's database. Set
`RATINGS_SIEVE_CACHE_DB` to a persistent file path; the default is
`instance/ratings-sieve-cache.sqlite3`, relative to the working directory. The
database is created lazily on the first enabled preference lookup. Keep it on a
persistent private volume if using a standalone container. This setting is
ignored when running as an installed AudioMuse plugin.

### Renaming from Sonic Sieve

The repository/project name is now `ratings-sieve`; the Python package and
AudioMuse plugin ID are `ratings_sieve`. The plugin URL is now
`/plugins/ratings_sieve/api/similar_tracks`, and the explicit account header is
`X-Ratings-Sieve-Token`. Update client URLs/header names and profile environment
variable references (`RATINGS_SIEVE_LISTENER_*` in the example). Existing
`AM_PREF_*` variables are unchanged. No old plugin deployment is automatically
reconfigured or restarted, and the previous version had no database to migrate.

`plugin.json` is a development manifest. The archive in `dist/` contains only
plugin code at its root. Publishing still needs a catalogue and a reachable
`sourceUrl`/checksum in release metadata. No catalogue is served automatically.

## Bounds and failure behavior

- Requests accept `n=1..100` (default 50); candidate pool is capped at 400.
- Duplicate candidate IDs and the explicit seed ID are removed before lookups.
- Preserve `item_id`, title/artist, duplicate, radius and mood parameters;
  select the configured AudioMuse server so returned IDs are provider IDs.
- Cache misses use one read-only `getSong` per unique candidate, sequentially, with
  per-call timeout up to 5 seconds and a 20-second overall lookup budget including
  the candidate fetch. Large pools may time out; this is a development limitation.
  Socket timeouts are not a hard process-level deadline.
- A lookup/auth/backend failure returns 502, not an unrated fallback. Insufficient
  eligible candidates returns a shorter list or an empty array without relaxing
  the threshold. No additional refill requests are made.
- No background jobs, ratings writes, playlist writes,
  production monkey patches, container restarts, or service changes.

## Next implementation steps

1. Run a direct isolated trial using the intended listener's credentials, measuring
   the default 200-candidate workload and empty-result handling.
2. Measure the persistent cache hit rate and add bounded concurrency for misses
   if needed; add cache pruning and an admin clear-cache action.
3. Extend the Navidrome plugin/host contract to identify the authenticated listener
   and select their Ratings Sieve token. The endpoint already supports per-user
   policies and disabled profiles; automatic listener propagation is still needed.
4. Add a separate Similar Song preference URL to the Navidrome plugin so its other
   operations continue to use core. Test against production-equivalent Navidrome;
   use a separate test instance with matching versions and library mappings.
5. Package a catalogue release and add settings UI/secret storage if desired.
   Installing, reconfiguring production, and restarting services require approval.

See [compatibility notes](docs/compatibility.md) for verified interfaces and evidence.

Validation covers independent user policies, disabled-account bypass, token-bound
identity, simultaneous requests, policy/toggle reload, and credential separation,
alongside persistent-cache tests for expiry, clearing, batched reads, restart
persistence, account/server isolation, database outages and failed API calls.
The original read-only live 12-candidate
trial returned HTTP 200 with three eligible tracks for one configured account;
the multi-user tests use synthetic accounts and perform no production mutations.
