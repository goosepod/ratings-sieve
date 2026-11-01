# Compatibility and verification

Sonic Sieve is a development plugin tested with AudioMuse AI 3.7.0,
Navidrome 0.64.2, and the AudioMuse Navidrome plugin 11.0.0.

## Supported extension point

AudioMuse's plugin context supports register(ctx) and ctx.add_blueprint().
Blueprints are mounted at /plugins/<plugin_id>. The inspected version has no
supported hook to filter an existing similarity response, so Sonic Sieve adds
/plugins/sonic_sieve/api/similar_tracks without modifying the core endpoint.

The original GET /api/similar_tracks endpoint accepts a seed ID or title/artist,
result count, server selection, and duplicate/radius/mood flags. Its serializer
scopes results to the selected media server. Keep server selection aligned with
the Navidrome account so item_id values identify the right library's tracks.

The existing Navidrome AudioMuse plugin appends /api/similar_tracks to apiUrl.
Its SonicSimilarity request contains Song and Count, with no listener identity.
The HTTP call also carries no listener identity. Sonic Sieve supports independent
profiles selected by distinct authenticated request tokens. It does not infer the
current Navidrome listener from an unverified username. A trusted integration must
associate the authenticated listener with the correct token. A separate header,
X-Sonic-Sieve-Token, permits that integration to retain AudioMuse host authentication
in the Authorization header. The host API token must remain on the trusted server.

A separate Similar Song URL in the Navidrome plugin is needed before redirecting
that operation without affecting Artist Radio and other similarity modes.

## Ratings and hearts

Live read-only getSong calls verified that userRating contains the authenticated
account's rating and starred indicates a heart. Unrated/unhearted songs can omit
both fields. Rating zero also means unrated. averageRating is not used.
Annotated samples verified rated songs with and without hearts.

## Validation

- Automated policy, adapter, service, endpoint, registration and multi-user tests.
  Multi-user coverage includes both accounts enabled, one disabled, separate
  Navidrome credentials, independent policies, concurrent requests, configuration
  reload, token revocation, spoofed usernames, and invalid-configuration failures.
- A bounded read-only integration trial against a real installation returned
  HTTP 200: 12 candidates requested, 12 preferences read, 3 eligible tracks
  returned. Assertions checked rating eligibility and heart ordering.
  This earlier live trial verified one configured account; multi-user behavior
  is validated with synthetic accounts, not production account credentials.
- A code-only archive was built and its plugin registration verified.
- No production plugin installation, ratings or playlist writes, service restart,
  or production configuration changes were performed.
- Private server inventory, local account paths, and installation-specific
  credential-discovery helpers are intentionally excluded from public source.

## Primary references

- https://neptunehub.github.io/AudioMuse-AI/PLUGIN/
- https://github.com/NeptuneHub/AudioMuse-AI-NV-plugin/tree/05b6408ec4b3f09a865a7dea1e258620cc2b8227
- https://opensubsonic.netlify.app/docs/endpoints/getsong/

These notes describe the inspected versions; newer releases may differ.
