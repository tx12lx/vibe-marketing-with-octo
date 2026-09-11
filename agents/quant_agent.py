"""
Vibe OCTO Quant — Technical Auditor Agent

Receives a validated AdHocSizingRequest from Nexus (direct_count) and:
  1. Generates a BigQuery waterfall SQL query via the knowledge-grounded prompt.
  2. Executes the query against BigQuery using ADC credentials.
  3. Masks customer PII in Python before returning results.
  4. Parses the waterfall rows and computes an optimization note.
  5. Returns a QuantAuditLog on success, or a NexusErrorPayload on any failure —
     never raises raw exceptions to the orchestrator.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import warnings
from pathlib import Path
from typing import Optional, Union

from dotenv import load_dotenv
from core.resilience import resilient_bq_query
from core.ai_client import ask_ai

_log = logging.getLogger(__name__)

_AGENTS_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _AGENTS_DIR.parent

for _p in [str(_ROOT_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

load_dotenv(_ROOT_DIR / ".env")

from pydantic_schemas import (
    AdHocSizingRequest,
    NexusErrorPayload,
    QuantAuditLog,
    WaterfallLayer,
)
from core.base_agent import BaseAgent
from core.thought_display import ThoughtDisplay  # noqa: E402

_QUANT_SYSTEM = (
    "You are Vibe OCTO Quant, a BigQuery technical auditor for a Canadian telecom "
    "marketing team. "
    "You translate audience sizing requests into precise BigQuery Standard SQL waterfall "
    "queries.\n\n"
    "All table names, column meanings, sensitive-column handling, and business rules "
    "(what a term like 'postpaid' means, which values a status column actually takes, "
    "and so on) are provided separately, below, from the knowledge layer -- not hardcoded "
    "here. Treat anything marked '(confirmed)' as ground truth. Treat anything marked "
    "'(unconfirmed guess)' as tentative -- you may still use it, but say so plainly in your "
    "response if a result depends on an unconfirmed guess, so a human can verify it.\n\n"
    "=== GENERATION RULES ===\n"
    "- Never select customer PII (names, addresses, emails, phone numbers, IMEI)\n"
    "- Build a waterfall: a WITH-clause of CTEs (Common Table Expressions -- named,\n"
    "  reusable intermediate query steps), where each CTE narrows the population by\n"
    "  exactly one meaningful filter, cumulatively, on top of the previous CTE's output.\n"
    "  The first CTE is the full starting population ('Base Universe'); the last CTE is\n"
    "  the fully-filtered result ('Final Targetable Audience'). No pass-through CTEs\n"
    "  that apply no real filter.\n"
    "- Name each step in plain business language, not column names or SQL identifiers.\n"
    "- Always return exactly two output columns: layer_name STRING, audience_count INT64,\n"
    "  one row per CTE via UNION ALL, with explicit aliases on every arm (never rely on\n"
    "  positional column resolution).\n"
    "- Carry all columns forward at each step (SELECT * from the prior CTE, then filter)\n"
    "  so any later step can reference any column without needing to rejoin the base table.\n"
    "- Use Standard SQL syntax; backtick-quote all table references as `project.dataset.table`\n"
    "- Return ONLY the raw SQL -- no markdown, no explanation, no trailing semicolon\n"
    "- Case-sensitive STRING columns should generally be wrapped in UPPER() for comparison;\n"
    "  use the exact values given in the knowledge layer below for any column with confirmed\n"
    "  value notes, rather than a wildcard LIKE match, whenever exact values are known.\n\n"
    "=== OUTPUT FORMAT — ABSOLUTE REQUIREMENT ===\n"
    "The response MUST be 100% executable BigQuery Standard SQL and nothing else.\n"
    "PROHIBITED — the response must NEVER contain:\n"
    "  - Any English prose, commentary, or explanation of any kind\n"
    "  - Introductory phrases such as 'Here is the query', 'The following query',\n"
    "    'This SQL will', 'Sure!', or any conversational prefix whatsoever\n"
    "  - Markdown code fences (``` or ```sql)\n"
    "  - A trailing semicolon\n"
    "  - Any text appearing before the opening WITH or SELECT keyword\n"
    "  - Any text appearing after the final SELECT of the waterfall UNION ALL\n"
    "The very first character of the response must be 'W' (WITH) or 'S' (SELECT).\n"
    "Emitting any prose causes an immediate parse failure in the execution pipeline."
)

# ---------------------------------------------------------------------------
# FFH table awareness — injected whenever a query targets bq_dly_dbm_customer_profl
# ---------------------------------------------------------------------------

_FFH_TABLE = "bq_dly_dbm_customer_profl"

# Lowercase signals that identify an FFH/Home Solutions request
_FFH_SIGNALS_LOWER = frozenset([
    "ffh", "home solutions", "naked ffh", "dbm", "customer_profl",
    "bq_dly_dbm_customer_profl", "mnh_mob_ban", "naked home",
    "home phone customer", "internet customer", "residential customer",
])

# Runtime override injected into the prompt when an FFH table is detected.
# Supersedes the mobility-centric waterfall template above.
_FFH_WATERFALL_OVERRIDE = """\

