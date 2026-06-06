# Vibe Marketing with OCTO — Enterprise Agent Mesh Architecture

**Version:** 2.1 (Campaign-Agnostic Refactor)
**Status:** Approved Design Specification  
**Supersedes:** ARCHITECTURE.md v2.0

---

## 1. System Overview

### Problem Statement

Vibe Marketing with OCTO is a Canadian telecom marketing intelligence platform that eliminates the translation gap between business campaign briefs written in natural language and the BigQuery SQL execution required to size and execute those campaigns. Without this system, marketing consultants manually interpret briefs, hand-write SQL filters, and reconcile discrepancies across deployment records — a process that is slow, error-prone, and entirely non-reproducible.

Every validated execution currently lives only in engineers' heads. There is no self-accumulating institutional knowledge layer, no automated brief generation for stakeholders, and no mechanism for the system to improve itself from corrections without requiring a code deployment.

### Design Goals

1. Translate campaign briefs from natural language to validated, executable BigQuery waterfall SQL in under 30 seconds with zero manual data engineering.
2. Accumulate institutional knowledge through a self-healing flywheel — every validated execution automatically becomes a future-run blueprint on the next scheduled refresh.
3. Surface discrepancies between campaign intent and live schema before any query executes.
4. Produce standardized campaign briefs in Markdown for stakeholder review alongside the audience sizing audit.
5. Scale to new campaigns, new agents, and new data sources without code changes.

### Inviolable Design Principles

1. **All BigQuery access is unconditionally read-only.** No `UPDATE`, `INSERT`, `DELETE`, or `ALTER` is ever issued to any upstream table, dataset, or project. Platform state is managed exclusively in local files under the project root.
2. **Atomic Clean-Slate Refresh.** The `semantic_knowledge_index.json` is always rebuilt completely from scratch on each ingestion run. There is no incremental patching. Old state is fully replaced via an atomic `os.replace()` swap.
3. **Pydantic at every boundary.** No raw dicts cross agent boundaries. Every inter-component payload is a named Pydantic model with strict validation.
4. **NexusErrorPayload over raw exceptions.** Quant never raises to the orchestrator. All failures return structured error envelopes.
5. **7-step CTE waterfall is non-negotiable.** The linear `SELECT *` inheritance pattern and canonical seven-step sequence are inviolable contracts. No agent may alter the waterfall shape.
6. **GCH alias contract is immutable.** `a_gch`, `b_gch`, `c_gch` — the three GCH table aliases, their join keys, and the `SELECT DISTINCT b_gch.MOB_BAN` targeting key are frozen. Any deviation is a compilation error.
7. **ADC everywhere.** No service account key files. All GCP authentication uses Application Default Credentials.
8. **Ingestion pipeline is isolated from agent code.** `knowledge_base/ingester.py` never imports from `nexus_agent.py`, `quant_agent.py`, or `vibe_orchestrator.py`. Agents consume from pre-built indexes; they do not participate in ingestion.
9. **HITL is the only gate for Gold Tier promotion.** No record is ever automatically promoted to Gold Tier. A human must answer `Y` at the audit prompt, which writes to the local `verified_app_registry.json`. The next scheduled ingestion run sweeps it into the compiled `semantic_knowledge_index.json` as a permanent Gold blueprint.
10. **Campaign Agnosticism.** The system makes zero assumptions about which campaigns exist in `campaign_knowledge` or their characteristics. At ingestion time, ALL campaigns are discovered dynamically from the source table. The system works identically for a table with 1 campaign or 10,000 campaigns. No campaign names are hardcoded anywhere in the codebase. All examples in documentation use generic placeholders. New campaigns are automatically discovered and processed at next boot.

---

## 1.5 Campaign Agnosticism & Genericity

This system is designed to work with ANY campaign in the `campaign_knowledge` table, with ZERO hardcoding.

Examples of campaigns that work identically:
- A monthly email campaign for any wireless product
- A quarterly SMS upsell campaign for any internet bundle
- A custom retention campaign built by the marketing team today
- A future campaign not yet conceived

The system discovers campaigns at ingestion time by reading the `campaign_knowledge` BigQuery table. No campaign names, campaign codes, or sub-campaign codes are hardcoded in any source file. No assumptions are made about which campaigns exist or their properties.

When examples in this document use `{CAMP_ID}`, `{SUB_CAMP_ID}`, or `{CAMPAIGN_NAME}`, these are placeholders representing ANY campaign. The system processes all campaigns from the table equally, applying the same GOLD/BRONZE classification logic, the same 7-step waterfall generation, and the same HITL flywheel to every record it finds.

**Placeholder reference used throughout this document:**

| Placeholder | Meaning |
|---|---|
| `{CAMP_ID}` | Generic campaign code (e.g., any two-to-four-character campaign identifier) |
| `{SUB_CAMP_ID}` | Generic sub-campaign code (e.g., `{CAMP_ID}` + medium/segment suffix) |
| `{CAMPAIGN_NAME}` | Generic campaign display name |
| `{MEDIUM}` | Generic delivery channel (EM, SMS, PUSH, etc.) |
| `{CADENCE}` | Generic execution frequency (monthly, quarterly, weekly, etc.) |
| `{COLUMN_NAME}` | Generic BigQuery column name |
| `{GOOGLE_SHEET_URL}` | Generic Google Sheets or Docs URL |
| `{AUDIENCE_COUNT_BASE}` | Generic base universe audience size |
| `{AUDIENCE_COUNT_FINAL}` | Generic final filtered audience size |

---

## 2. System Topology

```
 ┌─────────────────────────────────────────────────────────────────────────────────────┐
 │                      VIBE MARKETING WITH OCTO — ENTERPRISE AGENT MESH               │
 └─────────────────────────────────────────────────────────────────────────────────────┘

 ┌──────────────────────────────────────────────────────────────────────────────────────┐
 │  READ-ONLY INPUTS (BigQuery — no writes ever)                                        │
 │                                                                                      │
 │  [1] Campaign Metadata Source                [2] Warehouse Schema Source             │
 │      wb-tian-pr-d0dbe6                           bi-srv-hsmdet-pr-7b9def             │
 │      .wb_tian_pr_dataset                         .adobe                              │
 │      .campaign_knowledge                         .INFORMATION_SCHEMA.COLUMNS         │
 │                                                  .INFORMATION_SCHEMA.TABLES          │
 │      + Google Sheets / Docs / PDFs                                                   │
 │        (via databrief_link column)                                                   │
 └───────────────┬───────────────────────────────────────────┬──────────────────────────┘
                 │ read-only                                 │ read-only
                 ▼                                           ▼
 ┌───────────────────────────────┐             ┌────────────────────────────────────────┐
 │  PILLAR 1                     │             │  PILLAR 2                              │
 │  Binary Knowledge Base        │             │  Dynamic Schema Discovery Layer        │
 │  & Atomic Refresh Pipeline    │             │                                        │
 │                               │             │  SchemaDiscoveryLayer                  │
 │  KnowledgeBaseIngester        │             │    adobe INFORMATION_SCHEMA query      │
 │    BQ rows + BriefFetcher     │             │    1-hour TTL cache (.schema_cache.json│
 │    + verified_app_registry.json             │    SchemaSnapshot injected at runtime  │
 │    → GOLD / BRONZE classify   │             │                                        │
 │    → atomic write             │             │  GOLD path: validates filter columns   │
 │      semantic_knowledge_index │             │  BRONZE path: full schema for zero-shot│
 └───────────────┬───────────────┘             └────────────────────┬───────────────────┘
                 │ semantic_knowledge_index.json (read at startup)   │ SchemaSnapshot
                 ▼                                                   ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────┐
 │  PILLAR 3: GATEWAY ORCHESTRATOR & UNIVERSAL JSON CONTRACT                            │
 │                                                                                      │
 │  vibe_orchestrator.py                                                                 │
 │    _AGENT_REGISTRY: nexus, quant, briefing                                           │
 │    startup: ingestion + schema discovery                                             │
 │    _run_console(): interactive loop                                                  │
 │                                                                                      │
 │  NexusAgent (Vibe OCTO Nexus/nexus_agent.py)                                        │
 │    classify_and_route() → WORKFLOW_A | WORKFLOW_B                                   │
 │    build_universal_spec() → UniversalJSONSpec                                        │
 │    _run_discrepancy_audit() → discrepancy_flags                                      │
 │    GoldTierIndex.lookup() → GOLD | BRONZE tier assignment                           │
 │                                                                                      │
 │  UniversalJSONSpec (pydantic_schemas.py)                                             │
 │    campaign identity + tier + knowledge_source + audience + guardrails              │
 │    discrepancy_flags + runtime_schema_snapshot + brief_agent_inputs                  │
 └─────────────────────────────────┬────────────────────────────────────────────────────┘
                                   │ UniversalJSONSpec (fan-out to both workers)
                    ┌──────────────┴──────────────────────────┐
                    ▼                                         ▼
 ┌──────────────────────────────┐           ┌─────────────────────────────────────────┐
 │  PILLAR 4 — WORKER A         │           │  PILLAR 4 — WORKER B                    │
 │  QuantAgent                  │           │  BriefingAgent                          │
 │  (Vibe OCTO Quant/)          │           │  (Vibe OCTO Briefing/)                  │
 │                              │           │                                         │
 │  WORKER_ID: quant_v1         │           │  WORKER_ID: briefing_v1                 │
 │  7-step CTE waterfall SQL    │           │  GOLD: few-shot via GoldTierIndex        │
 │  BQ read-only execution      │           │  BRONZE: zero-shot + schema context     │
 │  PII masking                 │           │  Markdown brief + telecom recs          │
 │  Optimization notes          │           │  Fuel iX prompt-cached inference         │
 │                              │           │                                         │
 │  OUTPUT: QuantAuditLog       │           │  OUTPUT: BriefingOutput                 │
 └──────────────┬───────────────┘           └────────────────────┬────────────────────┘
                │ QuantAuditLog                                   │ BriefingOutput
                └──────────────────────┬──────────────────────────┘
                                       ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────┐
 │  PILLAR 5: HITL FEEDBACK & ADDITIVE APP REGISTRY FLYWHEEL                           │
 │                                                                                      │
 │  HITLAuditLoop (hitl/audit_loop.py)                                                 │
 │                                                                                      │
 │  [AUDIT] Is this generated output 100% correct? (Y/N)                               │
 │                                                                                      │
 │  YES → append to verified_app_registry.json (local only, never touches BQ)          │
 │        promote_in_memory() in GoldTierIndex for current session                     │
 │        [FLYWHEEL] "upgraded to GOLD tier on next scheduled refresh"                 │
 │                                                                                      │
 │  NO  → collect correction → SemanticFailureLog entry                               │
 │        append to semantic_failure_log.json                                          │
 │        GlossaryManager.patch_from_failure() → zero-code self-learning               │
 │        [FLYWHEEL] "Semantic failure logged. Glossary updated."                      │
 └──────────────────────────────────────────────────────────────────────────────────────┘

 ──────────────────────────────────────────────────────────────────────────────────────
  SHARED INFRASTRUCTURE
 ──────────────────────────────────────────────────────────────────────────────────────
  Fuel iX API: https://api.fuelix.ai/v1/chat/completions  (model: claude-sonnet-4)
  Prompt caching: anthropic-beta: prompt-caching-2024-07-31 + ephemeral cache_control
  BigQuery ADC client (read-only, project: bi-srv-hsmdet-pr-7b9def + wb-tian-pr-d0dbe6)
  GlossaryManager (glossary.json + learnable via patch_from_failure)
  BriefFetcher (ADC auth, Google Docs / Sheets / PDFs / Drive links)
  .schema_cache.json (1-hour TTL, shared cache file, distinct cache keys per consumer)
```

