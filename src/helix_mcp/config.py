"""Explicit local configuration; no tokens in command-line arguments."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


class ConfigError(ValueError):
    pass


def read_token_file(variable: str) -> str:
    location = os.environ.get(variable, "")
    if not location:
        raise ConfigError(f"{variable} must name a secret file.")
    try:
        with Path(location).open(encoding="utf-8") as stream:
            token = stream.read(1024).strip()
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"Cannot read {variable}.") from exc
    if not 32 <= len(token) <= 512 or any(not 33 <= ord(c) <= 126 for c in token):
        raise ConfigError(f"{variable} must contain a 32-512 character ASCII token without whitespace.")
    return token


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    mcp_token: str = field(repr=False)
    worker_token: str = field(repr=False)
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1")
    allowed_origins: tuple[str, ...] = ()
    worker_id: str = "helix-floor-windows"
    target_setlist: int | None = None
    target_slot: str | None = None
    target_setlist_name: str = ""
    lease_seconds: int = 180
    queue_seconds: int = 600

    def __post_init__(self):
        for token in (self.mcp_token, self.worker_token):
            if not 32 <= len(token) <= 512 or any(not 33 <= ord(c) <= 126 for c in token):
                raise ConfigError("Role tokens must be random ASCII strings of 32-512 characters.")
        if self.mcp_token == self.worker_token:
            raise ConfigError("MCP and worker tokens must be different.")
        if not self.allowed_hosts or any(not h or '*' in h or '/' in h or '@' in h for h in self.allowed_hosts):
            raise ConfigError("HELIX_ALLOWED_HOSTS requires explicit hostnames; wildcards are not supported.")
        for origin in self.allowed_origins:
            parsed = urlsplit(origin)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
                raise ConfigError("HELIX_ALLOWED_ORIGINS must contain exact origins without paths.")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", self.worker_id):
            raise ConfigError("Invalid HELIX_WORKER_ID.")
        if self.target_setlist is not None and not 1 <= self.target_setlist <= 8:
            raise ConfigError("HELIX_TARGET_SETLIST must be 1-8.")
        if self.target_slot is not None and not re.fullmatch(r"(?:0[1-9]|[12][0-9]|3[0-2])[A-D]", self.target_slot):
            raise ConfigError("HELIX_TARGET_SLOT must be 01A-32D.")
        if len(self.target_setlist_name) > 64:
            raise ConfigError("Target setlist name is too long.")
        if not 30 <= self.lease_seconds <= 600 or not 30 <= self.queue_seconds <= 86400:
            raise ConfigError("Invalid job expiry settings.")

    @classmethod
    def from_env(cls) -> "Settings":
        setlist = os.environ.get("HELIX_TARGET_SETLIST", "").strip()
        try:
            target_setlist = int(setlist) if setlist else None
        except ValueError as exc:
            raise ConfigError("HELIX_TARGET_SETLIST must be an integer.") from exc
        return cls(
            data_dir=Path(os.environ.get("HELIX_DATA_DIR", "./data")).resolve(),
            mcp_token=read_token_file("HELIX_MCP_TOKEN_FILE"),
            worker_token=read_token_file("HELIX_WORKER_TOKEN_FILE"),
            allowed_hosts=tuple(h.strip().lower() for h in os.environ.get(
                "HELIX_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()),
            allowed_origins=tuple(o.strip() for o in os.environ.get("HELIX_ALLOWED_ORIGINS", "").split(",") if o.strip()),
            worker_id=os.environ.get("HELIX_WORKER_ID", "helix-floor-windows"),
            target_setlist=target_setlist,
            target_slot=os.environ.get("HELIX_TARGET_SLOT", "").strip() or None,
            target_setlist_name=os.environ.get("HELIX_TARGET_SETLIST_NAME", "").strip(),
        )

    def public_summary(self) -> dict:
        return {"worker_id": self.worker_id, "allowed_hosts": list(self.allowed_hosts),
                "allowed_origins": list(self.allowed_origins),
                "target": {"setlist": self.target_setlist, "slot": self.target_slot,
                           "setlist_name": self.target_setlist_name},
                "automatic_hardware_import": False}