=== FFH TABLE COLUMN OVERRIDE (HIGHEST AUTHORITY — supersedes ALL definitions above) ===
This request targets HOME SOLUTIONS (FFH) customers.
MANDATORY base table: `{bq_project}.adobe.bq_dly_dbm_customer_profl`

CONFIRMED column names for this table (use ONLY these — ignore TABLE 4 column list above):
  SIZE AGGREGATE  : COUNT(DISTINCT BACCT_NUM)  — NEVER use ban (does not exist)
  BACCT_NUM       : the account key (replaces ban everywhere)
  EX_STANDARD_EX  : standard exclusions flag (INT64, 0 = not excluded)
  FFH_STOPSELL_IND: stop sell flag (INT64, 0 = not stop-sold)
  CONTROL_GROUP_FLG: control group (STRING, 'N' = not in control group)
  SERV_PROV       : province (STRING, 2-letter code)
  MNH_MOB_BAN     : mobility BAN link (IS NULL = naked FFH, no linked mobility plan)
  CC_DNEM         : email DNC (replaces em_dnc)
  CC_DNSM         : SMS DNC (replaces sms_dnc)
  CC_DNRS         : outbound DNC (replaces ob_dnc)
  CC_DNDM         : direct mail DNC (replaces dm_dnc)

COLUMNS THAT DO NOT EXIST IN THIS TABLE — do not reference them:
  ban, standard_exclusions, primary_sub, sub_status, stop_sell, lob_desc,
  em_dnc, sms_dnc, ob_dnc, dm_dnc, province, control_group_flg (lowercase)

WATERFALL CTE STRUCTURE FOR FFH (dynamic middle — replaces the mobility template above):
  base_universe (always CTE 1, label "Base Universe"):
    SELECT * FROM `{bq_project}.adobe.bq_dly_dbm_customer_profl`
    WHERE EX_STANDARD_EX = 0
    [NO lob_desc filter; NO primary_sub filter — neither column exists in FFH table]

  Dynamic middle CTEs (include only steps that apply meaningful filters):
    Stop Sell (always include for FFH):
      SELECT * FROM <prior> WHERE FFH_STOPSELL_IND = 0
    Geographic Filter (when province scope specified):
      Use SERV_PROV for province filtering
    Targeting Criteria (when request-specific targeting applies):
      Use MNH_MOB_BAN for mobility link checks
    Channel Governance (when DNC constraints apply, must precede final anchor):
      Use CC_ DNC flags: CC_DNEM (email), CC_DNSM (SMS), CC_DNRS (outbound), CC_DNDM (direct mail)
    [Skip "Primary Subscriber" and "Standard Exclusions" — these columns do not exist in FFH table]

  final_targetable_audience (always last CTE, label "Final Targetable Audience"):
    SELECT * FROM <prior_cte> WHERE CONTROL_GROUP_FLG = 'N'

FINAL SELECT: UNION ALL of COUNT(DISTINCT BACCT_NUM) from each CTE with step_order for deterministic row ordering.
Schema identical for every arm:
  SELECT <N> AS step_order, '<step_label>' AS layer_name, COUNT(DISTINCT BACCT_NUM) AS audience_count FROM <cte_name>
  UNION ALL ...
  ORDER BY step_order