---

## 3. Pillar 1: Binary Knowledge Base & Atomic Refresh Pipeline

### Overview

Transforms the raw `campaign_knowledge` BigQuery table plus platform-local validated records into a single, statically compiled `semantic_knowledge_index.json` that all agents load at startup. This layer is the only component that reads the Campaign Metadata Source. No agent ever queries `campaign_knowledge` directly.

### Atomic Clean-Slate Refresh Pattern

On every ingestion run, the pipeline:
1. Reads all upstream sources (BQ + Google Sheets + local registry)
2. Builds an entirely new index in memory
3. Writes the new index to a temporary file
4. Calls `os.replace(tmp_path, index_path)` — atomic on POSIX and Windows, preventing partially-written reads

The old index is completely discarded. There is no incremental patching, no append, no merge with the previous file state. This guarantees the index is always self-consistent and can be fully regenerated from sources.

### 3.1 File: `knowledge_base/ingester.py`

**Class: `KnowledgeBaseIngester`**

```python
class KnowledgeBaseIngester:
    def __init__(self, config_path: str | Path) -> None:
        """Load ingestion_config.json. Instantiates BQ ADC client and BriefFetcher."""

    def run_full_refresh(self) -> IngestionSummary:
        """Execute the complete atomic clean-slate refresh cycle.

        Steps:
        1. _fetch_bq_rows()       — read campaign_knowledge (read-only)
        2. _fetch_brief_texts()   — BriefFetcher.to_flat_string() per databrief_link
        3. _load_registry()       — read verified_app_registry.json
        4. _classify_and_merge()  — GOLD/BRONZE classification
        5. _write_atomic()        — os.replace() swap of semantic_knowledge_index.json
        Returns IngestionSummary.
        """

    def _fetch_bq_rows(self) -> list[dict]:
        """Query campaign_knowledge. Column names resolved dynamically from
        INFORMATION_SCHEMA.COLUMNS at first call, then cached for the session."""

    def _fetch_brief_texts(self, rows: list[dict]) -> dict[str, str]:
        """Map {databrief_link -> flat text} for all rows with a URL.
        On fetch error: stores "" for that key (never raises)."""

    def _load_registry(self) -> list[dict]:
        """Read verified_app_registry.json. Returns [] if file absent."""

    def _classify_and_merge(
        self, rows: list[dict], brief_texts: dict, registry: list[dict]
    ) -> tuple[list[GoldCampaignRecord], list[CampaignMetadataRecord]]:
        """GOLD if BQ row has non-empty targeting+segment summary fields
        OR a registry entry with hitl_confirmed=true exists for that campaign key.
        BRONZE otherwise."""

    def _write_atomic(
        self, gold: list[GoldCampaignRecord], bronze: list[CampaignMetadataRecord]
    ) -> None:
        """Serialize to JSON, write to .tmp file, then os.replace() to final path."""
```

**Column name resolution:** On first `_fetch_bq_rows()` call, the ingester queries:
```sql
SELECT column_name
FROM `wb-tian-pr-d0dbe6.wb_tian_pr_dataset.INFORMATION_SCHEMA.COLUMNS`
WHERE table_name = 'campaign_knowledge'
ORDER BY ordinal_position
```
The actual column names in the table are mapped to logical roles (campaign identity, URL field, summary fields, active/inactive flag) using heuristic name matching. No column names are hardcoded in the ingester before this discovery query runs. This means adding or renaming columns in `campaign_knowledge` never breaks the ingestion pipeline.

**`IngestionSummary` dataclass:**
```python
@dataclass
class IngestionSummary:
    total_rows: int
    gold_count: int
    bronze_count: int
    fetch_errors: list[str]   # one entry per failed databrief_link fetch
    run_at: str               # ISO 8601 timestamp
```

### 3.2 Dataclasses

**`GoldCampaignRecord`:**
```python
@dataclass
class GoldCampaignRecord:
    camp_id: str
    sub_camp_id: str
    campaign_name: str
    targeting_summary: str     # from BQ field or verified_app_registry
    segment_summary: str       # from BQ field or verified_app_registry
    brief_text: str            # fetched from databrief_link, or "" if unavailable
    cadence: str
    medium: str
    campaign_purpose: str
    primary_products: str
    source: str                # "bq_metadata" or "verified_registry"
    bias_weight: float = 1.0   # deprioritized after HITL NO failures
    ingested_at: str = ""      # ISO timestamp of the refresh run
```

**`CampaignMetadataRecord`:**
```python
@dataclass
class CampaignMetadataRecord:
    camp_id: str
    sub_camp_id: str
    campaign_name: str
    cadence: str
    medium: str
    campaign_purpose: str
    primary_products: str
    databrief_link: str
    tier: str = "BRONZE"
```

### 3.3 File: `knowledge_base/tier_index.py`

**Class: `GoldTierIndex`**

```python
class GoldTierIndex:
    def load_from_file(self, index_path: Path) -> None:
        """Parse semantic_knowledge_index.json and populate internal dict."""

    def lookup(self, camp_id: str, sub_camp_id: str) -> GoldCampaignRecord | None:
        """Return record for "{camp_id}::{sub_camp_id}" key, or None."""

    def promote_in_memory(
        self,
        camp_id: str,
        sub_camp_id: str,
        targeting_summary: str,
        segment_summary: str,
        source: str = "verified_registry",
    ) -> None:
        """Add or update a record in-memory for the current session.
        Does NOT write to any file. The persistent promotion happens
        on the next run_full_refresh() call."""

    def deprioritize(self, camp_id: str) -> None:
        """Reduce bias_weight for all records matching camp_id by 0.2,
        floor at 0.2. Applied in-memory only."""

    def all_keys(self) -> list[str]:
        """Return sorted list of all camp_id::sub_camp_id keys."""
```

Internal storage: `dict[str, GoldCampaignRecord]` keyed by `f"{camp_id}::{sub_camp_id}"`.

### 3.4 Extension: `BriefFetcher.to_flat_string()`

New method added to `Vibe OCTO Nexus/core/brief_fetcher.py`:

