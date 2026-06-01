from __future__ import annotations

from abc import ABC, abstractmethod


class BaseConnector(ABC):
    """Base class for all downstream tool connectors.

    Each connector transforms the master brief JSON into a tool-specific
    output slice. Connectors are stateless — they read brief_json and
    glossary_data, and return a dict with at minimum a 'status' key.

    To add a new downstream tool:
    1. Create a subclass here with name = "your_tool"
    2. Implement generate()
    3. Register it in connectors/__init__.py
    """

    name: str = ""

    @abstractmethod
    def generate(self, brief_json: dict, glossary_data: dict) -> dict:
        """Transform brief JSON into connector-specific output.

        Args:
            brief_json: Structured brief JSON from the learn/translate phase.
            glossary_data: Full glossary dict (terms + any BQ mappings already added).

        Returns:
            Connector output dict. Must include 'status' key:
            - 'ready': connector output is complete and usable
            - 'pending_bq_mapping': waiting on BQ field mappings to be added
            - 'incomplete': required brief fields are missing
            - 'error': connector raised an exception
        """
        ...
