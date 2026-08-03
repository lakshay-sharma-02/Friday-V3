"""Config loader tests — defaults, file merge, env-var overrides.

Covers the full config hierarchy: hardcoded defaults < config file <
``FRIDAY_V4_<SECTION>_<FIELD>`` environment variables. All tests are
hermetic (tmp file paths + monkeypatched env), never touching the real
``~/.friday/v4_config.json``.
"""

from __future__ import annotations

import json

import pytest

from friday_v4.config import (
    V4Config,
    load_config,
    write_default_config,
)

# ==========================================================================
# Defaults (no file, no env)
# ==========================================================================


class TestDefaults:
    def test_missing_file_returns_defaults(self, tmp_path):
        cfg = load_config(tmp_path / "nope.json")
        assert cfg.voice.enabled is True
        assert cfg.voice.tts_provider == "piper"
        assert cfg.voice.hotword == "hey friday"
        assert cfg.desktop.wm == "auto"
        assert cfg.security.enabled is True
        assert cfg.security.scan_interval_minutes == 60
        assert cfg.collab.enabled is False

    def test_empty_file_returns_defaults(self, tmp_path):
        path = tmp_path / "v4_config.json"
        path.write_text("{}")
        cfg = load_config(path)
        assert cfg.voice.tts_provider == "piper"
        assert cfg.security.vulnerability_severity_threshold == "medium"


# ==========================================================================
# File merge (partial sections)
# ==========================================================================


class TestFileMerge:
    def _write(self, tmp_path, raw):
        path = tmp_path / "v4_config.json"
        path.write_text(json.dumps(raw))
        return path

    def test_partial_voice_section(self, tmp_path):
        path = self._write(tmp_path, {"voice": {"tts_provider": "kokoro"}})
        cfg = load_config(path)
        assert cfg.voice.tts_provider == "kokoro"   # from file
        assert cfg.voice.hotword == "hey friday"    # default preserved

    def test_partial_desktop_section(self, tmp_path):
        path = self._write(tmp_path, {"desktop": {"wm": "hyprland"}})
        cfg = load_config(path)
        assert cfg.desktop.wm == "hyprland"
        assert cfg.desktop.system_tray is True

    def test_partial_security_section(self, tmp_path):
        path = self._write(tmp_path,
                           {"security": {"scan_interval_minutes": 15}})
        cfg = load_config(path)
        assert cfg.security.scan_interval_minutes == 15
        assert cfg.security.secret_detection is True

    def test_partial_collab_section(self, tmp_path):
        path = self._write(tmp_path, {"collab": {"enabled": True}})
        cfg = load_config(path)
        assert cfg.collab.enabled is True
        assert cfg.collab.sync_interval_seconds == 30

    def test_unknown_fields_ignored(self, tmp_path):
        path = self._write(tmp_path, {"voice": {"bogus_field": 1}})
        cfg = load_config(path)
        assert not hasattr(cfg.voice, "bogus_field")
        assert cfg.voice.tts_provider == "piper"

    def test_invalid_json_returns_defaults(self, tmp_path):
        path = tmp_path / "v4_config.json"
        path.write_text("{not json!!!")
        cfg = load_config(path)
        assert cfg.voice.tts_provider == "piper"
        assert cfg.security.enabled is True


# ==========================================================================
# Environment overrides (highest priority)
# ==========================================================================