```python
def to_flat_string(self, url: str) -> str:
    """Fetch the document at url and return a single clean string.

    Google Sheets (multi-tab): iterates over all sheet tabs discovered
      from the sheet's metadata endpoint, fetches each as CSV via the
      export URL (?format=csv&gid=...), joins them with:
      '\n\n--- TAB: {tab_name} ---\n\n' separators, strips blank header
      rows from each CSV block.
    Google Docs / plain text: returns content after whitespace normalization.
    PDFs: delegates to existing _extract_pdf_text() method.
    On any fetch error: logs the error and returns "" — the ingestion
      pipeline continues with an empty brief text rather than aborting.
    """
```

### 3.5 Platform-Managed Local Files

**`semantic_knowledge_index.json`** — rebuilt on every ingestion run.

The index contains one entry per campaign row discovered in `campaign_knowledge`. The examples below use generic placeholders; actual values are populated from the table at ingestion time. The system works identically whether the table contains 1 campaign or 10,000 campaigns.

```json
{
  "schema_version": "1.0",
  "generated_at": "2026-06-05T14:00:00Z",
  "gold_count": 12,
  "bronze_count": 48,
  "gold_records": [
    {
      "camp_id": "{CAMP_ID}",
      "sub_camp_id": "{SUB_CAMP_ID}",
      "campaign_name": "{CAMPAIGN_NAME}",
      "targeting_summary": "Postpaid primary subscribers meeting {CAMP_ID}-specific eligibility criteria...",
      "segment_summary": "Base universe {AUDIENCE_COUNT_BASE}. After 7 CTE steps: {AUDIENCE_COUNT_FINAL} final audience.",
      "brief_text": "--- TAB: Targeting --- ...",
      "cadence": "{CADENCE}",
      "medium": "{MEDIUM}",
      "campaign_purpose": "Drive {CAMPAIGN_NAME} objectives on eligible accounts",
      "primary_products": "{CAMPAIGN_NAME} primary product",
      "source": "verified_registry",
      "bias_weight": 1.0,
      "ingested_at": "2026-06-05T14:00:00Z"
    }
  ],
  "bronze_records": [
    {
      "camp_id": "{CAMP_ID}",
      "sub_camp_id": "{SUB_CAMP_ID}",
      "campaign_name": "{CAMPAIGN_NAME}",
      "cadence": "{CADENCE}",
      "medium": "{MEDIUM}",
      "campaign_purpose": "Upsell or retain eligible subscribers for {CAMPAIGN_NAME}",
      "primary_products": "{CAMPAIGN_NAME} product bundle",
      "databrief_link": "{GOOGLE_SHEET_URL}",
      "tier": "BRONZE"
    }
  ]
}
```

**`verified_app_registry.json`** — additive, append-only by HITL YES.

Each entry records a single human-validated execution. Fields are populated from the `UniversalJSONSpec` and `QuantAuditLog` of that run:

```json
{
  "schema_version": "1.0",
  "records": [
    {
      "validated_at": "2026-06-05T15:23:00Z",
      "camp_id": "{CAMP_ID}",
      "sub_camp_id": "{SUB_CAMP_ID}",
      "campaign_name": "{CAMPAIGN_NAME}",
      "raw_input_prompt": "Run the {CAMPAIGN_NAME} campaign for {PERIOD}",
      "universal_json_spec": { "campaign_tier": "BRONZE", "filters": ["..."] },
      "targeting_summary": "Postpaid primary subscribers meeting {SUB_CAMP_ID}-specific eligibility criteria...",
      "segment_summary": "7-step waterfall. Base {AUDIENCE_COUNT_BASE}. Final count {AUDIENCE_COUNT_FINAL}.",
      "final_count": "{AUDIENCE_COUNT_FINAL}",
      "hitl_confirmed": true
    }
  ]
}
```

**`ingestion_config.json`**:
```json
{
  "schedule": {
    "mode": "on_startup",
    "interval_hours": 24,
    "note": "on_startup triggers refresh each time vibe_orchestrator.py launches"
  },
  "source": {
    "bq_project": "wb-tian-pr-d0dbe6",
    "bq_table": "wb-tian-pr-d0dbe6.wb_tian_pr_dataset.campaign_knowledge",
    "active_filter_note": "Column names resolved from INFORMATION_SCHEMA at runtime"
  },
  "gold_classification": {
    "bq_summary_fields": ["targeting_summary", "segment_summary"],
    "registry_confirmation_field": "hitl_confirmed",
    "empty_strings_treated_as_null": true
  },
  "output_files": {
    "knowledge_index": "semantic_knowledge_index.json",
    "verified_registry": "verified_app_registry.json"
  },
  "brief_fetch": {
    "timeout_seconds": 60,
    "skip_on_error": true,
    "max_retries": 2
  }
}
```

### 3.6 Isolation Contract

The `knowledge_base/` package imports only:
- `google.cloud.bigquery` (ADC client, read-only)
- `Vibe OCTO Nexus/core/brief_fetcher.py` (for `BriefFetcher`)
- Standard library (`json`, `dataclasses`, `pathlib`, `datetime`, `os`, `tempfile`)

It never imports from `nexus_agent.py`, `quant_agent.py`, or `vibe_orchestrator.py`.

---

## 4. Pillar 2: Dynamic BigQuery Schema Discovery Layer

### Overview

Eliminates all hardcoded column references from agent SQL generation logic. This layer queries BigQuery's `INFORMATION_SCHEMA` at runtime for the `adobe` dataset, caches the result for one hour, and injects the live schema verbatim into agent system prompts. When a new column is added to a production table, agents gain the ability to reference it on the next cache refresh — no code change required.

### 4.1 File: `schema_discovery/discovery_layer.py`

**Class: `SchemaDiscoveryLayer`**

```python
class SchemaDiscoveryLayer:
    def __init__(
        self,
        project: str,
        datasets: list[str],
        cache_path: Path,
        ttl_seconds: int = 3600,
    ) -> None: ...

    def get_snapshot(self, refresh: bool = False) -> SchemaSnapshot:
        """Return a cached or freshly queried SchemaSnapshot.

        Cache key: JSON of {source, project, datasets (sorted)}.
        Distinct from the existing Quant schema cache key — the two
        coexist in the same .schema_cache.json file without collision.
        On TTL expiry or refresh=True: re-queries INFORMATION_SCHEMA.
        """

    def to_prompt_string(self, snapshot: SchemaSnapshot) -> str:
        """Serialize snapshot to the compact inline format consumed by
        QuantAgent._generate_waterfall_sql() and BriefingAgent.execute()."""
```

**`SchemaColumn` dataclass:**
```python
@dataclass
class SchemaColumn:
    table_name: str
    column_name: str
    data_type: str
    is_nullable: bool
    description: str
    is_view: bool
```

**`SchemaSnapshot` dataclass:**
```python
@dataclass
class SchemaSnapshot:
    project: str
    datasets: list[str]
    columns: list[SchemaColumn]
    fetched_at: str     # ISO timestamp
    cache_hit: bool

    def to_dict(self) -> dict: ...

    @classmethod
    def from_dict(cls, d: dict) -> "SchemaSnapshot": ...
```

### 4.2 INFORMATION_SCHEMA Queries

Both queries are read-only against `bi-srv-hsmdet-pr-7b9def.adobe`:

**Columns query:**
```sql
SELECT
    table_name,
    column_name,
    data_type,
    CASE WHEN is_nullable = 'YES' THEN TRUE ELSE FALSE END AS is_nullable,
    COALESCE(description, '') AS description
FROM `bi-srv-hsmdet-pr-7b9def.adobe.INFORMATION_SCHEMA.COLUMNS`
ORDER BY table_name, ordinal_position
```

**Tables query (view detection):**
```sql
SELECT table_name, table_type
FROM `bi-srv-hsmdet-pr-7b9def.adobe.INFORMATION_SCHEMA.TABLES`
WHERE table_type IN ('VIEW', 'MATERIALIZED_VIEW', 'BASE TABLE')
```

The `is_view` flag on each `SchemaColumn` is set by cross-referencing `table_type` from the tables query.

### 4.3 Cache Strategy

The `.schema_cache.json` file already exists in `Vibe OCTO Quant/` and is used by `bq_reporter/bq_client.py`. `SchemaDiscoveryLayer` uses the same file path but a distinct cache key:

```python
# SchemaDiscoveryLayer cache key
json.dumps({
    "source": "schema_discovery_layer",
    "project": project,
    "datasets": sorted(datasets),
}, sort_keys=True)

# Quant bq_reporter cache key (existing)
# Different structure — no collision
```

TTL check: `(datetime.now() - datetime.fromisoformat(cached["fetched_at"])).seconds > ttl_seconds`

### 4.4 Runtime Injection

Called once at orchestrator startup:

