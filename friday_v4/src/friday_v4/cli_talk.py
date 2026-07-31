"""CLI commands for `friday4 talk` — interactive voice session with Friday.

Usage:
    friday4 talk                              # Hotword mode ("Hey Friday")
    friday4 talk --push-to-talk               # Hold Ctrl+Space to talk
    friday4 talk --push-to-talk --push-to-talk-key ctrl+shift+m
                                             # Custom push-to-talk hotkey
    friday4 talk --tts-provider kokoro        # Use specific TTS engine
    friday4 talk --no-chimes                  # Disable audio cues
    friday4 voice setup / status / test       # Voice management
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - annotation only
    from .voice.tts import TextToSpeech

logger = logging.getLogger("friday_v4.cli_talk")


# ---------------------------------------------------------------------------
# Terminal UI helpers
# ---------------------------------------------------------------------------

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_CYAN = "\033[96m"
_RED = "\033[91m"
_CLR_LINE = "\033[2K\r"


def _print_logo():
    print()
    print(f"  {_BOLD}{_CYAN}◆ FRIDAY{_RESET} {_DIM}V4 — Voice Interface{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}")
    print()


def _print_state(state: str, detail: str = ""):
    icons = {"idle": "●", "hotword": "◉", "listening": "🎤",
             "processing": "⚙", "speaking": "🎧"}
    colors = {"idle": _DIM, "listening": _GREEN,
              "processing": _YELLOW, "speaking": _CYAN}
    icon = icons.get(state, "●")
    color = colors.get(state, _DIM)
    line = f"  {color}{icon} {state.upper()}{_RESET}"
    if detail:
        line += f" {_DIM}{detail}{_RESET}"
    print(f"{_CLR_LINE}{line}")


def _print_you(text: str):
    print(f"\n{_GREEN}  You:{_RESET} {text}")


def _print_friday(text: str):
    print(f"{_CYAN}  Friday:{_RESET} {text}")
    print()


def _print_error(text: str):
    print(f"\n{_RED}  ⚠ {text}{_RESET}\n")


def _print_help():
    print(f"\n{_DIM}  Commands:{_RESET}")
    print(f"  {_DIM}  Say \"Hey Friday\" to talk (or hold the push-to-talk key){_RESET}")
    print(f"  {_DIM}  Type 'exit' to quit{_RESET}")
    print(f"  {_DIM}  Type 'help' for this menu{_RESET}")
    print()


# ---------------------------------------------------------------------------
# CLI Commands
# ---------------------------------------------------------------------------


def cmd_talk(args: argparse.Namespace):
    """Start interactive voice session with Friday."""
    from .voice.pipeline import VoicePipeline
    from .voice.router import VoiceRouter
    from .voice.core import config_from_file

    _print_logo()

    # Base = ~/.friday/v4_config.json (falls back to defaults), then
    # command-line flags override specific fields.
    config = config_from_file()
    if args.push_to_talk:
        config.hotword = ""
    if args.silero_vad:
        config.vad_mode = 3
    if args.no_chimes:
        config.enable_chimes = False
    if args.tts_provider:
        config.tts_provider = args.tts_provider
    if args.tts_voice:
        config.tts_voice = args.tts_voice
    if args.silence_timeout is not None:
        config.silence_timeout_seconds = args.silence_timeout
    if args.max_utterance is not None:
        config.max_utterance_seconds = args.max_utterance
    if args.hotword_sensitivity is not None:
        config.hotword_sensitivity = args.hotword_sensitivity
    if args.tts_speed is not None:
        config.tts_speed = args.tts_speed

    pipeline = VoicePipeline(config)
    router = VoiceRouter(pipeline, enable_proactive=True)

    pipeline.on_transcription = lambda text: _print_you(text)
    pipeline.on_state_change = lambda s: _print_state(s.value)
    pipeline.on_error = lambda err: _print_error(err)
    pipeline.route_function = router.route

    if not pipeline.start():
        _print_error("Failed to start voice pipeline. Run 'friday4 voice setup' to diagnose.")
        return 1

    def _handle_text(text: str) -> None:
        """Print, route, and speak a transcribed/typed user input."""
        text = text.strip()
        if not text:
            return
        _print_you(text)
        try:
            response = router.route(text)
        except Exception as exc:
            response = f"Sorry, I ran into an error: {exc}"
        if response:
            _print_friday(response)
            pipeline.speak(response)

    # Push-to-talk: bind a real hold-to-talk hotkey so the CLI actually
    # listens while the key is held (previously --push-to-talk only
    # disabled the hotword but never bound a key). Degrades gracefully
    # to typed input when the optional `keyboard` lib is unavailable.
    ptt_bound = False
    if args.push_to_talk:
        ptt_bound = _bind_push_to_talk(pipeline, _handle_text,
                                       key=args.push_to_talk_key)
        if ptt_bound:
            print(f"  {_GREEN}  🎙 Push-to-talk bound: hold"
                  f" {args.push_to_talk_key} to talk{_RESET}")
        else:
            print(f"  {_YELLOW}  ⚠ Push-to-talk hotkey unavailable"
                  f" (install 'keyboard') — type instead{_RESET}")

    print(f"  {_DIM}Hotword:{_RESET} '{config.hotword or 'disabled'}'"
          f"  {_DIM}TTS:{_RESET} {pipeline.active_provider}"
          f"  {_DIM}VAD:{_RESET} mode {config.vad_mode}")
    _print_help()

    _print_state("idle", "Watching for context...")
    try:
        proactive_text = router.proactive_notify(force=False)
        if proactive_text:
            _print_friday(proactive_text)
    except Exception:
        pass

    try:
        while True:
            try:
                user_input = input(f"{_DIM}  > {_RESET}").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if user_input.lower() in ("exit", "quit", "stop"):
                break
            elif user_input.lower() in ("help", "?"):
                _print_help()
                continue
            elif user_input == "setup":
                return cmd_voice_setup(args)
            elif user_input:
                _handle_text(user_input)
    except KeyboardInterrupt:
        print()
    finally:
        if ptt_bound:
            _unbind_push_to_talk()
        pipeline.stop()
        router.cleanup()
        print(f"\n{_DIM}  Voice session ended.{_RESET}\n")

    return 0


def _bind_push_to_talk(pipeline, on_text, key: str = "ctrl+space") -> bool:
    """Bind a hold-to-talk global hotkey via the optional `keyboard` lib.

    Press-and-hold starts recording; release transcribes and hands the text
    to ``on_text``. Returns True if the hook was registered, False if the
    library is unavailable or registration failed (caller degrades to
    typed input).
    """
    try:
        import keyboard
    except ImportError:
        return False

    try:
        # keyboard lib supports per-key press/release handlers
        parts = [p.strip() for p in key.lower().split("+")]
        base = parts[-1]
        modifiers = parts[:-1]
        recording = {"active": False}

        def _modifiers_active() -> bool:
            try:
                return all(keyboard.is_pressed(m) for m in modifiers)
            except Exception:
                return False

        def _on_press(event):
            # Guard against auto-repeat / duplicate presses: once recording
            # is active, further presses must not reset the audio buffer.
            if event.name != base or not _modifiers_active() or recording["active"]:
                return
            recording["active"] = True
            pipeline.push_to_talk()

        def _on_release(event):
            # Process on base-key release regardless of modifier state —
            # the user may release Ctrl before Space.
            if event.name != base or not recording["active"]:
                return
            recording["active"] = False
            text = pipeline.stop_recording_and_process()
            if text:
                on_text(text)

        keyboard.on_press_key(base, _on_press, suppress=False)
        keyboard.on_release_key(base, _on_release, suppress=False)
        return True
    except Exception:
        return False


def _unbind_push_to_talk() -> None:
    """Remove the push-to-talk hotkey hooks (best-effort).

    Uses unhook_all() because this CLI owns the session process — the only
    hooks registered are the push-to-talk handlers we installed.
    """
    try:
        import keyboard
        keyboard.unhook_all()
    except Exception:
        pass


def _await_tts_provider() -> "TextToSpeech | None":
    """Wait (bounded) for the primary TTS provider's async model load.

    `voice status` / `voice setup` construct a TextToSpeech whose kokoro
    model download runs on a daemon thread — if this process exits first
    the download dies mid-write, leaving a corrupt partial model that
    would fail to load forever. Diagnostic CLIs must let the load finish.

    Always blocks on the *primary* provider (kokoro), even when a fallback
    like edge-tts is already available — otherwise `voice status` reports
    kokoro as ✗ because the fallback wins the init race.    Returns the prepared TextToSpeech instance, or None when TTS is
    unavailable.
    """
    try:
        from .voice.tts import TextToSpeech
        tts = TextToSpeech()
        for p in tts.list_providers():
            wait = getattr(p, "_wait_loaded", None)
            if callable(wait):
                try:
                    wait(timeout=600)
                except Exception:
                    pass
                break
        # Promote the primary (kokoro) over whatever fallback won the
        # init race — otherwise `voice status` reports the wrong engine.
        ensure = getattr(tts, "_ensure_primary_loaded", None)
        if callable(ensure):
            ensure()
        return tts
    except Exception:
        return None


def cmd_voice_setup(args: argparse.Namespace):
    """Run audio setup wizard."""
    _print_logo()
    print(f"  {_BOLD}Voice Setup Wizard{_RESET}")
    print(f"  {_DIM}{'─' * 40}{_RESET}\n")

    # 1. Microphone
    print(f"  {_BOLD}Step 1:{_RESET} Microphone... ", end="")
    sys.stdout.flush()
    try:
        from .voice.audio import list_input_devices
        devices = list_input_devices()
        if devices:
            default = next((d for d in devices if d.is_default), devices[0])
            print(f"{_GREEN}✅ Found{_RESET}")
            print(f"     {default.name} ({default.inputs} channels)")
        else:
            print(f"{_RED}❌ Not found{_RESET}")
            print(f"     {_DIM}Connect a microphone and run again.{_RESET}")
    except Exception as exc:
        print(f"{_RED}❌ Error{_RESET}")
        print(f"     {_DIM}{exc}{_RESET}")

    # 2. Speakers / TTS
    print(f"  {_BOLD}Step 2:{_RESET} Speakers... ", end="")
    sys.stdout.flush()
    try:
        tts = _await_tts_provider()
        if tts and tts.is_available:
            print(f"{_GREEN}✅ {tts.active_provider_name}{_RESET}")
            for p in tts.list_providers():
                status = f"{_GREEN}available{_RESET}" if p["available"] else f"{_RED}unavailable{_RESET}"
                internet = " [internet]" if p["requires_internet"] else ""
                print(f"     {_DIM}{p['name']}: {status} (quality: {p['quality']}){internet}{_RESET}")
        else:
            print(f"{_RED}❌ No TTS available{_RESET}")
    except Exception as exc:
        print(f"{_RED}❌ Error{_RESET}")
        print(f"     {_DIM}{exc}{_RESET}")

    # 3. Hotword
    print(f"  {_BOLD}Step 3:{_RESET} Hotword detection... ", end="")
    sys.stdout.flush()
    try:
        from .voice.hotword import HotwordDetector
        hw = HotwordDetector("hey friday")
        if hw.is_available:
            print(f"{_GREEN}✅ {hw.provider_name}{_RESET}")
        else:
            print(f"{_YELLOW}⚠ Limited{_RESET}")
    except Exception as exc:
        print(f"{_RED}❌ Error{_RESET}")
        print(f"     {_DIM}{exc}{_RESET}")

    # 4. STT
    print(f"  {_BOLD}Step 4:{_RESET} Speech recognition... ", end="")
    sys.stdout.flush()
    try:
        from .voice.stt import SpeechToText
        stt = SpeechToText()
        if stt.is_available:
            print(f"{_GREEN}✅ {stt.active_provider}{_RESET}")
        else:
            print(f"{_RED}❌ No STT available{_RESET}")
            print(f"     {_DIM}Install: pip install faster-whisper{_RESET}")
    except Exception as exc:
        print(f"{_RED}❌ Error{_RESET}")
        print(f"     {_DIM}{exc}{_RESET}")

    print(f"\n  {_BOLD}{'─' * 40}{_RESET}")
    print(f"  Run {_GREEN}friday4 talk{_RESET} to start the voice session.")
    print()
    return 0


def cmd_voice_status(args: argparse.Namespace):
    """Show voice interface status."""
    _print_logo()

    print(f"  {_BOLD}Audio Devices:{_RESET}")
    try:
        from .voice.audio import list_input_devices, list_output_devices
        inputs = list_input_devices()
        outputs = list_output_devices()
        print(f"  {_DIM}  Inputs:{_RESET} {len(inputs)}")
        for d in inputs[:3]:
            print(f"    {'●' if d.is_default else '○'} {d.name}")
        print(f"  {_DIM}  Outputs:{_RESET} {len(outputs)}")
        for d in outputs[:3]:
            print(f"    {'●' if d.is_default else '○'} {d.name}")
    except Exception:
        print(f"  {_RED}  Could not enumerate{_RESET}")

    print(f"\n  {_BOLD}Text-to-Speech:{_RESET}")
    try:
        tts = _await_tts_provider()
        if tts is None:
            raise RuntimeError("TTS unavailable")
        for p in tts.list_providers():
            status = f"{_GREEN}✓{_RESET}" if p["available"] else f"{_RED}✗{_RESET}"
            internet = " (internet)" if p["requires_internet"] else ""
            print(f"  {status} {p['name']} — {p['quality']}{internet}")
    except Exception:
        print(f"  {_RED}  Could not check{_RESET}")

    print(f"\n  {_BOLD}Speech-to-Text:{_RESET}")
    try:
        from .voice.stt import SpeechToText
        stt = SpeechToText()
        # Let faster-whisper's async load finish (bounded) so status is
        # truthful — a fresh install downloads ~400 MB on first check.
        for p in stt.list_providers():
            thread = getattr(p, "_load_thread", None)
            if thread is not None:
                thread.join(timeout=600)
        for p in stt.list_providers():
            if p["available"]:
                print(f"  {_GREEN}✓{_RESET} {p['name']}")
            else:
                print(f"  {_YELLOW}… loading{_RESET} {p['name']}")
    except Exception:
        print(f"  {_RED}  Could not check{_RESET}")

    print(f"\n  {_BOLD}Hotword & VAD:{_RESET}")
    try:
        from .voice.hotword import HotwordDetector
        hw = HotwordDetector("hey friday")
        print(f"  {_GREEN}✓{_RESET} hotword: {hw.provider_name}")
        from .voice.vad import VoiceActivityDetector
        vad = VoiceActivityDetector(mode=1)
        print(f"  {_GREEN}✓{_RESET} vad: {vad.provider_name}")
    except Exception:
        print(f"  {_RED}  Could not check{_RESET}")

    print()
    return 0


def cmd_voice_test(args: argparse.Namespace):
    """Test voice by making Friday speak a test phrase."""
    from .voice.tts import TextToSpeech

    print(f"\n{_CYAN}  ◆ FRIDAY — Voice Test{_RESET}\n")

    tts = TextToSpeech()
    if not tts.is_available:
        _print_error("No TTS available")
        return 1

    phrase = args.text or "Hello. I'm Friday, your AI operating partner."
    print(f"  {_DIM}Speaking: \"{phrase}\"{_RESET}")
    print(f"  {_DIM}Provider: {tts.active_provider_name}{_RESET}")
    print()

    tts.speak_and_wait(phrase)
    print(f"\n  {_GREEN}✅ Done{_RESET}\n")
    return 0


# ---------------------------------------------------------------------------
# Argument parsers
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Main entry point for the integrated `friday4` CLI."""
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        prog="friday4",
        description="Friday V4 — Voice Interface & Desktop Control",
    )
    subparsers = parser.add_subparsers(dest="command")

    build_talk_parser(subparsers)
    build_voice_parser(subparsers)

    from .cli_daemon import build_daemon_parser
    build_daemon_parser(subparsers)

    from .cli_doctor import build_doctor_parser
    build_doctor_parser(subparsers)

    from .cli_desktop import build_desktop_parser
    build_desktop_parser(subparsers)

    from .cli_proactive import build_proactive_parser
    build_proactive_parser(subparsers)

    from .cli_intelligence import build_intelligence_parser
    build_intelligence_parser(subparsers)

    args = parser.parse_args(argv)

    if hasattr(args, "func"):
        return args.func(args) or 0

    parser.print_help()
    return 0


