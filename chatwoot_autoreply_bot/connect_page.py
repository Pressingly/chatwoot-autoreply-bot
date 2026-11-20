"""Server-rendered HTML for the Connect page. No scripts, no inline styles; every value is escaped."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import escape

from chatwoot_autoreply_bot.events import InboxRef
from chatwoot_autoreply_bot.keystore import Connection

TITLE = "Auto-reply LLM keys"
HOST_NOT_ALLOWED = "host no longer allowed, reconnect"
REFILLED_FIELDS = ("inbox_id", "base_url", "model")


@dataclass(frozen=True)
class PageView:
    csrf_token: str
    allowed_hosts: Collection[str]
    connections: Sequence[Connection] = ()
    inboxes: Sequence[InboxRef] = ()
    error: str | None = None
    values: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class InboxRow:
    inbox_id: int
    inbox: InboxRef | None
    connection: Connection | None


def inbox_rows(inboxes: Sequence[InboxRef], connections: Sequence[Connection]) -> list[InboxRow]:
    known = {inbox.inbox_id: inbox for inbox in inboxes}
    connected = {connection.inbox_id: connection for connection in connections}
    return [InboxRow(inbox_id, known.get(inbox_id), connected.get(inbox_id)) for inbox_id in sorted(known | connected)]


def render_connect_page(view: PageView) -> str:
    body = "\n".join(
        [
            f"<h1>{TITLE}</h1>",
            _error(view.error),
            "<h2>Inboxes</h2>",
            _inbox_table(inbox_rows(view.inboxes, view.connections), view.csrf_token, view.allowed_hosts),
            "<h2>Connect a key</h2>",
            _connect_form(view),
        ]
    )
    return _document(body)


def render_notice(message: str) -> str:
    return _document(f"<h1>{TITLE}</h1>\n<p>{escape(message)}</p>")


def _document(body: str) -> str:
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{TITLE}</title>\n</head>\n<body>\n<main>\n{body}\n</main>\n</body>\n</html>\n"
    )


def _error(message: str | None) -> str:
    return f'<p role="alert"><strong>{escape(message)}</strong></p>' if message else ""


def _inbox_table(rows: Sequence[InboxRow], csrf_token: str, allowed_hosts: Collection[str]) -> str:
    if not rows:
        return "<p>No inboxes seen yet. Connect a key by inbox id below.</p>"
    header = "".join(
        f"<th>{name}</th>"
        for name in ("Inbox", "Name", "Account", "Provider host", "Model", "Key", "Set by", "Set at", "")
    )
    lines = "\n".join(_inbox_row(row, csrf_token, allowed_hosts) for row in rows)
    return f"<table>\n<thead><tr>{header}</tr></thead>\n<tbody>\n{lines}\n</tbody>\n</table>"


def _inbox_row(row: InboxRow, csrf_token: str, allowed_hosts: Collection[str]) -> str:
    inbox_cells = [
        str(row.inbox_id),
        row.inbox.name if row.inbox else "",
        str(row.inbox.account_id) if row.inbox and row.inbox.account_id else "",
    ]
    cells = "".join(
        f"<td>{escape(value)}</td>" for value in inbox_cells + _connection_cells(row.connection, allowed_hosts)
    )
    action = _delete_form(row.inbox_id, csrf_token) if row.connection else ""
    return f"<tr>{cells}<td>{action}</td></tr>"


def _connection_cells(connection: Connection | None, allowed_hosts: Collection[str]) -> list[str]:
    if connection is None:
        return ["", "", "not connected", "", ""]
    key = f"ending {connection.key_hint}" if connection.key_hint else "set"
    return [
        _host_cell(connection.host, allowed_hosts),
        connection.model,
        key,
        connection.set_by,
        _timestamp(connection.set_at),
    ]


def _host_cell(host: str, allowed_hosts: Collection[str]) -> str:
    return host if host in allowed_hosts else f"{host} ({HOST_NOT_ALLOWED})"


def _timestamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%d %H:%M UTC") if epoch else ""


def _delete_form(inbox_id: int, csrf_token: str) -> str:
    return (
        f'<form method="post" action="/connect/{inbox_id}/delete">{_csrf_input(csrf_token)}'
        '<button type="submit">Remove key</button></form>'
    )


def _csrf_input(csrf_token: str) -> str:
    return f'<input type="hidden" name="csrf_token" value="{escape(csrf_token)}">'


def _connect_form(view: PageView) -> str:
    values = {name: escape(view.values.get(name, "")) for name in REFILLED_FIELDS}
    options = "".join(f'<option value="{inbox.inbox_id}">{escape(inbox.name)}</option>' for inbox in view.inboxes)
    hosts = escape(", ".join(sorted(view.allowed_hosts)))
    return "\n".join(
        [
            '<form method="post" action="/connect">',
            _csrf_input(view.csrf_token),
            f'<p><label>Inbox id <input name="inbox_id" inputmode="numeric" list="known-inboxes" required '
            f'value="{values["inbox_id"]}"></label></p>',
            f'<datalist id="known-inboxes">{options}</datalist>',
            f'<p><label>Base URL <input name="base_url" type="url" required maxlength="500" '
            f'placeholder="https://api.openai.com/v1" value="{values["base_url"]}"></label></p>',
            f"<p>Allowed hosts: {hosts}</p>",
            f'<p><label>Model <input name="model" required maxlength="200" value="{values["model"]}"></label></p>',
            '<p><label>API key <input name="api_key" type="password" autocomplete="off" required '
            'maxlength="500"></label></p>',
            "<p>The key is checked with one small request before it is saved, and is never shown again.</p>",
            '<p><button type="submit">Check and save</button></p>',
            "</form>",
        ]
    )