```python
# vibe_orchestrator.py startup sequence
schema_discovery = SchemaDiscoveryLayer(
    project="bi-srv-hsmdet-pr-7b9def",
    datasets=["adobe"],
    cache_path=_QUANT_DIR / ".schema_cache.json",
)
snapshot = schema_discovery.get_snapshot()
schema_str = schema_discovery.to_prompt_string(snapshot)

quant.set_runtime_schema(schema_str)        # new method on QuantAgent
briefing.set_runtime_schema(schema_str)     # new method on BriefingAgent
nexus.set_runtime_schema_snapshot(snapshot.to_dict())  # new method on NexusAgent
```

### 4.5 GOLD vs. BRONZE Column Reasoning

**GOLD campaign path:** The `targeting_summary` and `segment_summary` fields from the `GoldCampaignRecord` provide the primary targeting logic. The live `SchemaSnapshot` is used only by `_run_discrepancy_audit()` to verify that filter column names referenced in the summaries still exist in the current schema. This validates against schema drift without requiring a re-derivation of targeting logic.

**BRONZE campaign path:** The full `SchemaSnapshot` is injected into the Quant and Briefing system prompts. The LLM maps targeting intent directly from business language to physical column names using zero-shot reasoning over the live schema. No column names are hardcoded anywhere in the agent codebase.

---

## 5. Pillar 3: Gateway Orchestrator & Universal JSON Contract

### Overview

The Gateway Nexus (NexusAgent + vibe_orchestrator.py) is the sole translator between unstructured human input and the structured `UniversalJSONSpec` that all downstream workers consume. Its output is the immutable interface for campaign execution. No worker ever re-interprets the original user prompt.

### 5.1 Schema: `UniversalJSONSpec` (new in `pydantic_schemas.py`)

```python
class UniversalJSONSpec(BaseModel):
    """Universal inter-agent contract. Superset of AudienceSizingRequest.

    Emitted by NexusAgent.build_universal_spec().
    Consumed by QuantAgent.audit_from_spec() and BriefingAgent.subscribe().
    """
    model_config = ConfigDict(strict=True)

    # Core identity
    campaign_name: str
    campaign_code: str
    campaign_sub_code: str
    cadence: str
    medium: str

    # Tier and knowledge source provenance
    campaign_tier: Literal["GOLD", "BRONZE"]
    knowledge_source: Literal["brief_text", "bq_metadata", "nl_only"]
    gold_blueprint_id: Optional[str] = None   # "{camp_id}::{sub_camp_id}" if GOLD

    # Audience definition
    target_population: str
    filters: list[str]
    exclusion_layers: Optional[list[str]] = None
    optimization_context: Optional[str] = None

    # BQ routing
    bq_project: str = "bi-srv-hsmdet-pr-7b9def"
    bq_dataset: str = "adobe"

    # Runtime audit output
    discrepancy_flags: list[str] = []
    runtime_schema_snapshot: Optional[dict] = None

    # Briefing agent inputs
    brief_agent_inputs: Optional[dict] = None

    # Execution guardrails
    max_waterfall_steps: int = 7
    require_gch_suppression: bool = False
    dnc_channels: list[str] = []

    @field_validator("filters")
    @classmethod
    def filters_not_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("filters must contain at least one entry")
        return v

    def to_audience_sizing_request(self) -> "AudienceSizingRequest":
        """Backwards-compatible downcast for QuantAgent.audit()."""
        return AudienceSizingRequest(
            campaign_name=self.campaign_name,
            campaign_code=self.campaign_code,
            campaign_sub_code=self.campaign_sub_code,
            cadence=self.cadence,
            medium=self.medium,
            target_population=self.target_population,
            filters=self.filters,
            exclusion_layers=self.exclusion_layers,
            optimization_context=self.optimization_context,
            bq_project=self.bq_project,
            bq_dataset=self.bq_dataset,
        )
```

`AudienceSizingRequest` is preserved exactly as-is. The `UniversalJSONSpec` is a superset. All existing code consuming `AudienceSizingRequest` continues to work without modification.

### 5.2 Method: `NexusAgent.build_universal_spec()`

```python
def build_universal_spec(
    self,
    brief: dict,
    gold_index: GoldTierIndex,
    schema_snapshot: dict,
) -> Optional[UniversalJSONSpec]:
    """Build the UniversalJSONSpec from a campaign brief dict.

    1. Extract camp_id + sub_camp_id from the brief deployments.
    2. gold_index.lookup() → set campaign_tier and gold_blueprint_id.
    3. Call existing _build_from_deployment_matrix() or
       build_sizing_request_from_brief() to derive targeting data.
    4. Run _run_discrepancy_audit() → populate discrepancy_flags.
    5. Build brief_agent_inputs dict for BriefingAgent.
    6. Return validated UniversalJSONSpec or None on parse failure.
    """
```

### 5.3 Method: `NexusAgent._run_discrepancy_audit()`

```python
def _run_discrepancy_audit(
    self,
    filters: list[str],
    exclusion_layers: list[str],
    schema_snapshot: dict,
    gold_record: Optional[GoldCampaignRecord],
) -> list[str]:
    """Non-blocking audit. Returns list of advisory flag strings.
    Execution proceeds regardless of flag content.

    Flag types (format: "{type}: {human_readable_description}"):

      unknown_column
        Fired when a filter string references a column name not found
        in the live schema_snapshot. Column name extracted by parsing
        the word immediately before =, IN, NOT IN, LIKE, or IS.
        Example: "unknown_column: '{COLUMN_NAME}' not found in
                  live adobe schema -- verify column name"

      missing_standard_exclusion
        Fired when none of the filters or exclusion_layers contain
        standard_exclusions, sub_status, or primary_sub patterns.

      logic_drift
        Fired for GOLD campaigns when filter intent diverges from
        gold_record.targeting_summary by a lightweight LLM comparison
        (reuses _call_simple() with a compact prompt).
        Only fires on GOLD path.

      gch_bypass_detected
        Fired when campaign_code is in the configured GCH-required
        suppression set and no 'GCH' or 'recency suppression' string
        appears in exclusion_layers. The suppression set is configurable;
        add campaign codes that require mandatory GCH anti-join suppression
        before execution.
    """
```

### 5.4 `vibe_orchestrator.py` Changes

**Updated startup sequence:**
```python
def main() -> None:
    _silence_google_noise()
    load_dotenv(_NEXUS_DIR / ".env")
    load_dotenv(_QUANT_DIR / ".env", override=False)

    # Pillar 1: Knowledge Base ingestion
    ingester = KnowledgeBaseIngester(_ROOT / "ingestion_config.json")
    summary = ingester.run_full_refresh()
    print(f"\n[KNOWLEDGE BASE] Refresh complete: {summary.gold_count} GOLD, "
          f"{summary.bronze_count} BRONZE records indexed.")
    if summary.fetch_errors:
        print(f"[KNOWLEDGE BASE] {len(summary.fetch_errors)} brief fetch errors "
              f"(skipped): {summary.fetch_errors[:3]}")

    gold_index = GoldTierIndex()
    gold_index.load_from_file(_ROOT / "semantic_knowledge_index.json")

    # Pillar 2: Schema Discovery
    schema_discovery = SchemaDiscoveryLayer(
        project="bi-srv-hsmdet-pr-7b9def",
        datasets=["adobe"],
        cache_path=_QUANT_DIR / ".schema_cache.json",
    )
    snapshot = schema_discovery.get_snapshot()
    schema_str = schema_discovery.to_prompt_string(snapshot)
    status = "cache hit" if snapshot.cache_hit else "fresh fetch"
    print(f"[SCHEMA DISCOVERY] Adobe dataset schema loaded ({status}, "
          f"{len(snapshot.columns)} columns).")

    # Instantiate agents
    nexus = NexusAgent()
    quant = QuantAgent()
    briefing = BriefingAgent()
    hitl = HITLAuditLoop(
        gold_index=gold_index,
        glossary_manager=GlossaryManager(_NEXUS_DIR / "core" / "glossary.json"),
        failure_log_path=_ROOT / "semantic_failure_log.json",
        registry_path=_ROOT / "verified_app_registry.json",
    )

    # Inject shared runtime context
    quant.set_runtime_schema(schema_str)
    briefing.set_runtime_schema(schema_str)
    nexus.set_runtime_schema_snapshot(snapshot.to_dict())

    _run_console(nexus, quant, briefing, hitl, gold_index, snapshot.to_dict())
```

**Updated `_AGENT_REGISTRY`:**
```python
_AGENT_REGISTRY: dict[str, type] = {
    "nexus": NexusAgent,
    "quant": QuantAgent,
    "briefing": BriefingAgent,   # new
}
```

**Updated `route()` for WORKFLOW_B:**
```python
def route(
    nexus: NexusAgent,
    quant: QuantAgent,
    briefing: BriefingAgent,
    hitl: HITLAuditLoop,
    workflow: str,
    payload: dict,
    gold_index: GoldTierIndex,
    schema_snapshot: dict,
) -> None:
    if workflow == "WORKFLOW_B":
        spec = nexus.build_universal_spec(payload, gold_index, schema_snapshot)
        if spec is None:
            return
        if spec.discrepancy_flags:
            print("\n[DISCREPANCY AUDIT] Flags detected before execution:")
            for flag in spec.discrepancy_flags:
                print(f"  ! {flag}")
        audit_log = quant.audit_from_spec(spec)
        if isinstance(audit_log, NexusErrorPayload):
            # existing retry logic via nexus.route_with_retry()
            ...
            return
        brief_output = briefing.execute(spec)
        _format_audit_log(audit_log)
        _print_brief(brief_output)
        hitl.prompt(spec, audit_log, brief_output)

    elif workflow == "WORKFLOW_A":
        # existing ad-hoc path — unchanged
        ...
```