def build_talk_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "talk", help="Start interactive voice session",
        description="Talk to Friday using your voice. Say 'Hey Friday' or press a key.",
    )
    parser.add_argument("--push-to-talk", "-p", action="store_true",
                        help="Use push-to-talk mode (hold key to talk)")
    parser.add_argument("--push-to-talk-key", "-k", default="ctrl+space",
                        help="Push-to-talk hotkey, e.g. ctrl+shift+m "
                             "(default: ctrl+space)")
    parser.add_argument("--tts-provider", "-t", default=None,
                        choices=["auto", "piper", "edge", "kokoro", "pyttsx3"],
                        help="TTS provider (default: config or auto: "
                             "piper, then edge, then kokoro)")
    parser.add_argument("--tts-voice", default=None,
                        help="Voice name/ID override (e.g. af_bella)")
    parser.add_argument("--no-chimes", action="store_true",
                        help="Disable audio cue chimes")
    parser.add_argument("--silero-vad", "-s", action="store_true",
                        help="Use Silero VAD (better accuracy, more CPU)")
    parser.add_argument("--silence-timeout", type=float, default=None,
                        help="Seconds of silence before stopping recording (default: 2.0)")
    parser.add_argument("--max-utterance", type=float, default=None,
                        help="Max utterance length in seconds (default: 30.0)")
    parser.add_argument("--hotword-sensitivity", type=float, default=None,
                        help="Hotword sensitivity 0.0-1.0 (default: 0.7)")
    parser.add_argument("--tts-speed", type=float, default=None,
                        help="TTS speech rate 0.5-2.0 (default: 1.15)")
    parser.set_defaults(func=cmd_talk)


def build_voice_parser(subparsers) -> None:
    voice_parser = subparsers.add_parser(
        "voice", help="Voice interface management",
        description="Manage Friday's voice interface: setup, status, test.",
    )
    voice_sub = voice_parser.add_subparsers(dest="voice_command")

    setup_parser = voice_sub.add_parser("setup", help="Run audio setup wizard")
    setup_parser.set_defaults(func=cmd_voice_setup)

    status_parser = voice_sub.add_parser("status", help="Show voice interface status")
    status_parser.set_defaults(func=cmd_voice_status)

    test_parser = voice_sub.add_parser("test", help="Test voice by speaking a phrase")
    test_parser.add_argument("text", nargs="?",
                             default="Hello. I'm Friday, your AI operating partner.",
                             help="Text to speak (default: greeting)")
    test_parser.set_defaults(func=cmd_voice_test)


if __name__ == "__main__":
    raise SystemExit(main())
