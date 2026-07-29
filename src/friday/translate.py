"""Real-time Translation — translate text between languages with caching.

Uses multiple backends (argos-translate, LibreTranslate, Google Translate API)
with automatic fallback. All translation is optional — if no engine is
available, everything stays in original language.

Usage::

    from friday.translate import translate, detect_language

    text = translate("hello world", "en", "es")
    lang = detect_language("hola mundo")  # "es"
"""

from __future__ import annotations

import hashlib
import os
from typing import Optional

from .db import now_iso


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

#: ISO 639-1 language codes Friday supports for detection/translation.
SUPPORTED_LANGUAGES = {
    "en": "English", "es": "Spanish", "fr": "French", "de": "German",
    "it": "Italian", "pt": "Portuguese", "ru": "Russian", "zh": "Chinese",
    "ja": "Japanese", "ko": "Korean", "ar": "Arabic", "hi": "Hindi",
    "nl": "Dutch", "pl": "Polish", "sv": "Swedish", "tr": "Turkish",
    "vi": "Vietnamese", "th": "Thai", "el": "Greek", "he": "Hebrew",
    "cs": "Czech", "ro": "Romanian", "hu": "Hungarian", "fi": "Finnish",
    "da": "Danish", "uk": "Ukrainian", "no": "Norwegian", "id": "Indonesian",
    "ms": "Malay", "bn": "Bengali",
}


def detect_language(text: str) -> str:
    """Detect the language of a text string.

    Uses ``lingua`` (LanguageDetectorBuilder) if available for accurate
    detection. Falls back to a simple character-range heuristic.

    Returns an ISO 639-1 language code (e.g. ``"en"``, ``"es"``, ``"fr"``).
    Returns ``"en"`` on failure.
    """
    if not text or not text.strip():
        return "en"

    try:
        from lingua import LanguageDetectorBuilder
        detector = LanguageDetectorBuilder.from_all_languages().with_preloaded_language_models().build()
        result = detector.detect_language_of(text)
        if result:
            return str(result.iso_code_639_1()).lower()
    except Exception:
        pass

    # Fallback: character-range heuristics.
    text_lower = text.lower()
    # CJK characters
    cjk_count = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u3040' <= c <= '\u30ff')
    if cjk_count > len(text) * 0.1:
        return "zh"
    # Cyrillic
    cyrillic_count = sum(1 for c in text if '\u0400' <= c <= '\u04ff')
    if cyrillic_count > len(text) * 0.1:
        return "ru"
    # Arabic
    arabic_count = sum(1 for c in text if '\u0600' <= c <= '\u06ff')
    if arabic_count > len(text) * 0.1:
        return "ar"
    # Common words heuristic for Western languages (word-boundary matching).
    words = set(text_lower.split())
    if any(w in words for w in ["el", "la", "los", "las", "del", "con", "por", "para", "una", "más"]):
        return "es"
    if any(w in words for w in ["le", "la", "les", "des", "dans", "avec", "pour", "sur", "c'est", "nous", "vous"]):
        return "fr"
    if any(w in words for w in ["der", "die", "das", "mit", "und", "auf", "für", "nicht", "ein", "eine"]):
        return "de"
    if any(w in words for w in ["il", "la", "lo", "gli", "con", "per", "non", "che", "del", "della"]):
        return "it"
    if any(w in words for w in ["o", "a", "os", "as", "do", "da", "com", "para", "não", "é"]):
        return "pt"

    return "en"


# ---------------------------------------------------------------------------
# Translation backends
# ---------------------------------------------------------------------------


def _translate_argos(text: str, source: str, target: str) -> Optional[str]:
    """Translate using argos-translate (local, no API)."""
    try:
        import argostranslate.package
        import argostranslate.translate

        # Check if the language pair is available.
        from_code = source
        to_code = target
        installed = argostranslate.translate.get_installed_languages()
        from_lang = next((l for l in installed if l.code == from_code), None)
        to_lang = next((l for l in installed if l.code == to_code), None)
        if from_lang and to_lang:
            translation = from_lang.get_translation(to_lang)
            if translation:
                return translation.translate(text)
        return None
    except Exception:
        return None


def _translate_libretranslate(text: str, source: str, target: str) -> Optional[str]:
    """Translate using a self-hosted LibreTranslate instance."""
    url = os.environ.get("FRIDAY_LIBRETRANSLATE_URL", "")
    api_key = os.environ.get("FRIDAY_LIBRETRANSLATE_API_KEY", "")
    if not url:
        return None
    try:
        import urllib.request, urllib.parse, json
        payload = json.dumps({
            "q": text, "source": source, "target": target,
            "format": "text", "api_key": api_key or None,
        }).encode("utf-8")
        req = urllib.request.Request(
            url.rstrip("/") + "/translate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("translatedText")
    except Exception:
        return None


def _translate_google(text: str, source: str, target: str) -> Optional[str]:
    """Translate using Google Translate API (requires API key).

    Set ``GOOGLE_TRANSLATE_API_KEY`` in the environment.
    """
    api_key = os.environ.get("GOOGLE_TRANSLATE_API_KEY", "")
    if not api_key:
        return None
    try:
        import urllib.request, urllib.parse, json
        params = urllib.parse.urlencode({
            "q": text, "source": source, "target": target, "key": api_key,
        })
        url = f"https://translation.googleapis.com/language/translate/v2?{params}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("data", {}).get("translations", [{}])[0].get("translatedText")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Main translate function
# ---------------------------------------------------------------------------

_BACKENDS = [
    ("argos", _translate_argos),
    ("libre", _translate_libretranslate),
    ("google", _translate_google),
]


def translate(
    text: str,
    source: str = "en",
    target: str = "es",
    conn=None,
) -> str:
    """Translate text from source language to target language.

    Tries backends in order: argos-translate → LibreTranslate → Google API.
    Caches results in the ``translation_cache`` table when ``conn`` is provided.
    Returns the original text if no backend is available.

    Args:
        text: Text to translate.
        source: ISO 639-1 source language code.
        target: ISO 639-1 target language code.
        conn: Optional DB connection for caching.

    Returns:
        Translated text, or original text if translation unavailable.
    """
    if source == target or not text.strip():
        return text

    # Check cache first.
    if conn:
        text_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
        try:
            row = conn.execute(
                "SELECT translated_text FROM translation_cache "
                "WHERE text_hash = ? AND source_lang = ? AND target_lang = ?",
                (text_hash, source, target),
            ).fetchone()
            if row:
                return row["translated_text"]
        except Exception:
            pass

    # Try each backend.
    for name, backend in _BACKENDS:
        try:
            result = backend(text, source, target)
            if result:
                # Cache the result.
                if conn:
                    try:
                        conn.execute(
                            "INSERT OR REPLACE INTO translation_cache "
                            "(text_hash, source_lang, target_lang, translated_text, created_at) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (text_hash, source, target, result, now_iso()),
                        )
                        conn.commit()
                    except Exception:
                        pass
                return result
        except Exception:
            continue

    return text


def get_operator_language(conn) -> str:
    """Get the operator's preferred language from their profile."""
    try:
        row = conn.execute(
            "SELECT value FROM operator_preferences WHERE key = 'language'"
        ).fetchone()
        if row and row["value"] in SUPPORTED_LANGUAGES:
            return row["value"]
    except Exception:
        pass
    return "en"
