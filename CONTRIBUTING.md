# Contributing

Thanks for helping. Issues and pull requests are welcome.

## Reporting a bug

Search the existing issues first. A useful report includes the bot version or commit, the Chatwoot version, the
LLM provider, and the smallest steps that reproduce it, with tokens and customer data removed.

Security issues go through [SECURITY.md](SECURITY.md), not public issues.

## Development setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run ruff format .
uv run ruff check .
uv run pytest --cov=chatwoot_autoreply_bot --cov-fail-under=80
```

CI runs the same checks on every pull request. Tests must not need a live Chatwoot, LLM or Valkey: use
`httpx.MockTransport` and `fakeredis`.

## Branches

| Branch | Role |
|---|---|
| `foss-sandbox` | Staging. Every change lands here first. |
| `foss-main` | Production. Updated only by a promotion PR from `foss-sandbox`. |

Open your pull request against `foss-sandbox` and add a line under **Unreleased** in `appbox-docs/CHANGELOG.md`.

## Commits and pull requests

- Commit subjects follow [Conventional Commits](https://www.conventionalcommits.org/): `type(scope): subject`,
  imperative, lowercase, no trailing period, e.g. `feat(handoff): add a phrase list`.
- Keep pull requests small and focused, with tests for new behaviour.
- New dependencies must be MIT, BSD or Apache-2.0 licensed.

By contributing you agree that your contribution is licensed under the [MIT License](LICENSE).