class TestEnvOverrides:
    def test_str_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FRIDAY_V4_VOICE_TTS_PROVIDER", "edge-tts")
        cfg = load_config(tmp_path / "nope.json")
        assert cfg.voice.tts_provider == "edge-tts"

    def test_bool_override_false(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FRIDAY_V4_SECURITY_ENABLED", "0")
        cfg = load_config(tmp_path / "nope.json")
        assert cfg.security.enabled is False

    def test_bool_override_true_words(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FRIDAY_V4_COLLAB_ENABLED", "yes")
        cfg = load_config(tmp_path / "nope.json")
        assert cfg.collab.enabled is True

    def test_int_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FRIDAY_V4_SECURITY_SCAN_INTERVAL_MINUTES", "30")
        cfg = load_config(tmp_path / "nope.json")
        assert cfg.security.scan_interval_minutes == 30

    def test_float_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FRIDAY_V4_VOICE_HOTWORD_SENSITIVITY", "0.9")
        cfg = load_config(tmp_path / "nope.json")
        assert cfg.voice.hotword_sensitivity == pytest.approx(0.9)

    def test_env_beats_file(self, tmp_path, monkeypatch):
        path = tmp_path / "v4_config.json"
        path.write_text(json.dumps({"voice": {"tts_provider": "piper"}}))
        monkeypatch.setenv("FRIDAY_V4_VOICE_TTS_PROVIDER", "kokoro")
        cfg = load_config(path)
        assert cfg.voice.tts_provider == "kokoro"

    def test_invalid_int_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FRIDAY_V4_SECURITY_SCAN_INTERVAL_MINUTES", "abc")
        cfg = load_config(tmp_path / "nope.json")
        assert cfg.security.scan_interval_minutes == 60  # default kept


# ==========================================================================
# write_default_config
# ==========================================================================


class TestWriteDefaultConfig:
    def test_creates_file(self, tmp_path):
        path = tmp_path / "v4_config.json"
        written = write_default_config(path)
        assert written == path
        assert path.exists()
        raw = json.loads(path.read_text())
        assert raw["voice"]["tts_provider"] == "piper"
        assert raw["security"]["scan_interval_minutes"] == 60

    def test_existing_file_not_overwritten(self, tmp_path):
        path = tmp_path / "v4_config.json"
        path.write_text(json.dumps({"voice": {"tts_provider": "kokoro"}}))
        write_default_config(path)
        raw = json.loads(path.read_text())
        assert raw["voice"]["tts_provider"] == "kokoro"  # untouched


# ==========================================================================
# mobile_push section (Wave 15 — operator-configurable push hook)
# ==========================================================================


class TestMobilePushConfig:
    def test_defaults(self, tmp_path):
        cfg = load_config(tmp_path / "nope.json")
        assert cfg.mobile_push.enabled is True
        assert cfg.mobile_push.interval == 60.0
        assert cfg.mobile_push.priority == 0
        assert cfg.mobile_push.hook is None
        assert cfg.mobile_push.file_path is None

    def test_file_merge(self, tmp_path):
        path = tmp_path / "v4_config.json"
        path.write_text(json.dumps({"mobile_push": {
            "hook": "curl -s -X POST -d @- https://ntfy.sh/friday",
            "priority": 1,
        }}))
        cfg = load_config(path)
        assert cfg.mobile_push.hook == "curl -s -X POST -d @- https://ntfy.sh/friday"
        assert cfg.mobile_push.priority == 1
        assert cfg.mobile_push.enabled is True      # default preserved
        assert cfg.mobile_push.file_path is None

    def test_file_path_merge(self, tmp_path):
        path = tmp_path / "v4_config.json"
        path.write_text(json.dumps({"mobile_push": {
            "file_path": "/tmp/friday-outbox.jsonl",
            "enabled": False,
        }}))
        cfg = load_config(path)
        assert cfg.mobile_push.file_path == "/tmp/friday-outbox.jsonl"
        assert cfg.mobile_push.enabled is False

    def test_env_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FRIDAY_V4_MOBILE_PUSH_HOOK", "cat >> ~/push.log")
        monkeypatch.setenv("FRIDAY_V4_MOBILE_PUSH_INTERVAL", "30")
        cfg = load_config(tmp_path / "nope.json")
        assert cfg.mobile_push.hook == "cat >> ~/push.log"
        assert cfg.mobile_push.interval == 30

    def test_env_beats_file(self, tmp_path, monkeypatch):
        path = tmp_path / "v4_config.json"
        path.write_text(json.dumps({"mobile_push": {"hook": "from-file"}}))
        monkeypatch.setenv("FRIDAY_V4_MOBILE_PUSH_HOOK", "from-env")
        cfg = load_config(path)
        assert cfg.mobile_push.hook == "from-env"

    def test_default_config_includes_section(self, tmp_path):
        path = tmp_path / "v4_config.json"
        write_default_config(path)
        raw = json.loads(path.read_text())
        assert raw["mobile_push"]["enabled"] is True
        assert raw["mobile_push"]["hook"] is None


class TestConfigType:
    def test_load_returns_v4config(self, tmp_path):
        assert isinstance(load_config(tmp_path / "nope.json"), V4Config)
