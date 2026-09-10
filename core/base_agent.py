"""core/base_agent.py — Abstract base class for all Agent Mesh workers.

Every registered worker (see agents/*.py) must implement this contract.
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


class BaseAgent(ABC):
    """Abstract contract for all registered workers in the Agent Mesh.

    Every worker must declare class-level:
      WORKER_ID:        str                — unique versioned ID, e.g. "quant_v1"
      HANDLED_INTENTS:  frozenset[str]     — intent types this agent handles;
                                             used by _discover_agents() to build
                                             the agent registry automatically.
      INPUT_SCHEMA:     type[BaseModel]    — Pydantic model this agent's real
                                             entrypoint accepts
      OUTPUT_SCHEMA:    type[BaseModel]    — Pydantic model this agent's real
                                             entrypoint returns

    subscribe()/execute() exist to satisfy this contract uniformly, but an
    agent whose real entrypoint takes a different shape of input (NexusAgent's
    classify_intent(), QuantAgent's direct_count()) documents that in its own
    subscribe()/execute() docstrings rather than being forced through them.

    Workers must never raise raw exceptions to the orchestrator.
    Wrap all failures in NexusErrorPayload or a worker-specific error model.
    """

    WORKER_ID: str
    HANDLED_INTENTS: frozenset[str] = frozenset()
    INPUT_SCHEMA: type[BaseModel]
    OUTPUT_SCHEMA: type[BaseModel]

    @abstractmethod
    def subscribe(self, spec) -> None:
        """Accept input for this execution cycle."""

    @abstractmethod
    def execute(self) -> BaseModel:
        """Execute the worker pipeline and return a validated output model."""

    async def execute_async(self) -> Any:
        """Async wrapper for execute() — enables concurrent multi-agent calls.

        Default implementation delegates to the synchronous execute().
        Override in agents that have true async I/O (e.g. aiohttp BQ queries).
        """
        import asyncio
        return await asyncio.get_event_loop().run_in_executor(None, self.execute)

    def set_session_context(self, context: str) -> None:
        """Receive glossary/catalog context injected by the orchestrator."""
