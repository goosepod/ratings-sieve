# Sonic Sieve

Preference-aware Sonic Similar filtering for AudioMuse AI and Navidrome.
Repository: https://github.com/goosepod/sonic-sieve

Development prototype; no automatic installation or production configuration changes. This is an opt-in endpoint, not an interceptor.

The initial policy uses minimum **3 stars**, **exclude** below threshold,
**include unrated**, **boost hearts**, and a **4x candidate pool**.
Missing `userRating` or zero means unrated. A heart never overrides exclusion.
Boosting means stable promotion of eligible hearts before other candidates,
preserving sonic order within each group and leaving distance/metadata untouched.
Policies are typed Python configuration in this version; the environment configures
backends and credentials, not policy overrides.

## Verified integration and scope

AudioMuse 3.7.0 supports `register(ctx)` and `ctx.add_blueprint()` mounted at
`/plugins/<plugin_id>`. It exposes no supported similarity response filter hook.
This plugin therefore adds:

`GET /plugins/sonic_sieve/api/similar_tracks`

The response is the same JSON array shape as core `/api/similar_tracks`.
The plugin calls that original endpoint over HTTP with `n * 4` and a fixed
`server=Navidrome`, then reads each candidate's Navidrome `getSong` preferences.
It does not read or write Navidrome SQLite during normal operation.

The existing Navidrome AudioMuse plugin v11 appends `/api/similar_tracks` to its
configured `apiUrl`, so a future test can point `apiUrl` to the plugin prefix.
Its request does **not** carry listener identity. This version uses one explicitly
configured Navidrome account for every request authenticated to this endpoint.
It must not be presented as automatic per-listener filtering. Do not redirect a
shared Navidrome installation without resolving that limitation.

Other AudioMuse routes are deliberately absent. Changing the plugin base URL can
break Artist Radio, Find Sonic Path, Lyrics, Hyperbolic and DCLAP modes. An initial
trial should call this endpoint directly in isolation. A later full integration
needs explicit forwarding for the other operations or a Navidrome plugin change
that selects a separate URL just for Similar Song.

## Local development

```sh
cd ~/Projects/sonic-sieve
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python tools/build_plugin.py
```

For an isolated loopback trial, set the variables in `.env.example` in your shell.
The application does not automatically load `.env`. Use your own Navidrome account
or API key; no credentials have been copied into this repository. Then:

```sh
.venv/bin/flask --app sonic_sieve.web:create_app run --host 127.0.0.1 --port 5436
```

The standalone development route is `/api/similar_tracks`. Set an Authorization
header to `Bearer <AM_PREF_TOKEN>` when calling it. Missing token disables it (503).
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

`plugin.json` is a development manifest. The archive in `dist/` contains only
plugin code at its root. Publishing still needs a catalogue and a reachable
`sourceUrl`/checksum in release metadata. No catalogue is served automatically.

## Bounds and failure behavior

- Requests accept `n=1..100` (default 50); candidate pool is capped at 400.
- Duplicate candidate IDs and the explicit seed ID are removed before lookups.
- Preserve `item_id`, title/artist, duplicate, radius and mood parameters;
  select the configured AudioMuse server so returned IDs are provider IDs.
- Lookups use one read-only `getSong` per unique candidate, sequentially, with
  per-call timeout up to 5 seconds and a 20-second overall lookup budget including
  the candidate fetch. Large pools may time out; this is a development limitation.
  Socket timeouts are not a hard process-level deadline.
- A lookup/auth/backend failure returns 502, not an unrated fallback. Insufficient
  eligible candidates returns a shorter list or an empty array without relaxing
  the threshold. No additional refill requests are made.
- No persistent preference cache, background jobs, ratings writes, playlist writes,
  production monkey patches, container restarts, or service changes.

## Next implementation steps

1. Run a direct isolated trial using the intended listener's credentials, measuring
   the default 200-candidate workload and empty-result handling.
2. Add bounded concurrency and a short cache keyed by server, account, and song;
   test expiry and rating/heart changes before tuning performance.
3. Choose either an explicitly single-account integration or extend the Navidrome
   plugin/host contract to propagate a verified listener context. Never trust a
   freely supplied username as authentication.
4. Add a separate Similar Song preference URL to the Navidrome plugin so its other
   operations continue to use core. Test against production-equivalent Navidrome;
   use a separate test instance with matching versions and library mappings.
5. Package a catalogue release and add settings UI/secret storage if desired.
   Installing, reconfiguring production, and restarting services require approval.

See [compatibility notes](docs/compatibility.md) for verified interfaces and evidence.

Validation completed: 21 automated tests and a read-only live 12-candidate trial
returning HTTP 200 with three eligible tracks. The live trial uses the existing
analysis account, not an automatically detected listener. The installation-specific smoke helper and private server inventory are excluded from this public repository.
