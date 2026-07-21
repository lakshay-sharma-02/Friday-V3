"""LLM response parsing — must handle single objects, SSE streams, and the
trailing `data: [DONE]` some proxies append after a JSON object."""

from __future__ import annotations

from friday.llm import _extract_content


def test_single_object():
    body = '{"object":"chat.completion","choices":[{"message":{"content":"hello"}}]}'
    assert _extract_content(body) == "hello"


def test_sse_stream():
    body = (
        'data: {"object":"chat.completion.chunk","choices":[{"delta":{"content":"he"}}]}\n'
        'data: {"object":"chat.completion.chunk","choices":[{"delta":{"content":"llo"}}]}\n'
        'data: [DONE]\n'
    )
    assert _extract_content(body) == "hello"


def test_object_with_trailing_done():
    # oc/* models return a JSON object immediately followed by `data: [DONE]`.
    body = (
        '{"object":"chat.completion","choices":'
        '[{"message":{"content":"OK","reasoning_content":"thinking"}}]}'
        'data: [DONE]\n'
    )
    assert _extract_content(body) == "OK"


def test_object_with_inline_done_no_newline():
    body = (
        '{"object":"chat.completion","choices":'
        '[{"message":{"content":"x"}}]}data: [DONE]'
    )
    assert _extract_content(body) == "x"


def test_empty():
    assert _extract_content("") is None
    assert _extract_content("   ") is None
