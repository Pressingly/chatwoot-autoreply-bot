# Changelog

Changes to `Pressingly/chatwoot-autoreply-bot`, by release tag.

Tags follow the release pipeline: `vYY.MM.N-rc.N` builds the sandbox image, `vYY.MM.N` the prod image.

Every PR into `foss-sandbox` adds its entry under **Unreleased**. When a tag is cut, rename that heading to the
tag and date and start a new **Unreleased** section. Put anything an operator has to act on under
**Behaviour changes**.

## Unreleased

### Added

- First release: a Chatwoot Agent Bot service that verifies signed webhooks on `POST /webhook`, keeps a
  per-conversation transcript in Valkey, debounces bursts into one reply per pending conversation, answers
  through any OpenAI-compatible LLM, and hands off to a human by opening the conversation on request, on the
  LLM's `[[HANDOFF]]` signal or on failure.
  ([FOSS-725](https://pm.moneta.askii.ai/moneta/browse/FOSS-725/))
