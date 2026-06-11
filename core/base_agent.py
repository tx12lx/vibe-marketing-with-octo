"""core/base_agent.py — Abstract base class for all Agent Mesh workers.

This module is the only file in the project-root core/ package (distinct from
Vibe OCTO Nexus/core/).  Every registered worker must implement this contract.
"""
from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

# Guarantee the project root is importable so pydantic_schemas resolves from here.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pydantic import BaseModel  # noqa: E402
from pydantic_schemas import UniversalJSONSpec  # noqa: E402


class BaseAgent(ABC):
    """Abstract contract for all registered workers in the Agent Mesh.

    Every worker must declare class-level:
      WORKER_ID:        str                — unique versioned ID, e.g. "quant_v1"
      HANDLED_INTENTS:  frozenset[str]     — intent types this agent handles;
                                             used by _discover_agents() to build
                                             the intent routing table automatically.
      INPUT_SCHEMA:     type[BaseModel]    — Pydantic model accepted by subscribe()
      OUTPUT_SCHEMA:    type[BaseModel]    — Pydantic model returned by execute()

    Orchestrator lifecycle: subscribe() -> set_session_context() -> execute()

    Workers must never raise raw exceptions to the orchestrator.
    Wrap all failures in NexusErrorPayload or a worker-specific error model.
    """

    WORKER_ID: str
    HANDLED_INTENTS: frozenset[str] = frozenset()
    INPUT_SCHEMA: type[BaseModel]
    OUTPUT_SCHEMA: type[BaseModel]

    @abstractmethod
    def subscribe(self, spec: UniversalJSONSpec) -> None:
        """Accept the UniversalJSONSpec for this execution cycle."""

    @abstractmethod
    def execute(self) -> BaseModel:
        """Execute the worker pipeline and return a validated output model."""

    async def execute_async(self) -> Any:
        """Async wrapper for execute() — enables concurrent multi-agent calls.

        Default implementation delegates to the synchronous execute().
        Override in agents that have true async I/O (e.g. aiohttp BQ queries).
        Used by the orchestrator when two independent agents run in parallel,
        e.g. QuantAgent + BriefingAgent for campaign_execution intent.
        """
        import asyncio
        return await asyncio.get_event_loop().run_in_executor(None, self.execute)

    def set_session_context(self, context: str) -> None:
        """Receive glossary/catalog context injected by the orchestrator."""

    def set_runtime_schema(self, schema_str: str) -> None:
        """Receive live schema context injected by the orchestrator (Pillar 2)."""
