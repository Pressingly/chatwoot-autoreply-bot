# chatwoot-autoreply-bot

A small Chatwoot [Agent Bot](https://www.chatwoot.com/docs/product/others/agent-bots) that answers customers with
any OpenAI-compatible LLM (OpenAI, an Askii key, a local gateway) and hands the conversation to a human when it
should. It is a separate service that talks to Chatwoot only through its webhook and REST API.

## How it works

1. Chatwoot posts every event of a bot-connected inbox to `POST /webhook`, signed with the Agent Bot's secret.
2. The bot refuses bodies over `MAX_BODY_BYTES` (1 MiB) before reading them, checks the signature (HMAC-SHA256
   over `"<timestamp>.<raw body>"`, at most 300 s old), drops repeated
   deliveries by `X-Chatwoot-Delivery`, records the message in a per-conversation transcript in Valkey and answers
   `200` straight away. The LLM never runs inside the request; Chatwoot gives a bot webhook five seconds.
3. Only a public, incoming `message_created` in a **pending** conversation triggers a reply. Agent replies, the
   bot's own replies and messages in open conversations only extend the transcript. Private notes are ignored.
4. Replies are debounced per conversation (`DEBOUNCE_SECONDS`, default 3): a burst of customer messages gets one
   answer that sees all of them. A per-conversation lock means two replies never run at once. The LLM call is
   capped at `LLM_TIMEOUT_SECONDS` x (`LLM_MAX_RETRIES` + 1) + 10 s (past that the bot hands off), each Chatwoot
   call at `CHATWOOT_TIMEOUT_SECONDS`, and the lock lasts longer than the worst case of all of them.
5. Before answering, the bot re-reads the conversation status and stays silent unless it is still pending. It
   shows typing, calls the LLM with the system prompt plus the transcript, checks the status again, and posts.
6. The bot remembers the newest customer message each reply (or handoff, or human agent reply) has answered,
   writing that down just before it posts. A message that arrives while the LLM is still working gets its own
   reply right after.

### Handoff

The bot posts `HANDOFF_MESSAGE` and sets the conversation to **open**, so it lands with your agents, when:

- the customer's unanswered messages contain a `HANDOFF_PHRASES` entry, such as "talk to a human";
- the LLM answers with the `[[HANDOFF]]` sentinel (the prompt tells it to when it is asked for a human, the
  customer is upset, or it cannot answer). The sentinel is never posted;
- the LLM call fails after its retries, returns nothing, or the reply turn fails for any other reason;
- no LLM is configured for the conversation's inbox, or its key is rejected.

When the reason is an LLM key problem, the bot also posts a **private note** for the agents: the inbox has no key
(with a link to `<PUBLIC_BASE_URL>/connect`), the provider rejected the key or model (401, 403 or 404, never
retried), the stored key cannot be read, or its host is no longer on `LLM_BASE_URL_ALLOWLIST`.

The note is posted once; opening the conversation is retried three times. If every attempt fails the bot logs an
error and the conversation stays pending.

The bot never writes in an open, resolved or snoozed conversation. Set the conversation back to pending to hand
it back to the bot.

## Setup in Chatwoot

1. **Settings > Bots > Add bot** (or `POST /api/v1/accounts/{id}/agent_bots`). Set the webhook URL to
   `https://<bot-host>/webhook` (in the Moneta server bundle, `https://chatwoot-autoreply.<domain>/webhook`).
2. Copy the bot's **access token** into `CHATWOOT_BOT_TOKEN` and its **secret** into `CHATWOOT_BOT_SECRET`.
   Chatwoot only signs webhooks when the bot has a secret; unsigned requests are refused.
3. **Settings > Inboxes > (inbox) > Bot configuration**: select the bot. New conversations in that inbox start as
   pending and go to the bot first.
4. If Chatwoot reaches the bot on a private address, Chatwoot's own SSRF guard (`SafeFetch`) refuses the webhook
   unless `SAFE_FETCH_ALLOW_PRIVATE_NETWORK=true`. A public URL that resolves to the same host avoids that.

The bot token may read a conversation, toggle its status and typing indicator, and create messages. It cannot
list messages, which is why the bot keeps its own transcript from the webhooks it has seen.

## Configuration

Settings come from the environment only. The service refuses to start if a required one is missing, and also
when it has no LLM source at all (neither `LLM_API_KEY` + `LLM_MODEL` nor a keystore). A lone `LLM_MODEL` is not a
fallback, and a keystore needs both `KEY_ENCRYPTION_KEY` and `KEYSTORE_VALKEY_URL`; with the allowlist empty, only
one of them set leaves the keystore off, while both set keep serving already pasted keys.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `CHATWOOT_BOT_TOKEN` | yes | | Agent Bot access token, sent as `api_access_token` |
| `CHATWOOT_BOT_SECRET` | yes | | Agent Bot secret, verifies `X-Chatwoot-Signature` |
| `VALKEY_URL` | yes | | Valkey/Redis URL for transcripts, dedupe and locks, e.g. `redis://valkey:6379/1` |
| `LLM_API_KEY` | no | | Deployment-wide fallback key for inboxes without a pasted key; set with `LLM_MODEL` |
| `LLM_MODEL` | no | | Fallback model name, e.g. `gpt-4o-mini`; set with `LLM_API_KEY` |
| `LLM_BASE_URL` | no | OpenAI | Fallback OpenAI-compatible `/v1` base URL |
| `CONNECT_ADMIN_MPASS_IDS` | no | empty | Comma-separated mPass ids (or emails) allowed on `/connect`; empty disables the page (404) |
| `KEY_ENCRYPTION_KEY` | with the allowlist | | Fernet key that encrypts pasted LLM keys |
| `KEYSTORE_VALKEY_URL` | with the allowlist | | Durable Valkey (`appendonly yes`, `noeviction`) for pasted keys and the inbox list |
| `PUBLIC_BASE_URL` | with the allowlist | | The bot's public https origin; used for the CSRF `Origin` check and in handoff notes |
| `CONNECT_TRUST_PREFERRED_USERNAME` | no | `false` | `true` only on hosts with email capture on, where mpass-auth-proxy stamps `preferred_username` with the mPass id |
| `CONNECT_EDGE_SECRET` | with the allowlist | | At least 32 characters; Traefik's mPass router sends it as `X-Mpass-Edge-Secret`, so containers that reach the bot directly cannot forge identity headers |
| `LLM_BASE_URL_ALLOWLIST` | no | `api.openai.com,api.askii.ai,api.sandbox.askii.ai,api.anthropic.com` | Hosts a pasted base URL may use (https only) |
| `DEFAULT_EMAIL_DOMAIN` | no | | Synthetic email domain, so `<id>@<domain>` and a bare `<id>` match the same allowlist entry |
| `CHATWOOT_INTERNAL_URL` | no | `http://chatwoot:3000` | Where the bot calls the Chatwoot API |
| `SYSTEM_PROMPT` | no | built in | Prompt for every account |
| `SYSTEM_PROMPT_ACCOUNT_<id>` | no | | Prompt for one Chatwoot account id, replacing `SYSTEM_PROMPT` |
| `HANDOFF_MESSAGE` | no | built in | Note posted before opening the conversation |
| `HANDOFF_PHRASES` | no | built in | Comma-separated, case-insensitive; empty disables the check |
| `DEBOUNCE_SECONDS` | no | `3` | Quiet time before replying to a burst |
| `LLM_TIMEOUT_SECONDS` | no | `30` | Per LLM attempt |
| `LLM_MAX_RETRIES` | no | `2` | Retries after the first LLM attempt |
| `LLM_MAX_TOKENS` | no | unset | Sent as `max_completion_tokens` when set |
| `CHATWOOT_TIMEOUT_SECONDS` | no | `10` | Per Chatwoot API call |
| `SIGNATURE_MAX_SKEW_SECONDS` | no | `300` | Oldest accepted webhook timestamp |
| `DEDUPE_TTL_SECONDS` | no | `86400` | How long a delivery id is remembered |
| `TRANSCRIPT_MAX_MESSAGES` | no | `40` | Messages kept per conversation |
| `TRANSCRIPT_TTL_SECONDS` | no | `604800` | Transcript lifetime after the last message |
| `MAX_BODY_BYTES` | no | `1048576` | Largest webhook body accepted (larger gets 413) |
| `PORT` | no | `8000` | HTTP port |

The built-in prompt keeps the bot on the business's own support and has it decline anything else. Keep that
scope in your own prompts: WhatsApp Business policy does not allow general-purpose assistants. The handoff
instruction is always appended to whichever prompt applies.

## Per-inbox LLM keys (`/connect`)

With `CONNECT_ADMIN_MPASS_IDS` set, an allowed admin can paste an LLM key for one inbox at
`<PUBLIC_BASE_URL>/connect`. The reply path picks the LLM per conversation: the inbox's pasted key first, then
the `LLM_API_KEY` fallback, otherwise a handoff with a private note pointing at the page.

- **Identity.** The page reads `X-Auth-Request-Email`, which mpass-auth ForwardAuth sets behind Traefik. Traefik
  must route `/connect` through mpass-auth and strip client-supplied `X-Auth-Request-*` headers first; the bot
  trusts the header and reads it nowhere else. No header gives 401, an identity not on the list gives 403. The
  value is normalised like Chatwoot's mPass patch (trimmed, lowercased, email shape checked without a regex), and
  `1234`, `1234@<DEFAULT_EMAIL_DOMAIN>` match each other on either side. With email capture on, the email header
  carries the user's real address and the mPass id arrives in `X-Auth-Request-Preferred-Username` (same edge,
  same strip). With `CONNECT_TRUST_PREFERRED_USERNAME=true`, set only where mpass-auth-proxy stamps that claim
  (email capture on), an all-ASCII-digit value there is matched as a bare mPass id too, and only beside a real
  email header. Off (the default), or beside a bare or synthetic email header, it is ignored: on capture-off hosts
  it is the raw Cognito attribute, which a user may be able to edit. The email header is always required.
