"""Confidence aggregation shared infrastructure.

Shared scoring logic extracted from domain-specific confidence modules
(understanding, initiative, insight). Each domain provides its own
Contributor dataclass, weight/threshold config, and cross-source multiplier.
"""