---

## 6. Pillar 4: Two-Worker Agent Registry

### Overview

A decoupled, event-driven registry framework. Adding a new micro-agent requires only: (1) implementing `BaseAgent`, (2) adding one line to `_AGENT_REGISTRY`. The core Gateway Nexus logic is never rewritten.

### 6.1 File: `core/base_agent.py`

```python
from __future__ import annotations
from abc import ABC, abstractmethod
from pydantic import BaseModel
from pydantic_schemas import UniversalJSONSpec


class BaseAgent(ABC):
    """Abstract contract for all registered workers in the Agent Mesh.

    Every worker must declare class-level:
      WORKER_ID:     str               — unique versioned ID, e.g. "quant_v1"
      INPUT_SCHEMA:  type[BaseModel]   — Pydantic model accepted by subscribe()
      OUTPUT_SCHEMA: type[BaseModel]   — Pydantic model returned by execute()

    Orchestrator lifecycle: subscribe() → set_session_context() → execute()

    Workers must never raise raw exceptions to the orchestrator.
    Wrap all failures in NexusErrorPayload or a worker-specific error model.
    """

    WORKER_ID: str
    INPUT_SCHEMA: type[BaseModel]
    OUTPUT_SCHEMA: type[BaseModel]

    @abstractmethod
    def subscribe(self, spec: UniversalJSONSpec) -> None:
        """Accept the UniversalJSONSpec for this execution cycle."""
        ...

    @abstractmethod
    def execute(self) -> BaseModel:
        """Execute the worker pipeline and return validated output."""
        ...

    def set_session_context(self, context: str) -> None:
        """Receive glossary/catalog context injected by the orchestrator."""

    def set_runtime_schema(self, schema_str: str) -> None:
        """Receive live schema context injected by the orchestrator."""
```

### 6.2 Worker A: QuantAgent (Formalized, Existing Logic Unchanged)

**Registration:**
```python
WORKER_ID = "quant_v1"
INPUT_SCHEMA = UniversalJSONSpec
OUTPUT_SCHEMA = QuantAuditLog
```

**New method added to `quant_agent.py`:**
```python
def audit_from_spec(self, spec: UniversalJSONSpec) -> Union[QuantAuditLog, NexusErrorPayload]:
    """Accept a UniversalJSONSpec and delegate to the existing audit() pipeline.

    Downcasts spec to AudienceSizingRequest via to_audience_sizing_request().
    The 7-step CTE waterfall, PII masking, optimization notes, and error
    boundary logic are entirely unchanged.
    """
    return self.audit(spec.to_audience_sizing_request().model_dump())
```

The existing `audit()`, `direct_count()`, `_run_audit()`, and all waterfall methods are untouched.

**Waterfall contract (reference, unchanged):**

| Step | CTE Name | Predicate Added |
|---|---|---|
| 1 | `base_universe` | `UPPER(lob_desc) IN (...)` |
| 2 | `after_primary_subscriber` | `primary_sub = 1` |
| 3 | `after_standard_exclusions` | `standard_exclusions = 0 AND sub_status = 'A'` |
| 4 | `after_stop_sell` | `stop_sell = 0` |
| 5 | `after_targeting_criteria` | All custom filters from `spec.filters` |
| 6 | `after_channel_governance` | DNC flags + GCH LEFT JOIN anti-join (if required) |
| 7 | `after_universal_control_group` | `control_group_flg = 'N'` — absolute last step |

Final output: `UNION ALL` of 7 `COUNT(DISTINCT ban)` arms, each with explicit `layer_name` and `audience_count` aliases.

**GCH suppression alias contract (immutable):**
```sql
-- Aliases are frozen. Any deviation is a compilation error.
FROM `bi-srv-hsmdet-pr-7b9def.gch_current.bq_campaign_segment`     a_gch
INNER JOIN `bi-srv-hsmdet-pr-7b9def.gch_current.bq_campaign_communication` b_gch
    ON a_gch.SEGMENT_ID = b_gch.SEGMENT_ID
INNER JOIN `bi-srv-hsmdet-pr-7b9def.gch_current.bq_campaign_description`  c_gch
    ON a_gch.CAMPAIGN_ID = c_gch.CAMPAIGN_ID
-- Targeting key law: MOB_BAN lives exclusively in b_gch
SELECT DISTINCT b_gch.MOB_BAN
```

### 6.3 Worker B: BriefingAgent (New)

**File:** `Vibe OCTO Briefing/briefing_agent.py`

**Registration:**
```python
WORKER_ID = "briefing_v1"
INPUT_SCHEMA = UniversalJSONSpec
OUTPUT_SCHEMA = BriefingOutput
```

**Class:**
```python
class BriefingAgent(BaseAgent):
    WORKER_ID = "briefing_v1"
    INPUT_SCHEMA = UniversalJSONSpec
    OUTPUT_SCHEMA = BriefingOutput

    def __init__(self) -> None:
        self._api_key = os.getenv("FUELIX_API_KEY")
        self._model = os.getenv("FUELIX_MODEL", "claude-sonnet-4")
        self._spec: Optional[UniversalJSONSpec] = None
        self._runtime_schema: str = ""
        self._gold_index: Optional[GoldTierIndex] = None

    def set_gold_index(self, gold_index: GoldTierIndex) -> None:
        self._gold_index = gold_index

    def subscribe(self, spec: UniversalJSONSpec) -> None:
        self._spec = spec

    def execute(self) -> BriefingOutput:
        """Generate the campaign brief via Fuel iX.

        GOLD path:
          1. Retrieve GoldCampaignRecord from self._gold_index.
          2. Inject targeting_summary + segment_summary + brief_text
             as a cached few-shot block in the system prompt.
             Uses anthropic-beta: prompt-caching-2024-07-31 header
             with cache_control: {"type": "ephemeral"} on the block.
          3. Confidence score: 0.90 if brief_text non-empty, 0.70 if empty.

        BRONZE path:
          1. Inject full SchemaSnapshot (self._runtime_schema) as context.
          2. Zero-shot reasoning from live schema.
          3. Confidence score: 0.60 if schema covers >= 80% of filter columns,
             0.40 otherwise.

        Both paths return BriefingOutput or raise never — wraps failures
        in a minimal BriefingOutput with empty brief_markdown.
        """
```

**System prompt:**
```python
_BRIEFING_SYSTEM = (
    "You are Vibe OCTO Briefing, a senior telecom marketing strategist "
    "for TELUS and Koodo. You generate structured campaign intelligence "
    "briefs combining data brief content, targeting logic, and predictive "
    "strategic recommendations. Output must be valid Markdown matching the "
    "prescribed section structure exactly. Strategic recommendations must be "
    "grounded in Canadian telecom industry dynamics: rate plan migration "
    "trends, ARPU optimization, device lifecycle economics, and omni-channel "
    "contact sequencing. Never reference SQL, BigQuery, or technical "
    "execution details in the brief output."
)
```

**Mandatory Markdown output structure:**
```markdown
# Campaign Brief: {campaign_name}
**Tier**: {GOLD|BRONZE} | **Generated**: {ISO timestamp} | **Confidence**: {score}

## Campaign Overview
{executive summary — 2-3 sentences}

## Data Brief Reference
{source document summary or "No data brief available" for Bronze without brief_text}

## Targeting Logic
{structured description of audience selection criteria in business language}

## Audience Segmentation
{breakdown of key segments: province splits, product eligibility tiers, lifecycle cohorts}

## Strategic Recommendations
1. {recommendation — rate plan / ARPU / device lifecycle / XS sell / contact sequencing}
2. ...
(up to 5 recommendations)

## Execution Checklist
- [ ] GCH recency suppression applied (confirm lookback window)
- [ ] DNC channel flags verified for {medium}
- [ ] Control group flag excluded (CTE step 7)
- [ ] Quebec province codes include both QC and PQ
- [ ] Product eligibility pair logic confirmed (_ind = 0 AND _elig = 1)
```

**`BriefingOutput` schema (new in `pydantic_schemas.py`):**
```python
class BriefingOutput(BaseModel):
    campaign_name: str
    tier: str
    brief_markdown: str
    executive_summary: str
    targeting_logic_summary: str
    strategic_recommendations: list[str]
    data_sources_cited: list[str]
    confidence_score: float        # 0.0 to 1.0
    generated_at: str              # ISO 8601
```