N starts at 1 for "Base Universe" and increments by 1 for each arm.
First arm label: "Base Universe" (step_order=1). Last arm label: "Final Targetable Audience".
"""


def _is_ffh_request(
    request: "AdHocSizingRequest",
) -> bool:
    """Return True if the request targets the FFH/Home Solutions table."""
    text = " ".join(filter(None, [
        request.target_population or "",
        " ".join(request.filters or []),
        request.optimization_context or "",
    ])).lower()
    return any(sig in text for sig in _FFH_SIGNALS_LOWER)


_EXTREME_DROP = 0.60
_HIGH_SCRUB_RATE = 0.80

# Total SQL-generation attempts for one sizing request (1 initial + retries-with-error-hint).
# Confirmed live that a compound OR/AND condition (e.g. "BYOD, or MTM, or contract ending in
# 30 days") can produce a syntactically malformed query more than once in a row -- a single
# retry isn't always enough for that class of correction, even though it's plenty for the
# simpler mismatched-column-count failures this mechanism was first built for.
_MAX_SQL_ATTEMPTS = 3

# Anchor labels for waterfall sort: "Base Universe" is always first,
# "Final Targetable Audience" and the legacy "Universal Control Group" label are always last.
_WATERFALL_ANCHORS_FIRST = frozenset(["Base Universe"])
_WATERFALL_ANCHORS_LAST = frozenset([
    "Final Targetable Audience",
    "After: Universal Control Group",  # backwards-compat with pre-Phase-2 sessions
])

_ADHOC_WATERFALL_PROMPT = """Generate a BigQuery audience waterfall query for this ad-hoc sizing request.

Population : {target_population}
Filters    : {filters_json}
BQ Project : {bq_project}
BQ Dataset : {bq_dataset}

Available schema:
{schema_context}

Waterfall structure required — two fixed anchors with request-determined middle steps.
Total CTEs: 3-10 (choose based on which filters actually apply; no pass-throughs allowed):

  CTE 1 (always): "Base Universe"
    SELECT * FROM `{bq_project}.{bq_dataset}.<table>` WHERE UPPER(lob_desc) IN (...) AND standard_exclusions = 0

  Dynamic middle CTEs — include only when the filter applies:
    Active Eligible Subscribers  : WHERE primary_sub = 1 AND sub_status = 'A' AND standard_exclusions = 0 AND stop_sell = 0
    Geographic Filter            : province scope (when specified in Filters)
    Product Eligibility          : ownership/eligibility pairs (when cross-sell in Filters)
    Lifecycle Window             : commit_end_date / T-X window (when specified)
    NBA Model Filter             : Table 2 join (when propensity model in Filters)
    Channel Governance           : DNC flag constraints; must precede "Final Targetable Audience"
                                   Apply channel exclusivity sieve when 'only', 'exclusively',
                                   or 'solely' pairs with a channel: named = 0, unnamed = 1.
                                   Four governed flags: em_dnc, sms_dnc, ob_dnc, dm_dnc.

  Last CTE (always): "Final Targetable Audience"
    SELECT * FROM <prior_cte> WHERE control_group_flg = 'N'
    Label must be exactly "Final Targetable Audience"

Counting unit — use whatever CONFIRMED BUSINESS RULES above says to count (e.g. individual
subscriber rows vs. distinct billing accounts). Only if no confirmed rule addresses this at all,
default to COUNT(*) (one row = one customer). Never default to COUNT(DISTINCT ban) — apply it
only when a confirmed business rule explicitly calls for counting distinct billing accounts.
The final SELECT is a UNION ALL of that same counting expression from each CTE in sequence order.
Every arm MUST carry explicit column aliases — no arm may omit step_order, AS layer_name, or AS audience_count.
Schema identical for every arm (step_order integer keeps BigQuery from reordering rows):
  SELECT <N> AS step_order, '<step_label>' AS layer_name, <same counting expression as every other arm> AS audience_count FROM <cte_name>
  UNION ALL ...
  ORDER BY step_order
N starts at 1 for "Base Universe" and increments by 1 for each subsequent arm.
First arm is always "Base Universe" (step_order=1); last arm is always "Final Targetable Audience".
control_group_flg = 'N' must NOT appear in any CTE above "Final Targetable Audience".

CTE structure rules — non-negotiable:
- Linear SELECT * inheritance: every CTE selects ALL columns from the immediately preceding
  CTE so that downstream WHERE clauses can reference any column without ambiguity.
    <any_cte>  : SELECT * FROM <prior_cte> WHERE <this_step_filter>
    EXCEPTION: when joining Table 2 or a self-join, alias the preceding CTE as t and use
    SELECT t.* to carry all columns while joining.