- **Listing.** Inboxes are learned from webhook payloads (`inbox.id`, `inbox.name`); any other inbox id can be
  typed in. Each connected inbox shows the provider host, model, the key's last four characters (only for keys of
  12 characters or more), who set it and when. The key itself is never shown or logged.
- **Saving.** The form takes an inbox id, an https base URL on `LLM_BASE_URL_ALLOWLIST`, a model and a key. The
  key is checked with one small completion (no retries, at most 15 s) before it is stored; an auth or model error
  is shown without the provider's own text. The key is encrypted with `KEY_ENCRYPTION_KEY` together with its
  inbox id, base URL and model, so a stored record cannot be moved to another inbox or pointed at another host.
  The host is checked against `LLM_BASE_URL_ALLOWLIST` again on every reply: a key whose host has since left the
  list is not used (the conversation hands off with a reconnect note) and the page marks it "host no longer
  allowed, reconnect". The env `LLM_BASE_URL` is operator-set and not checked. LLM calls never follow redirects.
- **Edge secret.** Every `/connect` request must carry `X-Mpass-Edge-Secret` equal to `CONNECT_EDGE_SECRET`
  (constant-time compare); without it the identity header is ignored and the request gets a 401.
- **CSRF.** Every POST needs the `__Host-cwbot_csrf` cookie token in the form and an `Origin` (or `Referer`)
  equal to `PUBLIC_BASE_URL`. Pages are sent `no-store` with `frame-ancestors 'none'`.
