"""Proactive Conversation Engine — merged into notification.py.

This module is a backward-compatibility wrapper. The implementation
now lives in ``notification.py`` to eliminate duplicate template systems
that both modules had for the same event types.

Usage::

    from .notification import check_and_proact

All proactive functionality (signal extraction, message building,
check_and_proact entry point) has been migrated to notification.py.
This file remains for backward compatibility with existing imports.
"""

from __future__ import annotations

from .notification import check_and_proact  # noqa: F401 — re-exported for backward compat
