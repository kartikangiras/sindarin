"""Shared bits for the connectors: HubSpot (crm.py), Dropbox Sign (esign.py), Google Drive and
Slides (gdrive.py). Each is a thin client the executors call after a gate has decided; none of
them decides anything, and a connector failure is reported in the room, never a reason to undo
what the books recorded."""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from typing import Any

import httpx


class ConnectorError(Exception):
    """A connector failure. The text is shown in a room; it must hold no secret."""


def call(
    http: httpx.Client,
    method: str,
    url: str,
    *,
    who: str,
    json: Any = None,
    data: Mapping[str, Any] | None = None,
    files: Any = None,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    content: bytes | None = None,
) -> dict[str, Any]:
    """One request; a JSON body back; a readable refusal when the service says no."""
    try:
        r = http.request(
            method,
            url,
            json=json,
            data=data,
            files=files,
            params=params,
            headers=headers,
            content=content,
        )
    except httpx.HTTPError as exc:
        raise ConnectorError(f"{who} unreachable: {exc.__class__.__name__}") from None
    body: Any = {}
    with contextlib.suppress(ValueError):
        body = r.json()
    if r.status_code >= 400:
        detail = ""
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                detail = str(err.get("message") or err.get("error_msg") or "")
            else:
                detail = str(body.get("message") or body.get("error_msg") or err or "")
        raise ConnectorError(f"{who}: HTTP {r.status_code}{' — ' + detail[:140] if detail else ''}")
    return body if isinstance(body, dict) else {"result": body}


def slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")[:60]
