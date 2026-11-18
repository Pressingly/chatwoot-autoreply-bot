# Security Policy

## Reporting a vulnerability

Please report security issues privately, not in a public issue or pull request.

- Use GitHub private vulnerability reporting: open the repository's **Security** tab and choose
  **Report a vulnerability**, or go to
  <https://github.com/Pressingly/chatwoot-autoreply-bot/security/advisories/new>.
- If that option is not available, open an issue titled "Security contact request" with no details, and a
  maintainer will reply with a private channel.

Include the affected version or commit, the Chatwoot version, and the steps to reproduce it.

We aim to acknowledge a report within three business days and keep you updated until it is fixed. Please give us a
reasonable time to release a fix before disclosing publicly.

## In scope

- Getting the bot to post, or to change a conversation, from a request Chatwoot did not sign.
- Replaying a signed webhook outside the timestamp window, or getting one delivery processed twice.
- Leaking message content, the bot token, the bot secret or the LLM key into logs or responses.
- Getting the bot to write in a conversation that is not pending.
- Exhausting memory or crashing the service with an unauthenticated request.

## Deployment notes

- **Only `POST /webhook` should be public.** Route nothing else from the internet; `/healthz` is for the
  container probe.
- **Keep the Agent Bot secret set.** Chatwoot signs webhooks only when the bot has one, and the bot refuses every
  unsigned request.
- **The bot token acts for the Agent Bot**: it can read and update conversations and post messages in the inboxes
  the bot is connected to. Treat it like an agent's token.
- **Keep Valkey private.** Anyone who can write to the bot's Valkey database can change what the bot remembers
  and when it replies.

## Out of scope

- Prompt injection that only makes the LLM say something off-topic or wrong. Mitigate with the system prompt and
  `HANDOFF_PHRASES`; the bot has no tools beyond posting a reply and handing off.
- Issues in Chatwoot or the LLM provider; report those to them.
