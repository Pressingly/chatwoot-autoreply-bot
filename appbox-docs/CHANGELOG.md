# Changelog

Changes to `Pressingly/chatwoot-autoreply-bot`, by release tag.

Tags follow the release pipeline: `vYY.MM.N-rc.N` builds the sandbox image, `vYY.MM.N` the prod image.

Every PR into `foss-sandbox` adds its entry under **Unreleased**. When a tag is cut, rename that heading to the
tag and date and start a new **Unreleased** section. Put anything an operator has to act on under
**Behaviour changes**.

## Unreleased

### Fixed

- Startup again refuses a bot with no LLM source: a keystore counts only while `CONNECT_ADMIN_MPASS_IDS` turns
  the page on, so a host that only generated `KEY_ENCRYPTION_KEY` no longer starts and hands off every
  conversation. `CONNECT_TRUST_PREFERRED_USERNAME` reads any value but `true`, `1` or `yes` as off instead of
  refusing to start, matching how mpass-auth-proxy reads `LAUNCHPAD_EMAIL_CAPTURE`.
  ([FOSS-829](https://pm.moneta.askii.ai/moneta/browse/FOSS-829/))

### Added

- First release: a Chatwoot Agent Bot service that verifies signed webhooks on `POST /webhook`, keeps a
  per-conversation transcript in Valkey, debounces bursts into one reply per pending conversation, answers
  through any OpenAI-compatible LLM, and hands off to a human by opening the conversation on request, on the
  LLM's `[[HANDOFF]]` signal or on failure. Unauthenticated bodies are capped at `MAX_BODY_BYTES`, the LLM
  call and every Chatwoot call are time-boxed inside the lock lifetime, outstanding turns resume after a restart, and `/healthz` checks Valkey.
  ([FOSS-725](https://pm.moneta.askii.ai/moneta/browse/FOSS-725/))
- Per-inbox LLM keys: admins on `CONNECT_ADMIN_MPASS_IDS` paste a base URL, model and key for an inbox at
  `/connect` (mPass identity from `X-Auth-Request-Email`, CSRF token plus `Origin` check). The key is checked with
  one small completion, encrypted with `KEY_ENCRYPTION_KEY` and stored in `KEYSTORE_VALKEY_URL`. Replies use the
  inbox key, then the env key, else hand off with a private note; a rejected key hands off without retrying.
  A stored key is used only while its host is still on `LLM_BASE_URL_ALLOWLIST`, and LLM calls never follow
  redirects. With `CONNECT_TRUST_PREFERRED_USERNAME=true` (email-capture hosts only), an all-digit
  `X-Auth-Request-Preferred-Username` beside a real email header is matched as the admin's mPass id, so bare-id
  allowlists work there too.
  ([FOSS-829](https://pm.moneta.askii.ai/moneta/browse/FOSS-829/))

### Behaviour changes

- `LLM_API_KEY` and `LLM_MODEL` are now optional and act as the fallback for inboxes without a pasted key. The
  bot still refuses to start with neither an env LLM nor a keystore, or with `LLM_API_KEY` but no `LLM_MODEL`; a
  lone `LLM_MODEL` is ignored. Turning on `CONNECT_ADMIN_MPASS_IDS` needs
  `KEY_ENCRYPTION_KEY`, `KEYSTORE_VALKEY_URL` (an `appendonly`, `noeviction` Valkey), `PUBLIC_BASE_URL` and
  `CONNECT_EDGE_SECRET`, and Traefik must route `/connect` through mpass-auth and send the edge secret. ([FOSS-829](https://pm.moneta.askii.ai/moneta/browse/FOSS-829/))