- **Audit.** Saves and deletes log the mPass id, inbox id, provider host and model. An admin known only by a
  real email (no mPass id in either header) is logged and recorded as `email-identity`.
- **Clients.** One client per inbox and key; a replaced or deleted key's client closes after any reply still
  using it.

Generate an encryption key with
`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Rotating it makes
stored keys unreadable: conversations in those inboxes hand off with a note until the key is pasted again.

## Running

```bash
docker build -t chatwoot-autoreply-bot .
docker run --env-file bot.env -p 8000:8000 chatwoot-autoreply-bot
```

- Expose only `POST /webhook` publicly, plus `/connect` behind mpass-auth when the Connect page is on. `GET
  /healthz` is for the container probe; it answers 503 while Valkey is unreachable.
- One replica is enough. Generations, locks and the answered marker live in Valkey, so more replicas do not
  answer twice; each process runs one worker.
- Conversations with a reply still to come are listed in Valkey; on startup the bot reschedules them, so turns
  waiting or running when it stopped are picked up again without answering twice. A turn killed between marking
  its messages answered and posting leaves them unanswered until the customer writes again; a turn killed
  mid-handoff can leave the note without the open.
- Valkey evicting keys (an `allkeys-lru` policy) can drop a transcript or those markers: that shortens the
  bot's context, or skips a resume. Pasted keys never live there: `KEYSTORE_VALKEY_URL` must not evict.

## Development

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run ruff format .
uv run ruff check .
uv run pytest --cov=chatwoot_autoreply_bot --cov-fail-under=80
```

## Licence

MIT, see [LICENSE](LICENSE). The bot contains no Chatwoot code: it is an independent service that uses Chatwoot's
public webhook and REST API, and does not use Chatwoot's Enterprise or Captain features.
