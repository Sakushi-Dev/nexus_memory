"""The single extension seam for all background aux jobs: :class:`JobHandler`.

A :class:`JobHandler` owns one or more job ``kinds`` and knows how to (1) defensively
parse a host-supplied raw result into a structured value and (2) apply that value
to the store (under the shared write lock), optionally enqueuing cascade jobs.

The :class:`~nexus_memory.core.auxbus.bus.AuxBus` holds a ``dict[str, JobHandler]``
registry and dispatches a submitted result to the handler that owns the job's
``kind``. Adding a new background job type means adding a new ``JobHandler``
subclass and registering it in the owning layer's constructor — zero orchestrator
or table changes.

The module is fully offline and deterministic; it never imports or calls any
network/LLM SDK.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class JobHandler(ABC):
    """Base class for an aux-job handler bound to one or more job ``kinds``.

    Subclasses declare the ``kinds`` they own (the discriminators the bus routes
    on) and an ``output_format`` doc/routing hint, then implement the two
    abstract methods.
    """

    # The discriminators this handler owns; the bus registers the handler under
    # each kind in this tuple.
    kinds: tuple[str, ...] = ()

    # A doc/routing hint for the host: "text" (prose) or "json" (strict JSON).
    output_format: str = "text"

    # ================================================================== #
    # the seam
    # ================================================================== #
    @abstractmethod
    def parse_result(self, raw: str, job: dict) -> Any:
        """Parse a host-supplied raw result into a structured value.

        MUST be defensive: it never raises. A malformed result maps to a safe
        sentinel (e.g. ``[]`` = all-NOOP for a JSON handler; identity for a prose
        handler).
        """
        raise NotImplementedError

    @abstractmethod
    def apply(self, parsed: Any, job: dict) -> dict:
        """Commit ``parsed`` for ``job`` under the shared ``db.lock``.

        May enqueue cascade jobs. Returns
        ``{"status": "success", "applied": <kind>}``.
        """
        raise NotImplementedError
