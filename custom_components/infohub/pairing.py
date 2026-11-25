"""Which displays may use InfoHub: pairing, approved in Home Assistant.

A display identifies itself in its hello by device_id and, once paired,
the token InfoHub gave it. Without a valid token it gets nothing (no
layout, no data) and its actions and buttons are ignored: it waits,
listed as pending, until someone approves it in the InfoHub panel. Then
InfoHub sends it a new random token, which it keeps and presents from
then on.

Only a hash of each token is stored. The tokens travel over the display
connection itself, which is plain ws:// - someone able to read the LAN
traffic could still copy one.

Displays known before pairing existed are approved once, without a
token yet (grandfather()): the next display to connect under such a
device_id gets a token, so the ones already in use keep working.

No Home Assistant imports: __init__.py hands in the stored data and a
function that saves it.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

OK = "ok"  # paired, the token matches
ISSUE = "issue"  # approved, no token yet: give it one
PENDING = "pending"  # unknown, or the token doesn't match: wait for approval

TOKEN_BYTES = 32


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DisplayAuth:
    """The approved displays (persisted) and the ones waiting (not)."""

    def __init__(self, data: dict[str, Any] | None, save: Callable[[dict[str, Any]], None]):
        data = data or {}
        # device_id -> {"token_hash": str | None, "approved_at": str}
        self._displays: dict[str, dict[str, Any]] = {
            k: dict(v) for k, v in (data.get("displays") or {}).items()
        }
        self._migrated: bool = bool(data.get("migrated"))
        self._save = save
        # device_id -> {"address", "since", "reason"}; cleared on restart
        self.pending: dict[str, dict[str, Any]] = {}

    def as_data(self) -> dict[str, Any]:
        return {"displays": self._displays, "migrated": self._migrated}

    def _changed(self) -> None:
        self._save(self.as_data())

    def check(self, device_id: str | None, token: str | None) -> str:
        """OK, ISSUE or PENDING for a display's hello."""
        entry = self._displays.get(device_id) if device_id else None
        if entry is None:
            return PENDING
        token_hash = entry.get("token_hash")
        if token_hash is None:
            return ISSUE
        if isinstance(token, str) and token and hmac.compare_digest(token_hash, _hash(token)):
            return OK
        return PENDING

    def issue(self, device_id: str) -> str:
        """A new token for an approved display; the old one stops working."""
        token = secrets.token_urlsafe(TOKEN_BYTES)
        entry = self._displays.setdefault(device_id, {"approved_at": _now()})
        entry["token_hash"] = _hash(token)
        self.pending.pop(device_id, None)
        self._changed()
        return token

    def approve(self, device_id: str) -> None:
        """Lets device_id pair: it gets a new token when it next connects
        (at once, if it is waiting). Approving a paired display again
        replaces its token, for a display that lost it."""
        self._displays[device_id] = {"token_hash": None, "approved_at": _now()}
        self._changed()

    def forget(self, device_id: str) -> None:
        """device_id has to be approved again."""
        self._displays.pop(device_id, None)
        self.pending.pop(device_id, None)
        self._changed()

    def note_pending(self, device_id: str, address: str | None) -> None:
        """A display that is waiting: unknown, or with a wrong token."""
        self.pending[device_id] = {
            "address": address,
            "since": _now(),
            "reason": "token" if device_id in self._displays else "new",
        }

    def is_approved(self, device_id: str) -> bool:
        return device_id in self._displays

    def grandfather(self, device_ids: Iterable[str]) -> None:
        """Once, at the first start with pairing: approves the displays
        known before (without tokens; see the module docstring)."""
        if self._migrated:
            return
        for device_id in device_ids:
            self._displays.setdefault(device_id, {"token_hash": None, "approved_at": _now()})
        self._migrated = True
        self._changed()

    def displays(self) -> list[dict[str, Any]]:
        """For the panel: every approved and waiting display."""
        out = [
            {
                "device_id": device_id,
                "approved": True,
                "paired": entry.get("token_hash") is not None,
                "approved_at": entry.get("approved_at"),
            }
            for device_id, entry in self._displays.items()
        ]
        out += [
            {"device_id": device_id, "approved": False, "paired": False, **info}
            for device_id, info in self.pending.items()
            if device_id not in self._displays or info.get("reason") == "token"
        ]
        return out
