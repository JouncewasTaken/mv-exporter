"""Extraction adapters.

The loader is source-agnostic: an adapter yields (external_id, raw_record) pairs
and reports the cursor for resumability. Your working Multiview document exporter
plugs in as MultiviewAdapter.extract(). DemoAdapter generates synthetic, seeded,
PHI-free rows so the pipeline runs end-to-end and is safe for public release.
"""
from __future__ import annotations

import random
from abc import ABC, abstractmethod
from datetime import date, timedelta
from typing import Iterator, Optional

MODULES = ("AP", "GL", "AR")
CURSOR_KEY = "seq"  # adapters advance a monotonic cursor for chunked resume


class ExtractAdapter(ABC):
    @abstractmethod
    def extract(self, module: str, *, since: Optional[str]) -> Iterator[tuple[Optional[str], dict, str]]:
        """Yield (external_id, raw_record, cursor_value).

        `since` is the last committed cursor_value (None on first pass). The
        adapter MUST resume strictly after it. cursor_value must be monotonic
        so checkpointing is correct across interrupted passes.
        """
        raise NotImplementedError


class MultiviewAdapter(ExtractAdapter):
    """Seam for the real Multiview export. Intentionally unimplemented here.

    Wire your proven document exporter in: page/scroll or scheduled-export the
    module, and for each source row yield (natural_key_or_None, row_dict, cursor).
    Keep yields streaming (generator) so the GL pass never materializes fully in
    memory -- that is what defeats the browser-export timeout.
    """
    def extract(self, module, *, since):
        raise NotImplementedError(
            "MultiviewAdapter.extract is a stub. Plug in the live exporter, or run with --demo."
        )


class DemoAdapter(ExtractAdapter):
    """Deterministic synthetic data. Seeded per-module => re-runs are identical,
    so idempotency and resume are testable. No real/PHI content."""

    def __init__(self, rows: int = 25):
        self.rows = rows

    def extract(self, module, *, since):
        if module not in MODULES:
            raise ValueError(f"unknown module {module!r}; expected one of {MODULES}")
        start = int(since) + 1 if since is not None else 1
        rng = random.Random(f"{module}-seed")  # stable stream per module
        base = date(2024, 1, 1)
        for seq in range(1, self.rows + 1):
            amount = round(rng.uniform(50, 9500), 2)  # advance rng even when skipping
            posted = (base + timedelta(days=seq)).isoformat()
            if seq < start:
                continue
            ext_id = f"{module}-{seq:06d}"
            record = {
                "external_id": ext_id,
                "module": module,
                "date_posted": posted,
                "amount": amount,
                "memo": f"synthetic {module} row {seq}",
            }
            yield ext_id, record, str(seq)