**`Vibe OCTO Briefing/` directory structure:**
```
Vibe OCTO Briefing/
├── __init__.py
├── briefing_agent.py
├── requirements.txt      # requests, pydantic, python-dotenv
└── .env                  # FUELIX_API_KEY, FUELIX_MODEL, BQ_PROJECT
```

---

## 7. Pillar 5: HITL Feedback & Additive App Registry Flywheel

### Overview

Every successful agent execution is a learning opportunity. The human is the sole arbiter of correctness. A YES confirmation writes a validated record to the local `verified_app_registry.json`. A NO response captures the failure in structured form and immediately improves the system's knowledge without touching any code or production data.

### 7.1 File: `hitl/audit_loop.py`

**Class: `HITLAuditLoop`**

```python
class HITLAuditLoop:
    def __init__(
        self,
        gold_index: GoldTierIndex,
        glossary_manager: GlossaryManager,
        failure_log_path: Path,
        registry_path: Path,
    ) -> None: ...

    def prompt(
        self,
        spec: UniversalJSONSpec,
        audit_log: QuantAuditLog,
        briefing_output: Optional[BriefingOutput] = None,
    ) -> None:
        """Print the HITL prompt and dispatch to YES or NO handler."""
        response = input("\n[AUDIT] Is this generated output 100% correct? (Y/N): ")
        if response.strip().upper() == "Y":
            self._handle_yes(spec, audit_log, briefing_output)
        else:
            self._handle_no(spec, audit_log)
```

### 7.2 YES Path

```python
def _handle_yes(
    self,
    spec: UniversalJSONSpec,
    audit_log: QuantAuditLog,
    briefing_output: Optional[BriefingOutput],
) -> None:
    targeting_summary = self._build_targeting_summary(spec, audit_log)
    segment_summary = self._build_segment_summary(audit_log)

    registry_entry = {
        "validated_at": datetime.now(tz=timezone.utc).isoformat(),
        "camp_id": spec.campaign_code,
        "sub_camp_id": spec.campaign_sub_code,
        "campaign_name": spec.campaign_name,
        "raw_input_prompt": spec.brief_agent_inputs.get("raw_prompt", "") if spec.brief_agent_inputs else "",
        "universal_json_spec": spec.model_dump(),
        "targeting_summary": targeting_summary,
        "segment_summary": segment_summary,
        "final_count": audit_log.final_count,
        "hitl_confirmed": True,
    }

    # Additive local write — no BQ interaction
    self._append_to_registry(registry_entry)

    # Immediate in-memory promotion for current session
    self._gold_index.promote_in_memory(
        camp_id=spec.campaign_code,
        sub_camp_id=spec.campaign_sub_code,
        targeting_summary=targeting_summary,
        segment_summary=segment_summary,
    )

    print(
        f"\n[FLYWHEEL] Campaign '{spec.campaign_name}' validated and saved "
        "to local App Registry."
    )
    print(
        "[FLYWHEEL] It will be compiled as a permanent GOLD tier blueprint "
        "on the next scheduled refresh."
    )
```

`_append_to_registry()` reads the current `verified_app_registry.json`, appends the new entry to `records`, and writes the file back. Uses a file lock (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows) to prevent concurrent write corruption.

### 7.3 NO Path

```python
def _handle_no(self, spec: UniversalJSONSpec, audit_log: QuantAuditLog) -> None:
    correction = input(
        "\n[AUDIT] What specifically was incorrect? Describe in plain language: "
    ).strip()

    failure_type = self._infer_failure_type(correction, spec)
    glossary_gaps = self._find_glossary_gaps(spec.filters, spec.exclusion_layers or [])

    log_entry = SemanticFailureLog(
        timestamp=datetime.now(tz=timezone.utc).isoformat(),
        campaign_code=spec.campaign_code,
        worker_id="quant_v1",
        raw_input=json.dumps(spec.model_dump()),
        generated_output=audit_log.sql,
        correction_description=correction,
        inferred_failure_type=failure_type,
        glossary_gaps=glossary_gaps,
    )

    self._append_failure_log(log_entry)
    self._glossary_manager.patch_from_failure(log_entry)

    if spec.gold_blueprint_id:
        self._gold_index.deprioritize(spec.campaign_code)

    print("\n[FLYWHEEL] Semantic failure logged. Glossary updated. "
          "Zero-code self-learning applied.")
```

**Failure type inference rules:**

| Correction text contains | Inferred type |
|---|---|
| Column name absent from `SchemaSnapshot` | `schema_gap` |
| "wrong column" or "column" | `wrong_column` |
| "wrong filter" or "filter" | `wrong_filter` |
| "missing" + "exclusion" | `missing_exclusion` |
| "tier" or "blueprint" | `tier_mismatch` |
| Default | `wrong_filter` |

### 7.4 Schema: `SemanticFailureLog` (new in `pydantic_schemas.py`)

```python
class SemanticFailureLog(BaseModel):
    timestamp: str
    campaign_code: str
    worker_id: str
    raw_input: str
    generated_output: str
    correction_description: str
    inferred_failure_type: Literal[
        "wrong_column",
        "wrong_filter",
        "missing_exclusion",
        "tier_mismatch",
        "schema_gap",
    ]
    glossary_gaps: list[str]
```

`semantic_failure_log.json` is a flat JSON array. Initial state: `[]`.

### 7.5 `GlossaryManager.patch_from_failure()`

New method added to `Vibe OCTO Nexus/core/glossary.py`:

```python
def patch_from_failure(self, log_entry: "SemanticFailureLog") -> int:
    """Infer new or corrected terms from a semantic failure and merge.

    For each entry in log_entry.glossary_gaps:
      - If not already in glossary: add stub with confidence_score=0.3,
        source="failure_inference".

    If inferred_failure_type is 'wrong_column':
      - Find the closest existing glossary term matching the incorrect
        column reference and reduce its confidence_score by 0.15
        (floor at 0.05).

    If inferred_failure_type is 'missing_exclusion':
      - Extract the missing term from correction_description and add
        a stub exclusion entry.

    Calls self.save() with portfolio="inferred_failure" at end.
    Returns count of newly added terms.
    """
```

### 7.6 Flywheel Lifecycle

The flywheel applies identically to every campaign the system encounters. The pattern below uses generic placeholders — substitute any `{CAMP_ID}` and `{SUB_CAMP_ID}` from the `campaign_knowledge` table:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  RUN 1 — First-ever execution of {CAMPAIGN_NAME}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Ingestion: {CAMPAIGN_NAME} not in BQ with summaries,
    not in registry → classified BRONZE
  Execution: UniversalJSONSpec (campaign_tier=BRONZE)
    → Quant zero-shot SQL from live schema
    → BriefingAgent zero-shot brief (confidence=0.60)
  HITL: User answers Y
  → verified_app_registry.json gains one record
    (targeting_summary, segment_summary, hitl_confirmed=true)
  → GoldTierIndex.promote_in_memory() for current session

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  NEXT ORCHESTRATOR STARTUP (scheduled or manual)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Ingestion: reads BQ rows + verified_app_registry.json
    → finds hitl_confirmed=true for {CAMP_ID}::{SUB_CAMP_ID}
    → classifies as GOLD, source="verified_registry"
    → atomic write of new semantic_knowledge_index.json
      ({CAMPAIGN_NAME} now in gold_records)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  RUN 2 — {CAMPAIGN_NAME} execution after refresh
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  GoldTierIndex.lookup("{CAMP_ID}", "{SUB_CAMP_ID}") → GoldCampaignRecord
  UniversalJSONSpec (campaign_tier=GOLD,
    gold_blueprint_id="{CAMP_ID}::{SUB_CAMP_ID}")
  → Nexus: discrepancy audit validates filters against
    gold_record.targeting_summary (logic_drift check)
  → Quant: few-shot SQL anchored to proven targeting logic
  → BriefingAgent: few-shot brief (confidence=0.90)
  → Richer output, faster convergence, higher accuracy
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

This three-run lifecycle repeats for every campaign in the table. There is no code change required when a new campaign is added to `campaign_knowledge`.

---

## 8. Data Flow: End-to-End Request Lifecycle

### Startup Sequence

```
python vibe_orchestrator.py
  │
  ├─ 1. KnowledgeBaseIngester.run_full_refresh()
  │     a. BQ read: wb-tian-pr-d0dbe6.wb_tian_pr_dataset.campaign_knowledge
  │     b. Google Sheets/Docs/PDFs via BriefFetcher.to_flat_string()
  │     c. Read: verified_app_registry.json
  │     d. Classify GOLD/BRONZE, merge, atomic write
  │        semantic_knowledge_index.json
  │     e. Print [KNOWLEDGE BASE] summary
  │
  ├─ 2. GoldTierIndex.load_from_file(semantic_knowledge_index.json)
  │
  ├─ 3. SchemaDiscoveryLayer.get_snapshot()
  │     a. Check .schema_cache.json TTL
  │     b. On miss: BQ read adobe INFORMATION_SCHEMA
  │     c. Print [SCHEMA DISCOVERY] status
  │
  ├─ 4. Instantiate NexusAgent, QuantAgent, BriefingAgent, HITLAuditLoop
  │
  ├─ 5. Inject schema context into all three agents
  │
  └─ 6. _run_console() loop
```

