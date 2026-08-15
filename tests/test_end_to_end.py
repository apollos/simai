"""Pytest-discoverable wrappers around the executable end-to-end suites."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

from simai.config import load_config


def _load(name: str):
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_core_end_to_end(tmp_path):
    suite = _load("smoke_test")
    suite.checks.clear()
    work = tmp_path / "core"
    work.mkdir()
    suite.run(work)


def test_api_end_to_end(tmp_path):
    suite = _load("api_test")
    suite.checks.clear()
    work = tmp_path / "api"
    work.mkdir()
    suite.run(work)


def test_source_binding_security_booleans_are_not_coerced(tmp_path):
    config_path = tmp_path / "simai.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "runtime": {"profile": "local_wsl", "data_dir": str(tmp_path / "data")},
                "profiles": {"local_wsl": {"openclaw_gateway": "http://127.0.0.1:1"}},
                "source_bindings": [
                    {
                        "id": "unsafe",
                        "channel": "openclaw-weixin",
                        "account_id": "bot",
                        "sender_key": "owner",
                        # YAML strings must not become truthy security flags.
                        "enabled": "false",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="enabled must be boolean"):
        load_config(config_path)