Apply filters cumulatively — each CTE adds one new predicate on top of the prior stage.
Use ONLY the filter criteria listed above.
{optimization_context_section}
OUTPUT: return raw SQL only — no prose, no fences, no semicolon.
The first character must be 'W' (WITH). Any explanatory text causes a pipeline parse failure."""

# Used only when a confirmed correction is being applied to a query that already ran
# successfully (see QuantAgent.apply_confirmed_correction()) -- a narrow edit instruction,
# not a fresh "write the whole query" prompt, so nothing about the query the AI is NOT
# being asked to fix (the base population, the counting unit, any other CTE) is free to
# drift between one run and the next the way it can when the whole query is regenerated
# from a natural-language description every time.
#
# Two distinct kinds of correction need two distinct edits, not one: "also exclude X" is a
# brand-new restriction (add a CTE on top); "renewal window should also include BYOD and
# MTM" redefines what an EXISTING CTE already means (that CTE's own WHERE clause has to
# change) -- inserting an ADDITIONAL narrowing CTE can only ever make a population smaller,
# so it structurally cannot express a correction that's supposed to widen or redefine one.
# Confirmed live: a correction of the second kind, forced through the add-only version of
# this prompt, produced an identical result to before the correction -- silently a no-op.
_APPLY_CORRECTION_PROMPT = """You already generated and successfully ran this exact BigQuery waterfall query:

{previous_sql}

The user has now confirmed the following additional correction(s). Apply ONLY these, and
change nothing else about the query above:
{corrections_json}

For EACH correction, first decide which of these two cases it is:
  ADD    -- a brand-new constraint that isn't already represented by any existing CTE
            (e.g. "also require X"). Insert exactly one new CTE for it, immediately before
            the final CTE ("Final Targetable Audience" or equivalent), applying it as a
            WHERE clause on top of the immediately preceding CTE's output:
            SELECT * FROM <prior_cte> WHERE <new filter>.
  MODIFY -- the correction redefines, widens, loosens, or replaces the logic an EXISTING
            CTE already implements (e.g. it changes what counts as something a CTE already
            filters on, rather than adding an unrelated new requirement). Find that one CTE
            by reading its label and WHERE clause, and rewrite ONLY that CTE's WHERE clause
            to match the corrected definition exactly -- do not insert a new CTE for this
            case, edit the existing one in place.

Rules — non-negotiable:
- Copy the "Base Universe" CTE and every other existing CTE that isn't the direct subject of
  a MODIFY forward EXACTLY as they already appear above: same table, same filters, same
  column names, same counting expression (e.g. COUNT(*) or COUNT(DISTINCT <column>)). Do not
  redesign, reorder, rename, or "improve" anything that isn't explicitly one of the
  corrections listed above.
- At most one existing CTE may be rewritten per MODIFY correction, and nothing else about it
  (its position, its label, unless the old label no longer fits) changes; every ADD
  correction gets exactly one new CTE and touches nothing else.
- Give each new CTE (from an ADD) a short, plain-English label describing the correction (not
  column names).
- Renumber step_order sequentially across the whole query and update the final UNION ALL
  SELECT list accordingly, in the exact same output shape (step_order, layer_name,
  audience_count) and the exact same counting expression already used by every other arm
  above.
