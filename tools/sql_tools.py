"""tools/sql_tools.py — Tool schema for structured waterfall SQL output.

Design-time only.  Activation: set FUELIX_USE_TOOL_CALLING=1 and confirm
Fuel iX supports tool use.  When active, QuantAgent uses this instead of
asking the model to produce raw SQL text and parsing CTE names from it.

Migration path:
  Phase 3E now: schema defined here, text-SQL approach still used in quant_agent.py
  When Fuel iX confirms tool use: flip use_tool_calling flag, remove SQL text parsing
  After tool use is live: refactor QuantAgent to two-round-trip pattern:
    Round 1 — model reads domain catalog, picks views and columns
    Round 2 — model calls define_waterfall_steps with confirmed filter clauses
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Waterfall step definition tool
# ---------------------------------------------------------------------------

define_waterfall_steps: dict = {
    "name": "define_waterfall_steps",
    "description": (
        "Define the waterfall CTE steps for this audience sizing request. "
        "Step 1 must be 'Base Universe'. The last step must be 'Final Targetable Audience' "
        "and always includes control_group_flg = 'N'. Middle steps apply meaningful filters — "
        "pass-through CTEs that add no filter are forbidden. "
        "Minimum 3 steps, maximum 10 steps."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "lob": {
                "type": "string",
                "enum": ["mobility", "ffh"],
                "description": "Line of business — determines base table",
            },
            "steps": {
                "type": "array",
                "minItems": 3,
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "properties": {
                        "step_name": {
                            "type": "string",
                            "description": (
                                "Plain business language label (e.g. 'After: Ontario & BC Only'). "
                                "No SQL identifiers or column names in the label."
                            ),
                        },
                        "filter_clause": {
                            "type": "string",
                            "description": (
                                "BigQuery SQL WHERE clause fragment for this step. "
                                "Must reference only confirmed column names from the schema."
                            ),
                        },
                        "join_table": {
                            "type": ["string", "null"],
                            "description": (
                                "Fully-qualified BQ table to JOIN in this step (e.g. GCH). "
                                "Null for filter-only steps."
                            ),
                        },
                        "join_clause": {
                            "type": ["string", "null"],
                            "description": "JOIN ON clause when join_table is set. Null otherwise.",
                        },
                    },
                    "required": ["step_name", "filter_clause"],
                },
                "description": "Ordered list of waterfall steps",
            },
            "sizing_aggregate": {
                "type": "string",
                "enum": ["COUNT(DISTINCT ban)", "COUNT(DISTINCT BACCT_NUM)"],
                "description": "Sizing aggregate — ban for mobility, BACCT_NUM for FFH",
            },
        },
        "required": ["lob", "steps", "sizing_aggregate"],
    },
}

# ---------------------------------------------------------------------------
# Business rule extraction tool (FeedbackAgent)
# ---------------------------------------------------------------------------

extract_business_rules: dict = {
    "name": "extract_business_rules",
    "description": (
        "Extract all distinct business rules from the user's correction. "
        "Each rule must be independently actionable. "
        "Classify scope as campaign (applies to one campaign), pattern (applies to campaigns "
        "with matching attributes), or universal (applies to all future requests)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "rules": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "rule_description": {
                            "type": "string",
                            "description": "Plain English description of the rule",
                        },
                        "rule_type": {
                            "type": "string",
                            "enum": [
                                "filter_add",
                                "exclusion_add",
                                "lookback_days",
                                "population_note",
                                "general",
                            ],
                        },
                        "scope": {
                            "type": "string",
                            "enum": ["campaign", "pattern", "universal"],
                        },
                        "structured_value": {
                            "type": "object",
                            "description": "Rule-type-specific payload: sql, filter, note, days, etc.",
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                        "ambiguity_note": {
                            "type": ["string", "null"],
                            "description": "What is unclear about this rule, if anything",
                        },
                    },
                    "required": ["rule_description", "rule_type", "scope", "structured_value", "confidence"],
                },
            },
            "new_glossary_terms": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "term": {"type": "string"},
                        "definition": {"type": "string"},
                        "sql_filter": {"type": ["string", "null"]},
                    },
                    "required": ["term", "definition"],
                },
                "description": "New business terms discovered in this correction",
            },
        },
        "required": ["rules"],
    },
}
