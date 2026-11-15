"""Gateway configuration: the site's chat section, plus tokens from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sre_gateway import harness

# Environment variables the gateway reads for itself. Tokens are named by the site.
MCP_CONFIG_ENV = "SRE_GATEWAY_MCP_CONFIG"
MODEL_ENV = "SRE_GATEWAY_MODEL"
STATE_ENV = "SRE_GATEWAY_STATE"


class ConfigError(Exception):
    """The gateway cannot start with this configuration."""


@dataclass
class Config:
    site_root: Path
    doc: dict[str, Any]
    chat: dict[str, Any]
    tokens: dict[str, str] = field(repr=False)
    mcp_config: Path | None = None
    model: str | None = None
    state_dir: Path = field(default_factory=lambda: harness.sre_common.state_dir() / "gateway")

    @property
    def responders(self) -> frozenset[str]:
        return frozenset(self.chat["responders"])

    @property
    def approvers(self) -> frozenset[str]:
        return frozenset(self.chat["approvers"])

    @property
    def hub(self) -> str:
        return self.chat["hub_channel"]

    @property
    def approval_ttl(self) -> int:
        return int(self.chat.get("approval_ttl_seconds", 300))

    @property
    def idle_stop_seconds(self) -> int:
        return int(self.chat.get("idle_stop_minutes", 30)) * 60

    @property
    def max_sessions(self) -> int:
        return int(self.chat.get("max_sessions", 2))

    def credential_names(self) -> list[str]:
        """Environment variables the site says hold credentials."""
        names = {name for source in self.doc.get("monitoring", []) for name in source.get("credentials_env", [])}
        assist = self.doc.get("incident", {}).get("assist") or {}
        if assist.get("api_key_env"):
            names.add(assist["api_key_env"])
        return sorted(names)

    def readable_dirs(self) -> list[Path]:
        """Directories the agent may read besides the site: the plugin and the site's local repository checkouts."""
        dirs = [harness.PLUGIN_ROOT]
        for alias, repo in self.doc.get("repos", {}).items():
            local = repo.get("path")
            if alias != "catalog" and local and Path(local).expanduser().is_dir():
                dirs.append(Path(local).expanduser().resolve())
        evidence = self.doc.get("incident", {}).get("evidence_dir")
        if evidence:
            dirs.append(Path(evidence).expanduser())
        return dirs


def load(env: dict[str, str] | None = None, take_tokens: bool = True) -> Config:
    """Resolve and validate the site, read the chat section and the tokens.

    With take_tokens, the tokens are removed from this process's environment
    once read, so the agent the gateway starts never inherits them.
    """
    env = dict(os.environ) if env is None else env
    try:
        site_root = harness.sre_common.resolve_site(env=env)
        report = harness.validate_site.validate(site_root)
    except harness.sre_common.SiteError as exc:
        raise ConfigError(str(exc)) from exc
    if not report.ok:
        raise ConfigError(f"site is not valid ({len(report.errors)} error(s)); run validate_site.py")
    doc = harness.sre_common.load_yaml(site_root / harness.sre_common.SITE_FILE)
    chat = doc.get("chat") or {}
    if not chat:
        raise ConfigError("site.yaml has no chat section; the gateway has nothing to connect to")

    tokens = {}
    for role, name in chat["secrets_env"].items():
        value = env.get(name, "")
        if not value:
            raise ConfigError(f"environment variable {name} ({role}) is not set")
        tokens[role] = value
        if take_tokens:
            os.environ.pop(name, None)

    mcp = env.get(MCP_CONFIG_ENV)
    if mcp and not Path(mcp).expanduser().is_file():
        raise ConfigError(f"{MCP_CONFIG_ENV} names a file that does not exist")
    config = Config(
        site_root=site_root,
        doc=doc,
        chat=chat,
        tokens=tokens,
        mcp_config=Path(mcp).expanduser() if mcp else None,
        model=env.get(MODEL_ENV) or None,
    )
    if env.get(STATE_ENV):
        config.state_dir = Path(env[STATE_ENV]).expanduser()
    return config
