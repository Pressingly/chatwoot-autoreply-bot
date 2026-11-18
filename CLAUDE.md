# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

`chatwoot-autoreply-bot`: an MIT-licensed Chatwoot Agent Bot. Chatwoot posts signed webhooks to it; it answers
pending conversations through an OpenAI-compatible LLM and hands off to humans by opening the conversation.

- `chatwoot_autoreply_bot/app.py`: FastAPI app, `POST /webhook` (verify, dedupe, record, schedule, 200) and
  `GET /healthz`. Generated docs routes are off. `create_app` takes a services factory for tests.
- `signature.py`: the HMAC check. `events.py`: payload parsing plus the reply-trigger and transcript rules.
- `store.py`: Valkey state (delivery dedupe, transcripts keyed by message id, generations, locks).
- `scheduler.py`: per-conversation debounce and lock; any failure in a turn ends in a handoff.
- `responder.py`: one reply turn. `chatwoot.py`: the Chatwoot REST calls. `llm.py`: the LLM call and sentinel.

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
- Only MIT, BSD or Apache-2.0 dependencies.
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