- Return ONLY the complete, updated raw SQL — no markdown, no explanation, no trailing
  semicolon. The first character must be 'W' (WITH). Any explanatory text causes a pipeline
  parse failure."""


class QuantAgent(BaseAgent):
    WORKER_ID = "quant_v1"
    HANDLED_INTENTS: frozenset[str] = frozenset({"sizing_request"})
    INPUT_SCHEMA = AdHocSizingRequest
    OUTPUT_SCHEMA = QuantAuditLog

    def __init__(self) -> None:
        self._default_project = os.getenv("BQ_PROJECT_ID", "bi-srv-hsmdet-pr-7b9def")
        self._default_dataset = os.getenv("BQ_DATASET", "campaign_data")
        # One QuantAgent instance is shared by every concurrent request (built once in
        # vibe_orchestrator.build_runtime()) -- both of these were plain instance attributes,
        # meaning two users' requests running on different worker threads at the same time
        # could overwrite each other's session context or last-generated SQL. Thread-local
        # storage isolates each request's worker thread automatically; see the properties
        # below.
        self._local = threading.local()
        self._knowledge_ctx: Optional["KnowledgeContext"] = None

    def set_knowledge_context(self, ctx: "KnowledgeContext") -> None:
        """Bind the centralised KnowledgeContext built at startup."""
        self._knowledge_ctx = ctx

    @property
    def _last_sql(self) -> str:
        return getattr(self._local, "last_sql", "")

    @_last_sql.setter
    def _last_sql(self, value: str) -> None:
        self._local.last_sql = value

    @property
    def _session_context(self) -> str:
        return getattr(self._local, "session_context", "")

    # ------------------------------------------------------------------
    # BaseAgent contract
    # ------------------------------------------------------------------

    def subscribe(self, spec: AdHocSizingRequest) -> None:
        """Not used — QuantAgent is invoked via direct_count(), not subscribe/execute."""

    def execute(self) -> QuantAuditLog:
        """Not used — QuantAgent is invoked via direct_count(), not subscribe/execute."""
        raise NotImplementedError(
            "QuantAgent does not use the subscribe/execute interface. Call direct_count() directly."
        )

    def set_session_context(self, context: str) -> None:
        """Receive dynamic glossary/catalog context from the orchestrator for prompt injection.

        Thread-local: only visible to whichever request's worker thread called this, see
        __init__'s note on why this can't be a plain instance attribute.
        """
        self._local.session_context = context

    # ------------------------------------------------------------------
    # Public API — strict gateway, never raises to orchestrator
    # ------------------------------------------------------------------

    def direct_count(self, request: AdHocSizingRequest) -> Union[QuantAuditLog, NexusErrorPayload]:
        """Path 2 — execute a request-aware waterfall count query for an ad-hoc sizing request."""
        ThoughtDisplay.progress("I'm calculating your audience now...")
        try:
            schema = self._fetch_schema(request.bq_project, request.bq_dataset)
            sql = self._generate_adhoc_waterfall_sql(request, schema)

            is_ffh = _is_ffh_request(request)
            if is_ffh:
                table_label = "TABLE 4 (FFH / Home Solutions customer profile)"
                skipped_steps = [
                    "Step 2: Primary subscriber filter (not applicable to Home Solutions)",
                    "Step 1 LOB filter: lob_desc column absent from FFH table",
                ]
                applied_rules = [
                    "Using EX_STANDARD_EX for standard exclusions",
                    "Using FFH_STOPSELL_IND for stop sell",
                    "Using BACCT_NUM as account key (not ban)",
                ]
            else:
                table_label = "TABLE 1 (Mobility subscriber base)"
                skipped_steps = None
                applied_rules = None

            # Show corrections being applied when optimization_context is set
            opt_ctx = (request.optimization_context or "").strip()
            if opt_ctx and applied_rules is None:
                applied_rules = [l.strip() for l in opt_ctx.splitlines() if l.strip()][:3]

            ThoughtDisplay.execution_plan(
                target_population=request.target_population or "unspecified",
                table_label=table_label,
                filters=[f for f in (request.filters or []) if f][:4],
                skipped_steps=skipped_steps,
                applied_rules=applied_rules,
            )
            ThoughtDisplay.progress("Running the waterfall query now...")
            raw_rows = None
            for attempt in range(1, _MAX_SQL_ATTEMPTS + 1):
                try:
                    raw_rows = self._execute_query(sql, request.bq_project)
                    break
                except Exception as exc:
                    # A malformed generated query (e.g. mismatched UNION ALL column counts,
                    # or an unbalanced paren/quote on a compound OR/AND condition -- seen
                    # live to need more than one attempt) is a SQL-quality problem, not a
                    # transient one -- retrying the same SQL would just fail the same way.
                    # Regenerate with BigQuery's own error fed back as a correction hint and
                    # retry execution, up to _MAX_SQL_ATTEMPTS total; the final attempt's
                    # failure is treated as real and propagates to the except block below.
                    from google.api_core.exceptions import GoogleAPICallError  # noqa: PLC0415

                    if not isinstance(exc, GoogleAPICallError) or attempt == _MAX_SQL_ATTEMPTS:
                        raise
                    _log.warning(
                        "SQL attempt %d/%d failed (%s) -- regenerating with the error as a hint.",
                        attempt, _MAX_SQL_ATTEMPTS, str(exc)[:300],
                    )
                    sql = self._generate_adhoc_waterfall_sql(request, schema, error_hint=str(exc)[:500])
            masked_rows = _mask_pii(raw_rows)
            waterfall = _parse_waterfall(masked_rows)
            note = _optimization_note(waterfall)
            final_count = _final_audience_count(waterfall)
            ThoughtDisplay.results_ready(final_count, waterfall, note)
            return QuantAuditLog(
                request=request,
                sql=sql,
                waterfall=waterfall,
                final_count=final_count,
                optimization_note=note,
            )
        except Exception as exc:
            # Previously silent -- this failure reaches the user only as a deliberately
            # vague "technical issue" message (see vibe_orchestrator.generate_stuck_
            # explanation), which is correct for them but meant there was nowhere at all
            # to find out what actually broke. Now it is at least in the server's own log.
            _log.exception(
                "direct_count failed for %r (target_population=%r): %s",
                request.campaign_name, request.target_population, exc,
            )
            return NexusErrorPayload(
                error_type="database_error",
                error_summary=str(exc)[:400],
                original_request=request.model_dump(),
                failed_sql=self._last_sql or None,
                retry_hint=(
                    "Check that filters use valid BQ column names. "
                    "Verify ADC credentials are active for the BQ project."
                ),
            )

    def _fetch_schema(self, project: str, dataset: str) -> str:
        """Schema + business meaning, from the knowledge layer -- not a live
        BigQuery fetch. This is the single source of what Quant knows about
        any table: real column descriptions (marked confirmed vs. an
        unconfirmed AI guess) and human-confirmed value notes, kept current
        by knowledge/sync_schema.py rather than a separate 24h cache."""
        try:
            from knowledge.retrieve import get_table_schema_text

            text = get_table_schema_text()
            if text == "(no tables synced yet)":
                return f"-- No tables synced into the knowledge layer yet for {project}.{dataset}"
            return text
        except Exception:
            return f"-- Schema unavailable for {project}.{dataset}"

    def _generate_adhoc_waterfall_sql(self, request: AdHocSizingRequest, schema: str, error_hint: str = "") -> str:
        opt_ctx = (request.optimization_context or "").strip()

        # Inject FFH column override when the request targets the Home Solutions table.
        override_parts: list[str] = []
        if _is_ffh_request(request):
            ffh_project = request.bq_project or self._default_project
            override_parts.append(_FFH_WATERFALL_OVERRIDE.format(bq_project=ffh_project))

        # Apply accumulated corrections on every retry.
        if opt_ctx:
            override_parts.append(
                f"\nCOLUMN NAME OVERRIDES — supersede all schema and waterfall definitions above."
                f" Apply these substitutions exactly as stated:\n{opt_ctx}\n"
            )

        # Set only on the one-time regenerate-after-failure retry in direct_count() -- the
        # model's own error, quoted back at it, is a far more specific correction signal
        # than a generic reminder to "keep every UNION ALL arm's columns identical" would be.
        if error_hint:
            override_parts.append(
                "\nYOUR PREVIOUS ATTEMPT AT THIS QUERY FAILED WITH THIS EXACT BIGQUERY ERROR "
                f"— fix the specific problem it describes, most likely a UNION ALL arm with the "
                f"wrong number or aliasing of columns:\n{error_hint}\n"
            )

        optimization_context_section = "\n".join(override_parts) if override_parts else ""

        prompt = _ADHOC_WATERFALL_PROMPT.format(
            target_population=request.target_population or "UNSPECIFIED",
            filters_json=json.dumps(
                [f for f in (request.filters or []) if f is not None],
                ensure_ascii=False,
            ),
            bq_project=request.bq_project or self._default_project,
            bq_dataset=request.bq_dataset or self._default_dataset,
            schema_context=schema[:6000] if schema else "(not available)",
            optimization_context_section=optimization_context_section,
        )
        sql = self._call_sql(prompt)
        self._last_sql = sql
        if os.getenv("QUANT_DEBUG_SQL"):
            print(f"[Quant SQL — ad-hoc]\n{sql}\n", file=sys.stderr)
        return sql

    def apply_confirmed_correction(
        self, previous_log: QuantAuditLog, corrections: list[str],
    ) -> Union[QuantAuditLog, NexusErrorPayload]:
        """Patch a just-confirmed correction directly onto the exact SQL that already ran
        successfully, instead of asking the AI to write the whole query over again from a
        natural-language description.

        Regenerating the entire query from scratch on every retry is how a confirmed
        correction can look "ignored" even though it was applied correctly: nothing stops
        the model from also silently rewriting something unrelated -- which values count
        as "postpaid", whether to count individual subscribers or distinct billing accounts
        -- and the user only ever sees the one final number change, with no way to tell that
        it moved for a reason that had nothing to do with what they actually corrected.
        Editing the known-good SQL in place, one new CTE at a time, removes that risk: the
        base population and every prior filter are copied forward untouched by instruction,
        not left to the model's discretion to reproduce faithfully.
        """
        request = previous_log.request
        updated_request = request.model_copy(
            update={"filters": [*(request.filters or []), *corrections]}
        )
        ThoughtDisplay.progress("Applying your correction to the query that already worked...")
        try:
            prompt = _APPLY_CORRECTION_PROMPT.format(
                previous_sql=previous_log.sql,
                corrections_json=json.dumps(corrections, ensure_ascii=False),
            )
            sql = self._call_sql(prompt)
            self._last_sql = sql
            if os.getenv("QUANT_DEBUG_SQL"):
                print(f"[Quant SQL — correction patch]\n{sql}\n", file=sys.stderr)

            try:
                raw_rows = self._execute_query(sql, request.bq_project)
            except Exception as exc:
                from google.api_core.exceptions import GoogleAPICallError  # noqa: PLC0415

                if not isinstance(exc, GoogleAPICallError):
                    raise
                # The targeted edit didn't produce valid SQL -- fall back to a full rebuild
                # (the pre-existing behavior) rather than surface a failure for something
                # that used to work before this correction was added.
                _log.warning(
                    "Correction-patch SQL failed (%s) -- falling back to a full rebuild.",
                    str(exc)[:300],
                )
                return self.direct_count(updated_request)

            masked_rows = _mask_pii(raw_rows)
            waterfall = _parse_waterfall(masked_rows)
            note = _optimization_note(waterfall)
            final_count = _final_audience_count(waterfall)
            ThoughtDisplay.results_ready(final_count, waterfall, note)
            return QuantAuditLog(
                request=updated_request,
                sql=sql,
                waterfall=waterfall,
                final_count=final_count,
                optimization_note=note,
            )
        except Exception as exc:
            _log.exception(
                "apply_confirmed_correction failed for %r: %s", request.campaign_name, exc,
            )
            return NexusErrorPayload(
                error_type="database_error",
                error_summary=str(exc)[:400],
                original_request=updated_request.model_dump(),
                failed_sql=self._last_sql or None,
                retry_hint="Falling back to rebuilding the full query from scratch.",
            )

    def _call_sql(self, prompt: str) -> str:
        """Call Fuel iX for SQL generation.

        When a KnowledgeContext is bound, pins it as an ephemeral cached block so
        the GOLD campaign patterns and business rules are cheap to reuse across calls.
        Falls back to a plain uncached call if no context is available.

        Extended thinking (budget_tokens=10000) is enabled when FUELIX_EXTENDED_THINKING=1.
        This allows the model to reason through the waterfall step sequence explicitly
        before committing to SQL.  Temperature is forced to 1 when thinking is active.
        """
        system = (
            _QUANT_SYSTEM + "\n\n" + self._session_context
            if self._session_context
            else _QUANT_SYSTEM
        )

        # Note: extended-thinking mode and Fuel iX/Anthropic prompt-caching
        # have no Gemini equivalent wired up here -- this is a plain call.
        if self._knowledge_ctx is not None:
            prompt = (
                "VIBE OCTO PROVEN SQL PATTERNS\n"
                "(Column patterns and business rules from all GOLD campaigns)\n\n"
                + self._knowledge_ctx.quant_context
                + "\n\n"
                + prompt
            )

        text = ask_ai(prompt, system=system, temperature=0, max_tokens=4096)
        return _clean_sql(text)

    def _execute_query(self, sql: str, project: str) -> list[dict]:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from google.cloud import bigquery  # type: ignore

            client = bigquery.Client(project=project)

        return resilient_bq_query(client, sql)


# ------------------------------------------------------------------
# Pure functions — PII masking, waterfall parsing, audit note
# ------------------------------------------------------------------

def _mask_pii(rows: list[dict]) -> list[dict]:
    """Remove hidden PII columns and mask filter-only PII values in-place."""
    try:
        from core.pii_masking import _is_pii, _is_filter_only_pii
    except ImportError:
        return rows

    out: list[dict] = []
    for row in rows:
        masked: dict = {}
        for k, v in row.items():
            if _is_pii(k):
                continue
            masked[k] = "***" if _is_filter_only_pii(k) else v
        out.append(masked)
    return out


def _parse_waterfall(rows: list[dict]) -> list[WaterfallLayer]:
    layers: list[WaterfallLayer] = []
    for row in rows:
        raw_name = str(
            row.get("layer_name")
            or row.get("LAYER_NAME")
            or ""
        ).strip()
        # Strip "CTE N:" / "CTE N -" prefix so labels display as clean corporate funnel steps.
        name = re.sub(r"^CTE\s*\d+\s*[:\-]\s*", "", raw_name, flags=re.IGNORECASE).strip()
        raw_count = row.get("audience_count") or row.get("AUDIENCE_COUNT") or 0
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            count = 0
        if name:
            layers.append(WaterfallLayer(layer_name=name, audience_count=count))
    # Sort: "Base Universe" first, "Final Targetable Audience" (and legacy label) last,
    # all middle steps preserve BQ UNION ALL row order (their original index).
    indexed = list(enumerate(layers))
    indexed.sort(key=lambda t: _waterfall_sort_key(t[0], t[1].layer_name))
    return [l for _, l in indexed]


def _waterfall_sort_key(idx: int, name: str) -> tuple:
    """Return a sort key that anchors "Base Universe" first and the final step last."""
    if name in _WATERFALL_ANCHORS_FIRST:
        return (0, idx)
    if name in _WATERFALL_ANCHORS_LAST or "control group" in name.lower():
        return (9999, idx)
    return (idx + 1, idx)


def _final_audience_count(waterfall: list[WaterfallLayer]) -> int:
    """Return the count from the final CTE (Final Targetable Audience or equivalent).

    Matches the new 'Final Targetable Audience' label and the legacy
    'Universal Control Group' label for backwards compatibility.
    Falls back to the last layer in the sorted waterfall.
    """
    final = next(
        (l for l in waterfall
         if "Final Targetable Audience" in l.layer_name
         or "Universal Control Group" in l.layer_name),
        waterfall[-1] if waterfall else None,
    )
    return final.audience_count if final else 0


def _optimization_note(waterfall: list[WaterfallLayer]) -> Optional[str]:
    if len(waterfall) < 2:
        return None

    base = waterfall[0].audience_count
    final = waterfall[-1].audience_count
    notes: list[str] = []

    for i in range(1, len(waterfall)):
        prev = waterfall[i - 1].audience_count
        curr = waterfall[i].audience_count
        if prev > 0:
            drop = (prev - curr) / prev
            if drop > _EXTREME_DROP:
                notes.append(
                    f"'{waterfall[i].layer_name}' removes {drop:.0%} of upstream audience "
                    f"({prev:,} -> {curr:,}) — verify this filter is correctly calibrated."
                )

    if base > 0:
        total_scrub = (base - final) / base
        if total_scrub > _HIGH_SCRUB_RATE:
            notes.append(
                f"Total scrub rate is {total_scrub:.0%} ({base:,} -> {final:,}). "
                "The combined filter stack is highly restrictive — consider relaxing "
                "criteria or phasing the campaign across multiple sends."
            )

    if not notes:
        return "Waterfall clean — no extreme audience drops detected across filter layers."

    return "Optimization Note: " + " | ".join(notes)


# Matches the first line that is recognisably SQL (WITH/SELECT or a SQL comment).
# Used by _clean_sql to strip any leading prose the model emits despite instructions.
_SQL_LEAD = re.compile(r"^\s*(WITH|SELECT|--)", re.IGNORECASE)


def _clean_sql(sql: str) -> str:
    # 1. Strip markdown fences.
    if "```" in sql:
        lines = sql.splitlines()
        start = next(
            (i + 1 for i, l in enumerate(lines) if l.strip().startswith("```")), 0
        )
        end = next(
            (i for i in range(len(lines) - 1, start - 1, -1) if lines[i].strip() == "```"),
            len(lines),
        )
        sql = "\n".join(lines[start:end])

    # 2. Strip any leading prose lines that precede the first SQL keyword.
    #    Defense-in-depth against "Here is the query:\n\nWITH ..." responses.
    lines = sql.splitlines()
    for i, line in enumerate(lines):
        if _SQL_LEAD.match(line):
            sql = "\n".join(lines[i:])
            break

    sql = sql.strip().rstrip(";").strip()

    # 3. Strip trailing CTE comma — emitted when the model hits the token limit
    #    immediately after the last CTE closing paren.  A bare comma at EOF means
    #    no SELECT follows, causing BQ "Unexpected end of script".
    sql = re.sub(r",\s*$", "", sql).strip()

    return sql


