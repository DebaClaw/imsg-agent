from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.config import load_config


def test_default_imsg_binary_uses_path_command(tmp_path: Path) -> None:
    config_path = tmp_path / "imsg.json"
    config_path.write_text(json.dumps({"data_dir": str(tmp_path / "data")}))

    config = load_config(config_path)

    assert str(config.imsg_binary) == "imsg"


def test_imsg_binary_can_be_overridden_by_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "imsg.json"
    config_path.write_text(json.dumps({"imsg_binary": "imsg"}))
    monkeypatch.setenv("IMSG_BINARY", "/opt/homebrew/bin/imsg")

    config = load_config(config_path)

    assert str(config.imsg_binary) == "/opt/homebrew/bin/imsg"
