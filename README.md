# Ratings Sieve

Ratings Sieve is an AudioMuse AI plugin that uses your Navidrome star ratings
and hearts to personalize similar-track recommendations. AudioMuse finds songs
by sound; Ratings Sieve filters and orders those candidates using your preferences.

Configure it from **Manage Plugins → Ratings Sieve → Settings**. Add accounts,
choose a policy for each person, and turn filtering on or off independently.
No JSON files or environment variables are required for normal plugin setup.

## What it does

By default, Ratings Sieve:

- Excludes tracks rated **1 or 2 stars**.
- Includes tracks rated **3–5 stars** and tracks you have **not rated**.
- Moves eligible **hearted tracks first**, preserving similarity order within
  the hearted and non-hearted groups. A heart never overrides an exclusion.
- Requests **four times** the desired number of candidates to allow for filtering.

Asking for 10 recommendations requests up to 40 candidates and returns up to 10
eligible tracks. If fewer qualify, the result is shorter. Ratings Sieve reads
your existing Navidrome preferences; it does not change them.

## Install

You need AudioMuse AI with analyzed music and a configured Navidrome server.
The plugin is tested with AudioMuse AI 3.7.0 and Navidrome 0.64.2.

This repository currently provides development packaging rather than a published
installable catalogue. Build the archive:

```sh
git clone https://github.com/goosepod/ratings-sieve.git
cd ratings-sieve
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python tools/build_plugin.py
```

The archive is written to `dist/ratings_sieve-<version>.zip`. Follow AudioMuse's
[local plugin catalogue instructions](https://neptunehub.github.io/AudioMuse-AI/PLUGIN/#test-your-plugin-locally)
using that archive and [plugin.json](plugin.json), then apply the installation
through AudioMuse. Its plugin manager installs the declared encryption dependency
on supported container builds.

## Set up through AudioMuse

1. Open **Manage Plugins → Ratings Sieve → Settings** as an AudioMuse administrator.
2. Select the **Navidrome server** already configured in AudioMuse. If none is
   listed, add one in AudioMuse's music server settings first.
3. Check the **AudioMuse API URL**. It must point to the original AudioMuse API
   reachable from the plugin, commonly `http://127.0.0.1:8000` inside its container.
   Leave the custom API token blank to use AudioMuse's own token automatically.
4. Click **Add account** for each person. Enter their actual Navidrome username
   and choose a password or API key belonging to that account.
5. Turn **Enable filtering** on for accounts that should use their preferences.
   Set their minimum rating, unrated behavior, heart boosting, and candidate
   multiplier. New accounts start with filtering off.
6. Click **Test connections** to check the entered settings without saving them.
7. Click **Save settings**. Copy the generated request token for each new account
   and store it privately for your client or integration. Tokens are shown once.

Settings are stored in AudioMuse's database and apply to subsequent recommendation
requests. The page is administrator-only; an administrator manages all profiles.

### Manage accounts and credentials

- **Disable filtering:** uncheck Enable filtering and save. That account receives
  ordinary AudioMuse recommendations, with no preference lookups or heart boost.
  Disabled accounts need a request token but no Navidrome credentials.
- **Change a password or API key:** enter its replacement and save. Leave the field
  blank to keep the existing credential. Changing credential type requires a new value.
- **Replace a request token:** check Generate a new request token on save. The old
  token stops working after the save; copy its replacement from the result page.
- **Remove an account:** click Remove account and save. Its request token stops working.

Passwords, API keys, and custom upstream tokens are encrypted before saving and
are never displayed again. Request tokens are stored as hashes. Ratings Sieve
automatically creates an encryption key in AudioMuse's persistent plugins directory
as `.ratings_sieve.key`; back up that directory together with the database. Restoring
encrypted credentials requires the matching key.

## Request recommendations

Ratings Sieve provides this endpoint inside AudioMuse:

```text
GET /plugins/ratings_sieve/api/similar_tracks
```

For an AudioMuse installation with authentication enabled, a trusted server-side
integration sends the AudioMuse API token in `Authorization` and the person's
generated request token in `X-Ratings-Sieve-Token`:

```sh
curl --get 'http://YOUR_AUDIOMUSE_HOST:8000/plugins/ratings_sieve/api/similar_tracks' \
  --header "Authorization: Bearer $AUDIOMUSE_API_TOKEN" \
  --header "X-Ratings-Sieve-Token: $RATINGS_SIEVE_ACCOUNT_TOKEN" \
  --data-urlencode 'item_id=YOUR_NAVIDROME_TRACK_ID' \
  --data-urlencode 'n=10'
```

Keep the AudioMuse API token on the trusted integration server, since it grants
administrative access. When AudioMuse authentication is disabled, the person's
request token alone can be sent as `Authorization: Bearer <token>`.

The response is a JSON array with AudioMuse's track metadata and distance values.
You can use `title` and `artist` instead of `item_id`. `n` accepts 1–100 and defaults
to 50. Optional `eliminate_duplicates`, `radius_similarity`, and `mood_similarity`
parameters accept `true` or `false`. The candidate pool is capped at 400 tracks.

**Automatic Navidrome integration is still required.** The existing AudioMuse
Navidrome plugin does not send listener identity and cannot yet select each
person's request token automatically. Ratings Sieve's settings page manages
profiles; it does not solve listener identification. Changing that plugin's shared
AudioMuse base URL can also break Artist Radio and other operations. A trusted
integration must route Similar Song requests to Ratings Sieve with the right token.

## Cache settings

Ratings and hearts are cached in AudioMuse's PostgreSQL database, separately for
each server, account, and track. Fresh values are read together; only missing or
expired entries are fetched from Navidrome. The cache survives restarts.

Set **Cache lifetime** on the settings page:

| Value | Behavior |
| --- | --- |
| `60` | Default: reuse cached preferences for up to one minute. |
| `0` | Fetch directly from Navidrome without cache reads or writes. |
| `1`–`86400` | Set a cache lifetime in seconds, up to one day. |

Rating changes, cleared ratings, and removed hearts become visible after expiry
and a successful refresh. Policy changes use the current cached values immediately.
Click **Clear cached ratings** to remove all accounts' cached preferences without
changing their settings; later requests fetch fresh values.

If the cache database fails, Ratings Sieve queries Navidrome directly. If a required
Navidrome lookup fails, it returns an error rather than treating a track as unrated
or using expired data. Large requests with an empty cache can hit the lookup time
limit because missing preferences are currently fetched sequentially.

## Standalone and advanced configuration

The standalone Flask application retains environment-variable and profile-file
configuration for development and custom deployments. See
[the standalone guide](docs/standalone.md), [.env.example](.env.example), and
[profiles.example.json](profiles.example.json).

An installed plugin uses legacy environment/file configuration only until settings
are first saved through its page. Saved page settings then take precedence.
Existing file profiles are not automatically imported into the page.

See [compatibility notes](docs/compatibility.md) for implementation and validation
details.
