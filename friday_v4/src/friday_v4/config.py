"""Configuration loader for Friday V4.

Reads configuration from ~/.friday/v4_config.json with sensible defaults.
Follows V3 convention of ~/.friday/ for all data.

Config hierarchy (lowest to highest priority):
  1. Hardcoded defaults (in code)
  2. ~/.friday/v4_config.json
  3. Environment variables (FRIDAY_V4_*)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("friday_v4.config")

CONFIG_PATH = Path.home() / ".friday" / "v4_config.json"


@dataclass
class VoiceConfig:
    enabled: bool = True
    stt_model: str = "base.en"
    tts_provider: str = "kokoro"
    tts_voice: str = ""
    hotword: str = "hey friday"
    hotword_sensitivity: float = 0.7
    vad_mode: int = 1
    silence_timeout_seconds: float = 2.0
    max_utterance_seconds: float = 30.0
    enable_chimes: bool = True
    push_to_talk_key: str = "ctrl+shift+space"


@dataclass
class DesktopConfig:
    enabled: bool = True
    wm: str = "auto"
    system_tray: bool = True
    global_hotkeys: bool = True


@dataclass
class CollabConfig:
    enabled: bool = False
    discovery: str = "mdns"
    workspace_name: Optional[str] = None
    sync_interval_seconds: int = 30


@dataclass
class SecurityConfig:
    enabled: bool = True
    scan_on_change: bool = True
    scan_interval_minutes: int = 60
    vulnerability_severity_threshold: str = "medium"
    secret_detection: bool = True


@dataclass
class V4Config:
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    desktop: DesktopConfig = field(default_factory=DesktopConfig)
    collab: CollabConfig = field(default_factory=CollabConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)


def load_config(path: Optional[Path] = None) -> V4Config:
    """Load V4 config from file, merging with defaults.
    
    Missing fields use defaults. Invalid JSON returns defaults with a warning.
    """
    config = V4Config()
    config_path = path or CONFIG_PATH

    if not config_path.exists():
        logger.debug(f"No config at {config_path}, using defaults")
        return config

    try:
        raw = json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(f"Failed to parse {config_path}: {exc}")
        return config

    # Merge voice section
    voice_raw = raw.get("voice", {})
    for field_name in ("enabled", "stt_model", "tts_provider", "tts_voice",
                        "hotword", "hotword_sensitivity", "vad_mode",
                        "silence_timeout_seconds", "max_utterance_seconds",
                        "enable_chimes", "push_to_talk_key"):
        if field_name in voice_raw:
            setattr(config.voice, field_name, voice_raw[field_name])

    # Merge desktop section
    desk_raw = raw.get("desktop", {})
    for field_name in ("enabled", "wm", "system_tray", "global_hotkeys"):
        if field_name in desk_raw:
            setattr(config.desktop, field_name, desk_raw[field_name])

    # Merge collab section
    collab_raw = raw.get("collab", {})
    for field_name in ("enabled", "discovery", "workspace_name", "sync_interval_seconds"):
        if field_name in collab_raw:
            setattr(config.collab, field_name, collab_raw[field_name])

    # Merge security section
    sec_raw =raw.get("security", {})
    for field_name in ("enabled", "scan_on_change", "scan_interval_minutes",
                        "vulnerability_severity_threshold", "secret_detection"):
        if field_name in sec_raw:
            setattr(config.security, field_name, sec_raw[field_name])

    logger.debug(f"Loaded config from {config_path}")
    return config


def write_default_config(path: Optional[Path] = None) -> Path:
    """Write a default config file if one doesn't exist."""
    config_path = path or CONFIG_PATH
    if config_path.exists():
        return config_path

    config_path.parent.mkdir(parents=True, exist_ok=True)
    default = {
        "voice": {
            "enabled": True,
            "stt_model": "base.en",
            "tts_provider": "kokoro",
            "tts_voice": "",
            "hotword": "hey friday",
            "hotword_sensitivity": 0.7,
            "vad_mode": 1,
            "silence_timeout_seconds": 2.0,
            "max_utterance_seconds": 30.0,
            "enable_chimes": True,
            "push_to_talk_key": "ctrl+shift+space",
        },
        "desktop": {
            "enabled": True,
            "wm": "auto",
            "system_tray": False,
            "global_hotkeys": True,
        },
        "collab": {
            "enabled": False,
            "discovery": "mdns",
            "workspace_name": None,
            "sync_interval_seconds": 30,
        },
        "security": {
            "enabled": True,
            "scan_on_change": True,
            "scan_interval_minutes": 60,
            "vulnerability_severity_threshold": "medium",
            "secret_detection": True,
        },
    }
    config_path.write_text(json.dumps(default, indent=2))
    logger.info(f"Default config written to {config_path}")
    return config_path
