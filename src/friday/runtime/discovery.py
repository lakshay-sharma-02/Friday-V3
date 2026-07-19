"""Capability discovery (M10). READ-ONLY reality scan. Does NOT mutate the
registry. Produces a DiscoveryResult; a separate availability-sync step
updates registry rows."""
from __future__ import annotations
import os
import shutil
from dataclasses import dataclass, field
from typing import List


@dataclass
class DiscoveryResult:
    available: List[str] = field(default_factory=list)
    unavailable: List[str] = field(default_factory=list)
    missing_deps: dict = field(default_factory=dict)


def discover(workers: List[dict]) -> DiscoveryResult:
    """Scan each declared worker's `requirements`.

    A requirement is satisfied if it is a PATH binary (shutil.which) OR an
    environment variable that is set (API workers). Returns availability per
    worker_id. Never raises on a missing binary."""
    res = DiscoveryResult()
    for w in workers:
        wid = w["worker_id"]
        reqs = w.get("requirements", []) or []
        missing = []
        for r in reqs:
            is_binary = shutil.which(r) is not None
            is_env = r in os.environ
            if not (is_binary or is_env):
                missing.append(r)
        if missing:
            res.unavailable.append(wid)
            res.missing_deps[wid] = missing
        else:
            res.available.append(wid)
    return res
