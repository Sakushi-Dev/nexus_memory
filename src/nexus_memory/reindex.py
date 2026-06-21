"""Top-level shim so ``python -m nexus_memory.reindex`` works (0.7.0).

The implementation lives in :mod:`nexus_memory.core.reindex`; this module just
re-exports its public surface and CLI so the documented command line is short.
"""

from __future__ import annotations

import sys

from .core.reindex import main, reembed

__all__ = ["main", "reembed"]


if __name__ == "__main__":  # pragma: no cover - CLI shim
    sys.exit(main())