### WORKFLOW_B: Named Campaign

The trace below uses `{CAMPAIGN_NAME}`, `{CAMP_ID}`, and `{SUB_CAMP_ID}` as placeholders. Any campaign discovered in `campaign_knowledge` follows this identical path:

```
User: "Run the {CAMPAIGN_NAME} campaign" (or any campaign in knowledge base)
  │
  ├─ nexus.classify_and_route() → ("WORKFLOW_B", "{CAMPAIGN_NAME}")
  ├─ nexus._find_brief_for_campaign("{CAMPAIGN_NAME}")
  │   → BQ read campaign_knowledge (read-only)
  │   → returns {deployments: [...rows...]}
  │
  ├─ nexus.build_universal_spec(brief, gold_index, schema_snapshot)
  │   a. gold_index.lookup("{CAMP_ID}", "{SUB_CAMP_ID}")
  │      → GOLD: campaign_tier="GOLD", gold_blueprint_id="{CAMP_ID}::{SUB_CAMP_ID}"
  │      → BRONZE: campaign_tier="BRONZE"
  │   b. _build_from_deployment_matrix(deployments)
  │      → AudienceSizingRequest (existing logic)
  │   c. _run_discrepancy_audit(filters, exclusions, schema_snapshot, gold_record)
  │      → list of advisory flag strings
  │   d. Assemble brief_agent_inputs
  │   e. Return UniversalJSONSpec (Pydantic validated)
  │
  ├─ Print [DISCREPANCY AUDIT] flags (if any)
  │
  ├─ quant.audit_from_spec(spec)
  │   → spec.to_audience_sizing_request()
  │   → existing audit() pipeline:
  │     schema fetch → SQL gen → BQ exec → PII mask → waterfall parse
  │   → QuantAuditLog
  │
  ├─ [on NexusErrorPayload: nexus.route_with_retry() — one correction attempt]
  │
  ├─ _format_audit_log(audit_log) → print waterfall table
  │
  ├─ briefing.subscribe(spec) + briefing.execute()
  │   GOLD: few-shot from GoldCampaignRecord (prompt-cached)
  │   BRONZE: zero-shot from schema context
  │   → BriefingOutput
  │
  ├─ _print_brief(briefing_output) → print Markdown
  │
  └─ hitl.prompt(spec, audit_log, briefing_output)
      Y → append to verified_app_registry.json
          → promote_in_memory()
          → [FLYWHEEL] "upgraded on next refresh"
      N → collect correction
          → SemanticFailureLog → semantic_failure_log.json
          → GlossaryManager.patch_from_failure()
          → [FLYWHEEL] "failure logged"
```

### WORKFLOW_A: Ad-Hoc NL Query (Unchanged)

```
User: "How many TELUS postpaid customers have SHS eligible?"
  │
  ├─ nexus.classify_and_route() → ("WORKFLOW_A", None)
  ├─ nexus.build_sizing_request_from_nl(query) → AdHocSizingRequest
  ├─ quant.direct_count(request) → QuantAuditLog
  ├─ _format_audit_log(audit_log)
  └─ hitl.prompt(spec=None, audit_log, briefing_output=None)
      (simplified: YES appends to registry with camp_id=None;
       NO captures failure without GoldTierIndex deprioritization)
```

---

## 9. Interface Contracts

All schemas reside in `pydantic_schemas.py`. Existing schemas are preserved without modification.

### Existing Schemas (Unchanged)

| Schema | Emitted By | Consumed By |
|---|---|---|
| `AudienceSizingRequest` | NexusAgent | QuantAgent.audit() |
| `AdHocSizingRequest` | NexusAgent | QuantAgent.direct_count() |
| `WaterfallLayer` | QuantAgent | QuantAuditLog |
| `QuantAuditLog` | QuantAgent | Orchestrator, HITLAuditLoop |
| `NexusErrorPayload` | QuantAgent | NexusAgent.route_with_retry() |

### New Schemas

| Schema | Emitted By | Consumed By |
|---|---|---|
| `UniversalJSONSpec` | NexusAgent.build_universal_spec() | QuantAgent.audit_from_spec(), BriefingAgent.subscribe(), HITLAuditLoop |
| `BriefingOutput` | BriefingAgent.execute() | Orchestrator, HITLAuditLoop |
| `SemanticFailureLog` | HITLAuditLoop._handle_no() | GlossaryManager.patch_from_failure(), semantic_failure_log.json |

---

## 10. File Manifest

### New Files to Create

| File | Purpose |
|---|---|
| `ARCHITECTURE.md` | This document |
| `knowledge_base/__init__.py` | Package init |
| `knowledge_base/ingester.py` | `KnowledgeBaseIngester`, `GoldCampaignRecord`, `CampaignMetadataRecord`, `IngestionSummary` |
| `knowledge_base/tier_index.py` | `GoldTierIndex` |
| `schema_discovery/__init__.py` | Package init |
| `schema_discovery/discovery_layer.py` | `SchemaDiscoveryLayer`, `SchemaSnapshot`, `SchemaColumn` |
| `hitl/__init__.py` | Package init |
| `hitl/audit_loop.py` | `HITLAuditLoop` |
| `core/__init__.py` | Package init (project root `core/`, distinct from `Vibe OCTO Nexus/core/`) |
| `core/base_agent.py` | `BaseAgent` ABC |
| `Vibe OCTO Briefing/__init__.py` | Package init |
| `Vibe OCTO Briefing/briefing_agent.py` | `BriefingAgent` |
| `Vibe OCTO Briefing/requirements.txt` | requests, pydantic, python-dotenv |
| `Vibe OCTO Briefing/.env` | FUELIX_API_KEY, FUELIX_MODEL, BQ_PROJECT |
| `ingestion_config.json` | Ingestion schedule and classification config |
| `semantic_knowledge_index.json` | Compiled knowledge index (initial: `{"schema_version":"1.0","gold_count":0,"bronze_count":0,"gold_records":[],"bronze_records":[]}`) |
| `verified_app_registry.json` | HITL-validated records (initial: `{"schema_version":"1.0","records":[]}`) |
| `semantic_failure_log.json` | Append-only failure log (initial: `[]`) |

### Files to Modify

| File | Changes |
|---|---|
| `pydantic_schemas.py` | Add `UniversalJSONSpec`, `BriefingOutput`, `SemanticFailureLog` |
| `vibe_orchestrator.py` | Import `BriefingAgent`; update `_AGENT_REGISTRY`; update `route()` and `main()` with startup sequence, schema discovery, and HITL |
| `Vibe OCTO Nexus/nexus_agent.py` | Add `build_universal_spec()`, `_run_discrepancy_audit()`, `set_runtime_schema_snapshot()`; migrate `_TAXONOMY_BQ_QUERY` and `_find_brief_for_campaign()` to `wb-tian-pr-d0dbe6.wb_tian_pr_dataset.campaign_knowledge` with dynamically resolved column names |
| `Vibe OCTO Nexus/core/brief_fetcher.py` | Add `to_flat_string()` |
| `Vibe OCTO Nexus/core/glossary.py` | Add `patch_from_failure()` |
| `Vibe OCTO Quant/quant_agent.py` | Add `audit_from_spec()`, `set_runtime_schema()`, `subscribe()`, `execute()`; implement `BaseAgent` |

### Files Unchanged

`Vibe OCTO Quant/bq_reporter/bq_client.py`, `Vibe OCTO Nexus/core/bq_client.py`, all existing waterfall prompts, GCH suppression constants, PII handling sets, `glossary.json` structure, `query_catalog.json`, `.env` files, `feedback_schema.py`.

---

## 11. BigQuery Access Summary

### Read-Only Queries Issued

| Source | Query Type | Consumer |
|---|---|---|
| `wb-tian-pr-d0dbe6.wb_tian_pr_dataset.campaign_knowledge` | SELECT (all rows) | `KnowledgeBaseIngester` |
| `wb-tian-pr-d0dbe6.wb_tian_pr_dataset.INFORMATION_SCHEMA.COLUMNS` | SELECT (column names) | `KnowledgeBaseIngester` (column discovery) |
| `bi-srv-hsmdet-pr-7b9def.adobe.INFORMATION_SCHEMA.COLUMNS` | SELECT | `SchemaDiscoveryLayer` |
| `bi-srv-hsmdet-pr-7b9def.adobe.INFORMATION_SCHEMA.TABLES` | SELECT | `SchemaDiscoveryLayer` |
| `bi-srv-hsmdet-pr-7b9def.adobe.*` (production tables) | SELECT (waterfall SQL) | `QuantAgent._execute_query()` |
| `bi-srv-hsmdet-pr-7b9def.gch_current.*` | SELECT (GCH anti-join) | `QuantAgent` (within generated SQL) |
| `wb-tian-pr-d0dbe6.wb_tian_pr_dataset.campaign_knowledge` | SELECT (per-campaign lookup) | `NexusAgent._find_brief_for_campaign()` |

