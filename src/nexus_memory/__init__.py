"""Nexus Memory — a local-first, SQLite-vec backed agent memory library."""

from __future__ import annotations

from .core.config import DEFAULT_DIM, NexusConfig
from .core.context import ContextAssembler
from .core.embeddings import Embedder, HashingEmbedder
from .core.orchestrator import NexusMemory
from .layers.diary import DiaryConfig, DiaryLayer, DiaryStore
from .layers.episodic.episodic import EpisodicStore
from .layers.episodic.summarization import MockSummarizer, Summarizer
from .layers.procedural.procedural import (
    DirectiveDetector,
    MockDirectiveDetector,
    ProceduralStore,
)
from .layers.semantic.extraction import (
    FactExtractor,
    MockFactExtractor,
    SpeakerAwareExtractor,
)
from .layers.working.working import WorkingMemory

__version__ = "0.4.2"

__all__ = [
    "__version__",
    "NexusMemory",
    "NexusConfig",
    "DEFAULT_DIM",
    "Embedder",
    "HashingEmbedder",
    "FactExtractor",
    "SpeakerAwareExtractor",
    "MockFactExtractor",
    # Multi-layer public surface
    "WorkingMemory",
    "EpisodicStore",
    "ProceduralStore",
    "Summarizer",
    "MockSummarizer",
    "DirectiveDetector",
    "MockDirectiveDetector",
    "ContextAssembler",
    # Diary layer (optional Layer V)
    "DiaryConfig",
    "DiaryLayer",
    "DiaryStore",
]
