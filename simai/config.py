"""Configuration loading and validation.

Security invariants (section 19 of the design doc) are enforced here:
they cannot be overridden by any runtime channel, only by editing the
config file on disk, and some of them are hard-pinned regardless.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

DEFAULT_CONFIG_LOCATIONS = (
    Path("~/.simai/simai.yaml"),
    Path("config/simai.yaml"),
)

# These settings are pinned: even if the YAML says otherwise, Simai
# refuses to weaken them (fail-safe, section 3.5 / 19).
HARD_INVARIANTS: dict[str, Any] = {
    ("voice", "retain_audio"): False,
    ("storage", "store_chat_transcript"): False,
    ("storage", "store_audio"): False,
    ("placement", "auto_commit"): False,
    ("placement", "allow_auto_merge"): False,
    ("placement", "allow_auto_move"): False,
    ("placement", "allow_auto_delete"): False,
    ("security", "allow_unlock_from_wechat"): False,
    ("security", "allow_password_in_cli_args"): False,
}


@dataclass
class SourceBinding:
    id: str
    channel: str
    account_id: str
    sender_key: str
    conversation_id: str | None = None
    enabled: bool = True
    passive_capture: bool = False
    allow_group: bool = False
    fail_closed_on_missing_identity: bool = True
    profiles: list[str] = field(default_factory=list)


@dataclass
class Config:
    raw: dict[str, Any]
    path: Path

    # -- convenience accessors -------------------------------------------
    @property
    def profile(self) -> str:
        return self.raw["runtime"].get("profile", "local_wsl")

    @property
    def timezone(self) -> str:
        return self.raw["runtime"].get("timezone", "Asia/Shanghai")

    @property
    def data_dir(self) -> Path:
        return Path(os.path.expanduser(self.raw["runtime"].get("data_dir", "~/.simai")))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "simai.db"

    @property
    def key_header_path(self) -> Path:
        return self.data_dir / "vault.header.json"

    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def export_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def openclaw_gateway(self) -> str:
        return self.raw["profiles"][self.profile]["openclaw_gateway"]

    @property
    def web_bind(self) -> str:
        return self.raw.get("web", {}).get("bind", "127.0.0.1")

    @property
    def web_port(self) -> int:
        return int(self.raw.get("web", {}).get("port", 18880))

    @property
    def inbox_socket_path(self) -> Path:
        raw = self.raw.get("sealed_inbox", {}).get("socket_path")
        if raw:
            return Path(os.path.expanduser(str(raw)))
        return self.data_dir / "inbox.sock"

    @property
    def plugin_token_path(self) -> Path:
        raw = self.raw.get("security", {}).get("plugin_token_path")
        if raw:
            return Path(os.path.expanduser(str(raw)))
        return self.data_dir / "plugin.token"

    def section(self, name: str) -> dict[str, Any]:
        return dict(self.raw.get(name, {}))

    def source_bindings(self) -> list[SourceBinding]:
        out: list[SourceBinding] = []
        for item in self.raw.get("source_bindings", []):
            profiles = item.get("profiles", [])
            if profiles and self.profile not in profiles:
                continue
            out.append(
                SourceBinding(
                    id=item["id"],
                    channel=item["channel"],
                    account_id=item["account_id"],
                    sender_key=item["sender_key"],
                    conversation_id=item.get("conversation_id"),
                    enabled=bool(item.get("enabled", True)),
                    passive_capture=bool(item.get("passive_capture", False)),
                    allow_group=bool(item.get("allow_group", False)),
                    fail_closed_on_missing_identity=bool(item.get("fail_closed_on_missing_identity", True)),
                    profiles=profiles,
                )
            )
        return out


def _apply_invariants(raw: dict[str, Any]) -> None:
    for (section, key), value in HARD_INVARIANTS.items():
        raw.setdefault(section, {})
        if raw[section].get(key) != value:
            raw[section][key] = value


def load_config(path: str | Path | None = None) -> Config:
    candidates = (
        [Path(os.path.expanduser(str(path)))]
        if path
        else [Path(os.path.expanduser(str(p))) for p in DEFAULT_CONFIG_LOCATIONS]
    )
    for candidate in candidates:
        if candidate.is_file():
            raw = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                raise TypeError("simai.yaml must contain a YAML mapping at the top level")
            _apply_invariants(raw)
            raw.setdefault("runtime", {})
            raw.setdefault("profiles", {"local_wsl": {"openclaw_gateway": "http://127.0.0.1:18791"}})
            config = Config(raw=raw, path=candidate)
            _validate_config(config)
            return config
    raise FileNotFoundError(
        "simai.yaml not found. Copy config/simai.yaml to ~/.simai/simai.yaml and adjust it."
    )


def _validate_config(config: Config) -> None:
    """Reject ambiguous capture identity before any message is accepted."""
    raw_bindings = config.raw.get("source_bindings", [])
    if not isinstance(raw_bindings, list):
        raise TypeError("source_bindings must be a list")
    bool_fields = (
        "enabled",
        "passive_capture",
        "allow_group",
        "fail_closed_on_missing_identity",
    )
    for index, item in enumerate(raw_bindings):
        if not isinstance(item, dict):
            raise TypeError(f"source_bindings[{index}] must be a mapping")
        for field_name in ("id", "channel", "account_id", "sender_key"):
            if not isinstance(item.get(field_name), str):
                raise TypeError(f"source_bindings[{index}].{field_name} must be a string")
        if item.get("conversation_id") is not None and not isinstance(item["conversation_id"], str):
            raise ValueError(f"source_bindings[{index}].conversation_id must be a string or null")
        for field_name in bool_fields:
            if field_name in item and not isinstance(item[field_name], bool):
                raise ValueError(f"source_bindings[{index}].{field_name} must be boolean")
        profiles = item.get("profiles", [])
        if not isinstance(profiles, list) or any(not isinstance(value, str) for value in profiles):
            raise ValueError(f"source_bindings[{index}].profiles must be a list of strings")

    if config.profile not in config.raw.get("profiles", {}):
        raise ValueError(f"Unknown runtime profile: {config.profile}")
    if not config.raw["profiles"][config.profile].get("openclaw_gateway"):
        raise ValueError(f"Profile {config.profile} has no openclaw_gateway")
    try:
        ZoneInfo(config.timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown runtime timezone: {config.timezone}") from exc
    daily_timezone = str(config.section("daily_capture").get("timezone", config.timezone))
    try:
        ZoneInfo(daily_timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown daily_capture timezone: {daily_timezone}") from exc

    models = config.section("models")
    if "inherit_main_default" in models and not isinstance(models["inherit_main_default"], bool):
        raise ValueError("models.inherit_main_default must be boolean")
    for field_name in ("task_agents", "task_models"):
        routes = models.get(field_name, {})
        if not isinstance(routes, dict) or any(
            not isinstance(task, str) or not task.strip() or not isinstance(route, str) or not route.strip()
            for task, route in routes.items()
        ):
            raise ValueError(f"models.{field_name} must map non-empty strings to non-empty strings")
    if models.get("inherit_main_default") is False:
        main_tasks = [name for name, agent in models.get("task_agents", {}).items() if agent == "main"]
        if main_tasks:
            raise ValueError(
                "Dedicated Simai tasks may not route to the main agent: " + ", ".join(main_tasks)
            )

    seen_ids: set[str] = set()
    seen_tuples: set[tuple[str, str, str, str | None]] = set()
    for binding in config.source_bindings():
        if binding.id in seen_ids:
            raise ValueError(f"Duplicate source binding id: {binding.id}")
        seen_ids.add(binding.id)
        identity = (
            binding.channel,
            binding.account_id,
            binding.sender_key,
            binding.conversation_id,
        )
        if identity in seen_tuples:
            raise ValueError(f"Duplicate source identity tuple: {binding.id}")
        seen_tuples.add(identity)
        if not binding.enabled:
            continue
        required = (binding.id, binding.channel, binding.account_id, binding.sender_key)
        if any(not value.strip() or "<" in value or ">" in value for value in required):
            raise ValueError(f"Enabled source binding has placeholder/empty identity: {binding.id}")
        if not binding.fail_closed_on_missing_identity:
            raise ValueError(f"Source binding must fail closed: {binding.id}")
