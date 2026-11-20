# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

`chatwoot-autoreply-bot`: an MIT-licensed Chatwoot Agent Bot. Chatwoot posts signed webhooks to it; it answers
pending conversations through an OpenAI-compatible LLM and hands off to humans by opening the conversation.

- `chatwoot_autoreply_bot/app.py`: FastAPI app, `POST /webhook` (verify, dedupe, record, schedule, 200) and
  `GET /healthz`. Generated docs routes are off. `create_app` takes a services factory for tests.
- `signature.py`: the HMAC check. `events.py`: payload parsing plus the reply-trigger and transcript rules.
- `store.py`: Valkey state (delivery dedupe, transcripts keyed by message id, answered marker, generations,
  outstanding turns, locks).
- `scheduler.py`: per-conversation debounce, lock and turn timeout, resume at startup; a failed turn ends in a
  handoff. `responder.py`'s `hand_off` never raises, so a handoff is never repeated.
- `digits.py`: strict ASCII digit parsing for every id and timestamp from a request.
- `responder.py`: one reply turn; customer messages past the answered marker are unanswered.
- `chatwoot.py`: the Chatwoot REST calls. `llm.py`: the LLM call, sentinel and key probe.
- `llm_router.py`: per-conversation LLM choice (inbox key, then env key) with a per-inbox client cache.
- `connect.py`, `connect_form.py`, `connect_page.py`: the `/connect` key page (routes, form validation, HTML).
  `identity.py`: mPass header normalisation and the admin allowlist. `keystore.py`: Fernet-encrypted per-inbox
  keys in `KEYSTORE_VALKEY_URL`. `inbox_recorder.py`: inboxes seen in webhooks, for the page's list.
- Never log or render an LLM key, an email address or a provider error body.

## Commands

```bash
uv sync
uv run ruff format .
uv run ruff check .
uv run pytest --cov=chatwoot_autoreply_bot --cov-fail-under=80
docker build --platform linux/amd64 .
```

## Rules

- Never call the LLM or Chatwoot inside the webhook request: Chatwoot times out after 5 s and then opens the
  conversation.
- The bot never writes in a conversation that is not pending. Re-read the status before acting.
- Log account and conversation ids and exception class names only, never message content, provider error
  bodies or secrets.
- Do not read, copy or adapt Chatwoot's `enterprise/` code (Captain and other Enterprise features).
- Only MIT, BSD or Apache-2.0 dependencies. `[tool.uv] exclude-newer` keeps a 14-day cooling window: move the
  date forward when re-locking, never past 14 days ago.
- Tests never need a live Chatwoot, LLM or Valkey: use `httpx.MockTransport` and `fakeredis`.

## Branch policy

| Branch | Role |
|---|---|
| `foss-sandbox` | Staging. Everything lands here first. |
| `foss-main` | Production. Updated only by a promotion PR from `foss-sandbox`, as a merge commit. |

Releases are tags: `vYY.MM.PATCH-rc.N` on `foss-sandbox`, `vYY.MM.PATCH` on `foss-main`. Every PR into
`foss-sandbox` adds an entry under **Unreleased** in `appbox-docs/CHANGELOG.md`.

## Commits and PRs

- [Conventional Commits](https://www.conventionalcommits.org/): `type(scope): subject`, imperative, lowercase,
  no trailing period.
- No AI attribution anywhere: no `Co-Authored-By` trailer, no "Generated with" line, in commits or PRs.
- PR descriptions have a `## Description` section and an optional `## Testing` section, written as bullet points.
