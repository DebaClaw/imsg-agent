"""
config.py — Load and validate agent configuration.

Config is read from config/imsg.json (or IMSG_AGENT_CONFIG env var).
Individual fields can be overridden via environment variables.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    imsg_binary: Path
    data_dir: Path
    rpc_timeout_seconds: int
    watch_debounce_ms: int
    history_limit: int
    chat_context_messages: int
    auto_approve: bool
    default_service: str
    max_inbox_age_hours: int


def load_config(path: Path | None = None) -> Config:
    """Load config from JSON file, with environment variable overrides."""
    if path is None:
        env_path = os.environ.get("IMSG_AGENT_CONFIG")
        if env_path:
            path = Path(env_path)
        else:
            path = Path(__file__).parent.parent / "config" / "imsg.json"

    with open(path) as f:
        data = json.load(f)

    data_dir_str = os.environ.get("IMSG_DATA_DIR") or data.get("data_dir", "~/imsg-data")
    binary_str = os.environ.get("IMSG_BINARY") or data.get("imsg_binary", "~/src/imsg/bin/imsg")

    return Config(
        imsg_binary=Path(binary_str).expanduser(),
        data_dir=Path(data_dir_str).expanduser(),
        rpc_timeout_seconds=int(data.get("rpc_timeout_seconds", 30)),
        watch_debounce_ms=int(data.get("watch_debounce_ms", 250)),
        history_limit=int(data.get("history_limit", 50)),
        chat_context_messages=int(data.get("chat_context_messages", 20)),
        auto_approve=bool(data.get("auto_approve", False)),
        default_service=str(data.get("default_service", "auto")),
        max_inbox_age_hours=int(data.get("max_inbox_age_hours", 48)),
    )