**No write queries are ever issued.** The BQ audit log for the service account will contain exclusively `SELECT` operations.

### Required IAM Permissions

| Project | Minimum Role |
|---|---|
| `wb-tian-pr-d0dbe6` | `roles/bigquery.dataViewer` on dataset `wb_tian_pr_dataset` |
| `bi-srv-hsmdet-pr-7b9def` | `roles/bigquery.dataViewer` on datasets `adobe` and `gch_current` |
| Both projects | `roles/bigquery.jobUser` (to run query jobs) |

No `roles/bigquery.dataEditor` or `roles/bigquery.admin` is required or should be granted.

---

## 12. Configuration Reference

### Environment Variables

| Variable | Used By | Default | Notes |
|---|---|---|---|
| `FUELIX_API_KEY` | Nexus, Quant, Briefing | required | Fuel iX API key |
| `FUELIX_MODEL` | Nexus, Quant, Briefing | `claude-sonnet-4` | Override model name |
| `BQ_PROJECT` | Nexus, Ingester | `bi-srv-hsmdet-pr-7b9def` | Execution project |
| `BQ_PROJECT_ID` | Quant | `bi-srv-hsmdet-pr-7b9def` | Query execution project |
| `BQ_DATASET` | Quant | `adobe` | Default dataset |
| `QUANT_DEBUG_SQL` | Quant | unset | Print SQL to stderr if set |

### Config Files

| File | Owner | Purpose |
|---|---|---|
| `ingestion_config.json` | Ingester | Schedule, BQ table ref, gold classification criteria |
| `glossary.json` | GlossaryManager | Campaign acronyms and codes, campaign-specific targeting configs, global scope rules |
| `query_catalog.json` | Orchestrator | SQL blueprint templates for keyword-triggered context injection |
| `semantic_knowledge_index.json` | KnowledgeBaseIngester | Compiled GOLD/BRONZE index (rebuilt atomically on each refresh) |
| `verified_app_registry.json` | HITLAuditLoop | Additive log of HITL YES confirmations |
| `semantic_failure_log.json` | HITLAuditLoop | Additive log of HITL NO corrections |
| `.schema_cache.json` | SchemaDiscoveryLayer + bq_reporter | Adobe schema TTL cache (1-hour, shared file, distinct keys) |

---

## 13. Verification & Testing

### 13.1 Pillar 1: Knowledge Base

**Isolation:** `import knowledge_base.ingester` in a clean Python shell. Assert that `"nexus_agent"`, `"quant_agent"`, `"vibe_orchestrator"` are absent from `sys.modules`.

**Classification boundary:** Mock a BQ row with non-empty targeting and segment summary fields. Assert `_classify_row(row) == "GOLD"`. Remove one field. Assert `"BRONZE"`.

**Registry merge:** Add a `hitl_confirmed=true` entry to `verified_app_registry.json` for a campaign that has no summaries in BQ. Run `run_full_refresh()`. Assert the campaign appears in `gold_records` of the new index.

**Atomic write:** Delete `.schema_cache.json` and `semantic_knowledge_index.json`. Interrupt `run_full_refresh()` mid-write (mock `os.replace()` to raise). Assert the old index file is still present and uncorrupted.

**`to_flat_string()`:** Pass a Google Sheet URL with 3 tabs. Assert output contains two `--- TAB:` separators. Assert passing a 403 URL returns `""` without raising.

### 13.2 Pillar 2: Schema Discovery

**Cache TTL:** Call `get_snapshot()` twice in sequence. Assert `snapshot.cache_hit` is `False` on first call, `True` on second. Manually set `fetched_at` 2 hours ago in `.schema_cache.json`. Assert next call returns `cache_hit=False`.

**Column presence:** Call `to_prompt_string(snapshot)`. Assert strings `ban`, `lob_desc`, `primary_sub`, `control_group_flg` appear in the output.

**Cache key coexistence:** After both `SchemaDiscoveryLayer.get_snapshot()` and a Quant `_fetch_schema()` call, assert `.schema_cache.json` contains entries for both cache keys without either overwriting the other.

### 13.3 Pillar 3: Gateway Orchestrator

**Spec construction:** Feed a mock deployment dict into `nexus.build_universal_spec()`. Assert the return type passes `UniversalJSONSpec` Pydantic validation. Assert `campaign_tier` matches GoldTierIndex state.

**Discrepancy audit — unknown column:** Pass `filters=["{COLUMN_NAME} = 1"]` with a schema snapshot that does not contain `{COLUMN_NAME}`. Assert `discrepancy_flags` contains an `unknown_column` entry.

**Discrepancy audit — GCH bypass:** Set `campaign_code` to a value present in the configured GCH-required suppression set, set `exclusion_layers=[]`. Assert `gch_bypass_detected` flag is present.

**Backwards compat:** Call `spec.to_audience_sizing_request()`. Assert the result passes `AudienceSizingRequest` Pydantic validation with no errors.

### 13.4 Pillar 4: Agent Registry

**Registry completeness:** Assert `_AGENT_REGISTRY.keys() == {"nexus", "quant", "briefing"}`. Assert each value is the correct class type.

**BaseAgent conformance:** Assert `isinstance(QuantAgent(), BaseAgent)` and `isinstance(BriefingAgent(), BaseAgent)`. Assert both have non-empty `WORKER_ID`, `INPUT_SCHEMA`, `OUTPUT_SCHEMA`.

**`audit_from_spec()` delegation:** Create a minimal `UniversalJSONSpec`. Mock `QuantAgent.audit()`. Call `quant.audit_from_spec(spec)`. Assert `audit()` was called with a valid `AudienceSizingRequest` dict (not a `UniversalJSONSpec` dict).

**BriefingOutput structure:** After a successful BriefingAgent execution, assert `confidence_score` is between 0.0 and 1.0, `generated_at` parses as ISO datetime, and `brief_markdown` starts with `# Campaign Brief:`.

**GOLD vs BRONZE prompts:** Assert that `BriefingAgent.execute()` on a GOLD spec sends a system prompt containing `targeting_summary` text from the gold record. Assert that BRONZE spec sends a prompt containing column names from `SchemaSnapshot`.

### 13.5 Pillar 5: HITL Flywheel

**YES path — local only:** Mock `input()` to return `"Y"`. Call `hitl.prompt(spec, audit_log, None)`. Assert `verified_app_registry.json` gains one entry with `hitl_confirmed=true`. Assert no BQ write operations occur (verified by ensuring no `UPDATE`, `INSERT`, `DELETE` in any BQ client calls during the test).

**YES path — in-memory promotion:** After YES, call `gold_index.lookup(spec.campaign_code, spec.campaign_sub_code)`. Assert it returns a non-None `GoldCampaignRecord` for the current session.

**NO path — failure log:** Mock `input()` to return `["N", "The province filter was missing BC"]` in sequence. Call `hitl.prompt(spec, audit_log, None)`. Assert `semantic_failure_log.json` gains one entry. Assert `inferred_failure_type == "wrong_filter"`.

**NO path — glossary patch:** Provide a `SemanticFailureLog` with `glossary_gaps=["province_bc"]`. Assert `glossary.json` gains a stub entry for `province_bc` with `confidence_score=0.3` and `source="failure_inference"`.

**Append-only invariant:** Run the NO path three times. Assert `semantic_failure_log.json` contains exactly 3 entries (no overwrites or truncations).

**BQ read-only guarantee (regression):** Grep all files in `knowledge_base/`, `hitl/`, `schema_discovery/`, and modified agent files for SQL keywords `UPDATE`, `INSERT`, `DELETE`, `ALTER`. Assert zero matches.

### 13.6 End-to-End Integration

1. Start the orchestrator with an empty `semantic_knowledge_index.json`. Assert `[KNOWLEDGE BASE]` and `[SCHEMA DISCOVERY]` headers appear in startup output.
2. Send `"Run the {CAMPAIGN_NAME} campaign"` (any campaign present in the `campaign_knowledge` table). Assert both the waterfall audit table and the Markdown brief appear in output. Assert `campaign_tier` in the spec is `"BRONZE"` (no prior validation exists).
3. Answer `N`. Enter `"Wrong province filter"`. Assert `semantic_failure_log.json` is written.
4. Answer `Y` on a re-run. Assert `[FLYWHEEL] Campaign ... validated and saved` is printed. Assert `verified_app_registry.json` contains one record with `hitl_confirmed: true`.
5. Restart the orchestrator. Assert the `{CAMPAIGN_NAME}` record appears in `gold_records` of `semantic_knowledge_index.json` after the startup ingestion. Assert `campaign_tier` in the next spec is `"GOLD"`.
6. Regression: Send `"How many TELUS postpaid customers have SHS eligible?"`. Assert the waterfall output is structurally identical to a pre-architecture run (same seven step names, same column structure).
