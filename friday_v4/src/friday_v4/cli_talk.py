"""CLI commands for `friday talk` — interactive voice session with Friday.

Usage:
    friday talk                          # Hotword mode ("Hey Friday")
    friday talk --push-to-talk           # Hold key to talk
    friday talk --tts-provider kokoro    # Use specific TTS engine
    friday talk --no-chimes              # Disable audio cues
    friday voice setup / status / test   # Voice management
"""

from __future__ import annotations

import argparse
import logging
import sys

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
    print(f"  {_DIM}  Say \"Hey Friday\" or press Ctrl+Space to talk{_RESET}")
    print(f"  {_DIM}  Type 'exit' to quit{_RESET}")
    print(f"  {_DIM}  Type 'help' for this menu{_RESET}")
    print()


# ---------------------------------------------------------------------------
# CLI Commands
# ---------------------------------------------------------------------------


def cmd_talk(args: argparse.Namespace):
    """Start interactive voice session with Friday."""
    from .voice.pipeline import VoicePipeline, PipelineConfig
    from .voice.router import VoiceRouter

    _print_logo()

    config = PipelineConfig(
        hotword="" if args.push_to_talk else "hey friday",
        hotword_sensitivity=0.7,
        vad_mode=3 if args.silero_vad else 1,
        tts_provider=args.tts_provider or "kokoro",
        enable_chimes=not args.no_chimes,
        silence_timeout_seconds=args.silence_timeout or 2.0,
    )

    pipeline = VoicePipeline(config)
    router = VoiceRouter(pipeline, enable_proactive=True)

    pipeline.on_transcription = lambda text: _print_you(text)
    pipeline.on_state_change = lambda s: _print_state(s.value)
    pipeline.on_error = lambda err: _print_error(err)
    pipeline.route_function = router.route

    if not pipeline.start():
        _print_error("Failed to start voice pipeline. Run 'friday voice setup' to diagnose.")
        return 1

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
                _print_you(user_input)
                response = router.route(user_input)
                if response:
                    _print_friday(response)
                    pipeline.speak(response)
    except KeyboardInterrupt:
        print()
    finally:
        pipeline.stop()
        router.cleanup()
        print(f"\n{_DIM}  Voice session ended.{_RESET}\n")

    return 0


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
        from .voice.tts import TextToSpeech
        tts = TextToSpeech()
        if tts.is_available:
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
    print(f"  Run {_GREEN}friday talk{_RESET} to start the voice session.")
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
        from .voice.tts import TextToSpeech
        tts = TextToSpeech()
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
    """Main entry point for the integrated `friday` CLI."""
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        prog="friday",
        description="Friday V4 — Voice Interface & Desktop Control",
    )
    subparsers = parser.add_subparsers(dest="command")

    build_talk_parser(subparsers)
    build_voice_parser(subparsers)

    from .cli_desktop import build_desktop_parser
    build_desktop_parser(subparsers)

    from .cli_proactive import build_proactive_parser
    build_proactive_parser(subparsers)

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
    parser.add_argument("--tts-provider", "-t", default="kokoro",
                        choices=["kokoro", "edge", "pyttsx3"],
                        help="TTS provider to use (default: kokoro)")
    parser.add_argument("--no-chimes", action="store_true",
                        help="Disable audio cue chimes")
    parser.add_argument("--silero-vad", "-s", action="store_true",
                        help="Use Silero VAD (better accuracy, more CPU)")
    parser.add_argument("--silence-timeout", type=float, default=2.0,
                        help="Seconds of silence before stopping recording (default: 2.0)")
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
