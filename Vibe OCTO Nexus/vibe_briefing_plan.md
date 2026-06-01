# Vibe Briefing — Project Plan
**Version:** 1.1  
**Date:** 2026-05-28  
**Author:** Omni-Channel Targeting & Orchestration Team  
**Status:** Draft — Pending Claude Code Feasibility Assessment  
**Change from v1.0:** Training/test split updated — 200 oldest briefs for learning, 42 most recent held out for testing

---

## 1. Project Overview

**Vibe Briefing** is a CLI tool that serves as the **master brain** for all campaign execution tools at our telecom marketing team. It reads campaign data briefs written in business language, studies historical brief patterns, and translates them into standardized JSON payloads that downstream tools can execute directly against BigQuery — without any further interpretation.

### Core Purpose
- **Eliminate** the business-to-data translation gap that causes campaign execution delays and errors
- **Standardize** how campaign logic is expressed across all tools
- **Scale** the team's capability without adding headcount
- **Power** all current and future downstream tools with one consistent JSON contract

### What Vibe Briefing Is NOT
- It is not a UI or web app (MVP is CLI only)
- It is not a real-time service (runs on demand)
- It is not a replacement for human review (it assists, not decides)
- It is not a campaign execution tool itself (it feeds tools that execute)

---

## 2. Problem Statement

Our omni-channel targeting and orchestration team executes marketing campaigns at a telecom company. Campaign requirements are written by business stakeholders in **data briefs** — documents that describe campaign logic in business language (e.g., "target loyal high-value customers who haven't upgraded recently").

### Current Pain Points
1. **Language gap** — Stakeholders write briefs in business language; data team executes in SQL/BQ logic
2. **Ambiguity** — Terms like "loyal," "high-value," "recently active" are interpreted differently
3. **Rework** — Back-and-forth between stakeholders and data team to clarify intent
4. **Inconsistency** — Same business term mapped differently across campaigns
5. **No standard contract** — Each downstream tool interprets briefs independently
6. **Institutional knowledge loss** — Business-to-data mappings live in people's heads, not systems

### Desired State
- Stakeholders write a brief in plain language
- Vibe Briefing reads it, consults historical patterns, and translates it to a standard JSON
- All downstream tools consume the same JSON to execute their specific tasks
- No interpretation needed by downstream tools — the JSON is unambiguous and executable

---

## 3. Architecture Overview

```
┌─────────────────────────────────────────────┐
│           DATA SOURCE (BigQuery)             │
│  monday_campaign_data table                  │
│  - campaign_id, brief_url, portfolio,        │
│    purpose, cadence, medium, success_flag    │
└────────────────────┬────────────────────────┘
                     │ Query historical briefs
                     ▼
┌─────────────────────────────────────────────┐
│         VIBE BRIEFING (CLI Tool)             │
│                                             │
│  Step 1: LEARN                              │
│  ├─ Fetch 200 historical briefs from BQ     │
│  ├─ Download brief documents from URLs      │
│  ├─ Analyze with Claude Sonnet 4.6          │
│  └─ Build portfolio-specific glossary       │
│                                             │
│  Step 2: TRANSLATE                          │
│  ├─ Accept new brief (text input)           │
│  ├─ Consult learned glossary                │
│  ├─ Translate business language → JSON      │
│  ├─ Validate JSON against schema            │
│  └─ Output standard JSON contract           │
└────────────────────┬────────────────────────┘
                     │ Standard JSON output
                     ▼
┌─────────────────────────────────────────────┐
│         DOWNSTREAM TOOLS (Current + Future) │
│                                             │
│  ✅ Campaign Sizing Tool (existing)          │
│  🔜 QA Checklist Runner                     │
│  🔜 Contact Frequency Governor              │
│  🔜 Channel Sequencing Optimizer            │
│  🔜 Execution Brief Generator               │
│  🔜 Data Brief Assistant Widget             │
│  🔜 [Any future tools]                      │
│                                             │
│  All tools:                                 │
│  - Read the standard JSON                   │
│  - Query BigQuery using JSON rules          │
│  - Return results                           │
└─────────────────────────────────────────────┘
```

---

## 4. Data Sources

### 4.1 Primary Data Source: BigQuery Monday.com Table
- **Table:** `monday_campaign_data` (exact name TBC with team)
- **Key columns:**
  - `campaign_id` — Unique campaign identifier
  - `brief_url` — Link to the data brief document (Google Doc/Sheet/PDF)
  - `portfolio` — Campaign portfolio (e.g., "Postpaid Winback")
  - `purpose` — Campaign purpose (e.g., "winback", "upsell", "retention", "acquisition")
  - `cadence` — Campaign cadence (e.g., "monthly", "weekly", "daily")
  - `medium` — Channel(s) used (e.g., "sms", "email", "outbound_call")
  - `success_flag` — Whether campaign achieved its goals (TBC)
  - Additional campaign metadata (TBC)

### 4.2 Customer Data Source: BigQuery Customer Tables
- All downstream tools will query customer data from BQ
- Vibe Briefing JSON must reference exact BQ field names
- BQ schema (field names, data types) to be injected into Claude's context
- A data dictionary (business term → BQ field mapping) will be maintained

### 4.3 Historical Briefs
- **MVP Portfolio:** [TO BE CONFIRMED — one specific portfolio]
- **Total available:** 242 data briefs in selected portfolio
- **Training set:** 200 oldest briefs (used for glossary learning)
- **Held-out test set:** 42 most recent briefs (reserved for testing — NOT used in learning)
- **Format:** URLs linking to documents (Google Docs, Sheets, or PDFs — TBC)
- **Access:** Via `brief_url` column in BQ table
- **Sort order:** By campaign date ascending — oldest 200 for learning, newest 42 held out

---

## 5. MVP Scope

### 5.1 MVP Goal
Validate that Vibe Briefing can:
1. Learn from 200 historical briefs (oldest) in one portfolio
2. Build a context-aware glossary from those 200 briefs
3. Translate a new brief from the held-out set (42 most recent) into valid JSON
4. Produce JSON that the existing sizing tool can consume and execute against BQ

### 5.2 MVP Input
- **Source:** BQ table (`monday_campaign_data`)
- **Filter:** Single portfolio (242 total briefs)
- **Training split:** 200 oldest briefs (sorted by campaign date ascending) → glossary learning
- **Test split:** 42 most recent briefs → held out, not used in learning
- **Format:** Plain text briefs accessed via `brief_url`
- **Test brief:** 1 brief selected from the 42 held-out most recent briefs

### 5.3 MVP Output
A standard JSON file containing:
- Campaign metadata
- Data sources (BQ tables, fields)
- Audience filtering logic (conditions, operators, values, SQL snippets)
- Join logic between BQ tables
- Exclusion rules
- Offer details and eligibility
- Channel sequence
- QC flags (ambiguities, contradictions, confidence scores)
- Downstream tool instructions (sizing, QA, frequency, sequencing)

### 5.4 MVP Exclusions (Phase 2+)
- Monday.com API direct integration (BQ table used instead)
- Google Sheets or Apps Script integration
- Real-time stakeholder brief guidance
- Web UI or API wrapper
- Auto-build glossary from new campaigns (done manually in MVP)
- Multi-portfolio support
- Cross-portfolio glossary

---

## 6. CLI Tool Design

### 6.1 Tech Stack
```
Language:         Python 3.9+
Claude API:       Anthropic SDK (claude-sonnet-4-6)
Claude Code:      Model: Sonnet 4.6, Effort: high
BQ Integration:   google-cloud-bigquery
CLI Framework:    click
JSON Validation:  pydantic
Config/Secrets:   python-dotenv
HTTP Requests:    requests (for downloading brief documents)
Testing:          pytest
```

### 6.2 Project Structure
```
vibe-briefing/
├── main.py                    # CLI entry point
├── commands/
│   ├── analyze.py             # Translate brief → JSON
│   ├── learn.py               # Build glossary from historical briefs
│   ├── validate.py            # Validate JSON against schema
│   └── sql.py                 # Generate SQL from JSON (for testing)
├── core/
│   ├── claude_client.py       # Anthropic API integration
│   ├── bq_client.py           # BigQuery integration
│   ├── brief_fetcher.py       # Download brief documents from URLs
│   ├── glossary.py            # Glossary management
│   ├── json_formatter.py      # Format output JSON
│   └── validators.py          # JSON schema validation
├── config/
│   ├── bq_schema.json         # BigQuery customer table schema
│   ├── glossary_seed.json     # Manually seeded starter glossary
│   ├── json_schema.json       # Output JSON schema definition
│   └── prompts/
│       ├── system_prompt.txt  # Claude system prompt
│       ├── learn_prompt.txt   # Prompt for glossary building
│       └── translate_prompt.txt # Prompt for brief translation
├── output/                    # Generated JSON files
├── glossary/                  # Learned glossary files
│   ├── generic.json           # Seed glossary
│   └── [portfolio_name].json  # Learned portfolio glossary
├── tests/
│   ├── sample_briefs/         # Anonymized test briefs
│   └── expected_outputs/      # Expected JSON for validation
├── .env                       # API keys (gitignored)
├── requirements.txt
└── README.md
```

### 6.3 CLI Commands

```bash
# CORE COMMANDS

# Step 1: Learn from historical briefs (run once per portfolio)
# Fetches the 200 OLDEST briefs — most recent 42 are held out for testing
vibe-briefing learn \
  --portfolio "postpaid-winback" \
  --bq-table "project.dataset.monday_campaign_data" \
  --limit 200 \
  --sort "campaign_date ASC" \
  --output glossary/postpaid_winback.json

# Step 2: Translate a new brief to JSON
vibe-briefing analyze \
  --brief briefs/q3_winback_brief.txt \
  --glossary glossary/postpaid_winback.json \
  --bq-schema config/bq_schema.json \
  --output output/q3_winback.json

# UTILITY COMMANDS

# Validate a JSON output against schema
vibe-briefing validate \
  --json output/q3_winback.json

# Generate SQL from JSON (for testing/debugging)
vibe-briefing sql \
  --json output/q3_winback.json \
  --output output/q3_winback.sql

# Run a sample BQ query to test JSON rules (Phase 2)
vibe-briefing test \
  --json output/q3_winback.json \
  --sample-size 1000
```

---

## 7. JSON Output Schema

The output JSON is the **standard contract** for all downstream tools. Every field must be executable against BigQuery without further interpretation.

```json
{
  "brief_metadata": {
    "brief_id": "string — unique ID",
    "brief_name": "string — campaign name",
    "campaign_objective": "string — (winback|upsell|retention|acquisition|reactivation)",
    "portfolio": "string — portfolio name from BQ table",
    "purpose": "string — from BQ table",
    "cadence": "string — from BQ table",
    "medium": ["string — channel list from BQ table"],
    "created_date": "ISO 8601",
    "translation_timestamp": "ISO 8601",
    "vibe_briefing_version": "string — schema version (e.g. 1.0)",
    "glossary_version": "string — which glossary was used"
  },

  "data_sources": {
    "primary_source": {
      "database": "bigquery",
      "project": "string — BQ project ID",
      "dataset": "string — BQ dataset",
      "tables": [
        {
          "table_name": "string",
          "alias": "string",
          "description": "string"
        }
      ]
    }
  },

  "audience": {
    "primary_segment": {
      "segment_name": "string",
      "business_description": "string — original stakeholder language",
      "data_rules": {
        "filter_logic": "AND | OR",
        "conditions": [
          {
            "condition_id": "string",
            "business_term": "string — original term from brief",
            "data_source": {
              "table": "string",
              "field": "string — exact BQ field name"
            },
            "operator": "string — (==|!=|>|<|>=|<=|in|not_in|between|contains|like)",
            "value": "string | number | array",
            "confidence_score": "number (0.0–1.0)",
            "glossary_matched": "boolean",
            "sql_snippet": "string — executable BQ SQL fragment"
          }
        ]
      },
      "joins": [
        {
          "join_id": "string",
          "from_table": "string",
          "from_field": "string",
          "to_table": "string",
          "to_field": "string",
          "join_type": "INNER | LEFT | RIGHT | FULL",
          "sql_snippet": "string — executable BQ JOIN clause"
        }
      ]
    },
    "exclusions": [
      {
        "exclusion_id": "string",
        "exclusion_name": "string",
        "business_description": "string",
        "data_source": {
          "table": "string",
          "field": "string"
        },
        "operator": "string",
        "value": "string | number | array",
        "lookback_window_days": "number (optional)",
        "confidence_score": "number (0.0–1.0)",
        "sql_snippet": "string — executable BQ SQL fragment"
      }
    ],
    "frequency_cap": {
      "max_contacts_per_customer": "number",
      "lookback_window_days": "number",
      "sql_snippet": "string"
    }
  },

  "offer": {
    "offer_id": "string",
    "offer_name": "string",
    "business_description": "string",
    "offer_type": "string — (discount|bundle|loyalty|limited_time|other)",
    "eligibility_rules": [
      {
        "rule_id": "string",
        "description": "string",
        "sql_snippet": "string"
      }
    ],
    "known_conflicts": [
      {
        "conflict_type": "string",
        "description": "string",
        "severity": "high | medium | low"
      }
    ]
  },

  "channels": {
    "preferred_channels": ["string"],
    "channel_sequence": [
      {
        "sequence_order": "number",
        "channel": "string — (sms|email|outbound_call|push|in_app)",
        "timing": {
          "send_on_day": "number",
          "send_time": "string",
          "send_time_zone": "string"
        },
        "rationale": "string"
      }
    ],
    "channel_constraints": [
      {
        "channel": "string",
        "constraint_description": "string",
        "sql_snippet": "string"
      }
    ]
  },

  "qc_flags": {
    "ambiguities": [
      {
        "flag_id": "string",
        "field": "string",
        "issue": "string",
        "severity": "high | medium | low",
        "suggested_clarification": "string"
      }
    ],
    "contradictions": [
      {
        "type": "string",
        "description": "string",
        "fields_involved": ["string"],
        "recommended_resolution": "string"
      }
    ],
    "low_confidence_rules": [
      {
        "condition_id": "string",
        "confidence_score": "number",
        "reason": "string"
      }
    ],
    "glossary_misses": [
      {
        "business_term": "string",
        "closest_match": "string",
        "action_required": "string"
      }
    ]
  },

  "execution_readiness": {
    "completeness_score": "number (0–100)",
    "overall_confidence": "number (0.0–1.0)",
    "qa_passed": "boolean",
    "ready_to_execute": "boolean",
    "blockers": ["string"],
    "warnings": ["string"]
  },

  "downstream_tool_inputs": {
    "sizing_tool": {
      "status": "ready | needs_review",
      "primary_segment_ref": "audience.primary_segment",
      "exclusions_ref": "audience.exclusions",
      "instructions": "string"
    },
    "qa_checklist": {
      "status": "ready | needs_review",
      "flags_ref": "qc_flags",
      "instructions": "string"
    },
    "frequency_governor": {
      "status": "ready | needs_review",
      "frequency_cap_ref": "audience.frequency_cap",
      "instructions": "string"
    },
    "channel_sequencer": {
      "status": "ready | needs_review",
      "channel_sequence_ref": "channels.channel_sequence",
      "instructions": "string"
    },
    "data_brief_assistant": {
      "status": "reserved — future tool",
      "instructions": "Vibe Briefing will power this widget in a future phase"
    }
  }
}
```

---

## 8. Claude Integration

### 8.1 Model & Settings
```
Model:        claude-sonnet-4-6
Effort Level: high
Max Tokens:   2000 (translation output)
Temperature:  0 (deterministic, no creativity needed)
```

### 8.2 Prompt Architecture

**System Prompt (injected once per session):**
```
You are Vibe Briefing — a campaign brief translation engine for a 
telecom marketing team. Your job is to translate business campaign 
briefs written by non-technical stakeholders into standardized JSON 
payloads that can be executed directly against BigQuery.

Context you will receive:
1. BigQuery Schema: The exact field names, data types, and table 
   structure of our customer data
2. Portfolio Glossary: A learned glossary of business terms and their 
   confirmed BigQuery mappings, built from 242 historical briefs
3. Campaign Metadata: Portfolio, purpose, cadence, medium from 
   our Monday.com campaign tracking table

Rules:
- Always use exact BigQuery field names from the provided schema
- Always include SQL snippets for every data rule
- Assign confidence scores (0.0–1.0) based on glossary match quality
- Flag any ambiguities or terms not in the glossary
- When unsure, flag it — do not guess silently
- Output valid JSON only, matching the provided schema exactly
- Never add fields not in the schema
```

**Learn Prompt (for glossary building):**
```
Analyze the following campaign brief from our [PORTFOLIO] portfolio.
Campaign metadata: purpose=[PURPOSE], medium=[MEDIUM], cadence=[CADENCE]

Extract every business term used to describe the target audience, 
offer, channels, and exclusions. For each term:
1. Identify the likely BigQuery field it maps to (from the schema)
2. Identify the operator and value
3. Generate the SQL snippet
4. Assign a confidence score

Brief:
[BRIEF_CONTENT]

Respond in JSON only using the glossary entry format.
```

**Translate Prompt (for new brief translation):**
```
Translate the following campaign brief into the standard JSON contract.

Campaign metadata from BQ: [METADATA]
Portfolio glossary: [GLOSSARY]
BigQuery schema: [SCHEMA]

Brief to translate:
[BRIEF_CONTENT]

Output valid JSON matching the provided schema exactly. 
Flag any terms not in the glossary under qc_flags.glossary_misses.
```

### 8.3 Token Estimation (Per Brief)
```
System prompt:          ~800 tokens
BQ schema:            ~1,500 tokens
Portfolio glossary:   ~2,000 tokens
Brief content:          ~600 tokens
Total input:          ~4,900 tokens

JSON output:          ~1,000 tokens

Estimated cost (Sonnet 4.6):  ~$0.015–0.020 per brief
```

---

## 9. Glossary Design

### 9.1 Glossary Structure
```json
{
  "glossary_metadata": {
    "portfolio": "string",
    "version": "string",
    "built_from_briefs": 200,
    "held_out_briefs": 42,
    "total_portfolio_briefs": 242,
    "last_updated": "ISO 8601",
    "total_terms": "number"
  },
  "terms": {
    "high-value customer": {
      "bq_field": "arpu",
      "operator": ">=",
      "value": 100,
      "sql_snippet": "arpu >= 100",
      "frequency_in_briefs": 47,
      "success_rate": 0.92,
      "confidence_score": 0.95,
      "context": {
        "works_for_purposes": ["upsell", "retention"],
        "medium": ["email", "outbound_call"]
      },
      "variations": ["high value", "high_value", "premium customer"],
      "source": "learned"
    }
  }
}
```

### 9.2 Glossary Evolution
```
Week 1 — MVP:
  Generic seed glossary (manually defined by team, ~50 terms)

Week 1-2 — Learning Phase:
  Run: vibe-briefing learn --portfolio "postpaid-winback" --limit 200 --sort "campaign_date ASC"
  Output: Learned glossary (~150-250 terms, with frequency and 
  success rates)
  Note: Most recent 42 briefs held out for testing — not included

Week 3+ — Ongoing:
  Each new completed campaign adds to glossary
  Terms updated with new frequency and success rate data
```

---

## 10. MVP Test Plan

### 10.1 Learning Phase Validation
```
Input:  200 oldest historical briefs from selected portfolio
        (42 most recent held out — NOT included in learning)
Output: Learned glossary file

Check:
✅ Glossary built from exactly 200 briefs (verify count)
✅ Glossary has 100+ unique terms
✅ High-frequency terms (10+ occurrences) have confidence ≥ 0.90
✅ All terms have BQ field mappings
✅ All terms have SQL snippets
✅ Success rates populated where available
✅ Held-out set (42 briefs) untouched and available for testing
```

### 10.2 Translation Phase Validation (Test Brief)
```
Input:  1 brief selected from the 42 held-out most recent briefs
        (same portfolio, lightly different criteria than training set)
Output: Standard JSON file

Level 1 — Structure Check:
✅ JSON validates against schema (pydantic)
✅ All required fields present
✅ No unexpected fields

Level 2 — Data Rule Check:
✅ All BQ fields exist in schema
✅ SQL snippets are syntactically valid
✅ Operators match field data types
✅ Confidence scores are appropriate

Level 3 — Business Logic Check:
✅ Business terms correctly mapped (manual review by team)
✅ Context-aware mapping used (e.g., winback vs upsell definitions)
✅ Exclusions are complete
✅ Ambiguities flagged correctly

Level 4 — Downstream Readiness:
✅ Sizing tool can execute SQL from JSON
✅ Sample BQ query returns reasonable row count (5K–500K)
✅ No BQ query errors
```

### 10.3 MVP Success Criteria
```
Metric                          Target
─────────────────────────────────────────
JSON schema validation          100% pass
Data rule correctness           ≥ 90%
Glossary term coverage          ≥ 85% of brief terms in glossary
Confidence scores (core rules)  ≥ 0.85
Ambiguity detection rate        Flags all high-severity ambiguities
Time to translate (1 brief)     < 20 seconds
Cost per brief                  < $0.05
Downstream tool readiness       ≥ 95%
```

---

## 11. Phased Rollout

### Phase 1 — MVP (Weeks 1-2)
- ✅ CLI tool (Python, click)
- ✅ Single portfolio (242 historical briefs)
- ✅ Plain text brief input
- ✅ Claude Sonnet 4.6, effort: high
- ✅ Learned glossary (built from 242 briefs)
- ✅ Standard JSON output
- ✅ JSON schema validation
- ✅ SQL snippet generation
- ✅ Test on 1 new brief
- ✅ Validate with sizing tool

### Phase 2 — Expansion (Weeks 3-6)
- 🔜 Multi-portfolio support
- 🔜 Cross-portfolio glossary
- 🔜 Brief fetching directly from BQ table (batch processing)
- 🔜 BQ query validation (run sample queries)
- 🔜 Additional downstream tools (QA checklist, frequency governor)
- 🔜 Auto-update glossary from new completed campaigns
- 🔜 Cost monitoring dashboard

### Phase 3 — Scale (Weeks 7-12)
- 🔜 All portfolios onboarded
- 🔜 Data Brief Assistant Widget integration (Vibe Briefing as backend)
- 🔜 Monday.com webhook trigger (auto-translate when brief submitted)
- 🔜 Stakeholder guidance (real-time hints as brief is written)
- 🔜 Performance analytics (which targeting criteria work best)
- 🔜 API wrapper (expose Vibe Briefing as internal service)

---

## 12. Feasibility Assessment — Questions for Claude Code

Please assess the feasibility of this plan by investigating the following:

### 12.1 Technical Setup
```
1. Is Python + click + anthropic SDK + google-cloud-bigquery 
   the right tech stack for this CLI tool?

2. What Python version is recommended, and are there any 
   dependency conflicts in the proposed stack?

3. Is pydantic the best library for JSON schema validation, 
   or is there a better alternative?

4. What's the simplest way to set up the project locally for a 
   team that is strong in SQL but not experienced in Python CLI tools?
```

### 12.2 BigQuery Integration
```
5. How do we authenticate to BigQuery from a Python CLI tool?
   (Service account key? OAuth? Application Default Credentials?)

6. Can we query the monday_campaign_data table and retrieve 
   brief_url values in a single query?

7. How do we download brief documents from URLs (Google Docs, 
   Sheets, or PDFs) in Python? What authentication is needed?

8. What are the BQ query costs for fetching 200 brief URLs?
   (Estimate based on ~1KB per row, 200 rows)

9. Is there a BQ API rate limit we need to worry about for 
   batch processing 242 briefs?
```

### 12.3 Claude API Integration
```
10. Can Claude Sonnet 4.6 reliably produce valid, schema-compliant 
    JSON from a campaign brief? What's the failure rate?

11. With ~4,900 input tokens + ~1,000 output tokens per brief, 
    what's the exact cost per translation at Sonnet 4.6 pricing?

12. Is effort level "high" appropriate for this task, or should 
    we start with "xhigh" given the complexity of brief translation?

13. What's the best way to handle JSON parsing errors if Claude 
    produces malformed output? (Retry? Fallback? Error to user?)

14. Should we use Anthropic's structured output feature, or prompt 
    engineering for JSON? Which is more reliable?

15. What's the estimated latency per brief at Sonnet 4.6 + high effort?
    (Target: < 20 seconds)
```

### 12.4 Glossary Building from 200 Briefs
```
16. How long will it take to analyze all 200 briefs with Claude?
    (Estimated: 200 × 15s = ~50 minutes — is this reasonable?)

17. Should we process briefs sequentially or in parallel? 
    Are there Anthropic API rate limits to consider?

18. What's the total cost to build the glossary from 200 briefs?
    (Estimate at Sonnet 4.6 pricing)

19. Can Claude reliably extract business term → BQ field mappings 
    from historical briefs written in varied business language?

20. How do we handle briefs with missing or unclear content 
    (blank fields, placeholder text, etc.)?

21. How do we sort and select the 200 oldest briefs from BQ?
    (Sort by campaign_date ASC, LIMIT 200 — confirm sort field name)

22. How do we ensure the 42 most recent briefs are excluded from 
    learning and preserved cleanly for testing?
```

### 12.5 Brief Document Fetching
```
21. What format are the brief_url documents likely in?
    (Google Docs, Google Sheets, PDF — which is most common?)

22. How do we download and extract text from Google Docs via URL?
    (Google Drive API? Export as plain text?)

23. How do we download and extract text from PDFs?
    (PyPDF2? pdfplumber? Other?)

24. What Google API authentication is needed to access brief URLs?
    (OAuth 2.0? Service account? Shared link access?)

25. What if some brief URLs are broken, private, or no longer accessible?
    How do we handle gracefully?
```

### 12.6 Performance & Cost
```
26. End-to-end latency estimate:
    - Fetch brief URL from BQ: ?ms
    - Download brief document: ?ms
    - Claude translation (Sonnet 4.6, high effort): ?s
    - JSON validation: ?ms
    - Total: ? seconds

27. Monthly cost estimate at:
    - 50 briefs/month: $?
    - 100 briefs/month: $?
    - 500 briefs/month: $?
    (Include Claude API + BQ costs)

28. One-time cost to build glossary from 200 briefs: $?
    (And total cost including the 42 held-out briefs if we later use them)
```

### 12.7 Error Handling & Reliability
```
29. What are the most likely failure points in this pipeline?
    (Claude API timeout? BQ auth? Document fetch failure? 
    JSON validation failure?)

30. Recommend an error handling strategy for each failure mode

31. Should we build retry logic for Claude API calls?
    (How many retries? With exponential backoff?)

32. How do we log errors in a way that's easy for a non-DevOps 
    marketing team to debug?
```

### 12.8 JSON Schema & Downstream Tool Compatibility
```
33. Is the proposed JSON schema complete enough for downstream 
    tools to execute BQ queries without further interpretation?

34. Are there any fields in the JSON schema that are redundant, 
    missing, or ambiguous?

35. How should the sizing tool consume the JSON?
    (Show a code snippet: JSON → SQL query → BQ execution)

36. Should we version the JSON schema from day 1?
    (Recommendation: yes — what's the simplest versioning approach?)
```

### 12.9 Risk Assessment
```
37. What is the biggest technical risk in this plan?

38. What is the most likely reason this MVP would fail?

39. What should we build first to de-risk the project?

40. Are there any permission, security, or compliance risks 
    we haven't considered?

41. What's the minimum viable version of this MVP that proves 
    the concept in the shortest time?
```

---

## 13. Pre-Development Checklist

Before starting development, confirm:

### Team Inputs Needed
```
□ Confirm BQ table name (monday_campaign_data — exact name?)
□ Confirm BQ column names (brief_url, portfolio, purpose, cadence, medium, success_flag)
□ Confirm portfolio name to use for MVP
□ Confirm brief document format (Google Docs? PDFs? Sheets?)
□ Provide BQ customer schema (tables, fields, data types)
□ Provide seed data dictionary (20-30 business terms → BQ fields)
□ Provide 3-5 anonymized sample briefs for testing
□ Identify test brief (1 newer brief to validate against)
```

### Access & Permissions Needed
```
□ BigQuery access (read) to monday_campaign_data table
□ BigQuery access (read) to customer data tables
□ Google Drive/Docs API access (to download brief documents)
□ Anthropic API key (Claude Sonnet 4.6)
□ Confirm no IT restrictions on running Python CLI tools locally
```

### Decisions Needed Before Building
```
□ Confirm portfolio for MVP
□ Confirm brief document format
□ Confirm where glossary will be stored (local file vs BQ table)
□ Confirm where JSON output will be stored (local file vs BQ table)
□ Confirm who owns the API key and how it's shared securely
```

---

## 14. Summary

| Attribute | Value |
|---|---|
| **Tool Name** | Vibe Briefing |
| **Type** | CLI tool (Python) |
| **MVP Timeline** | 1-2 weeks |
| **Primary Model** | Claude Sonnet 4.6 |
| **Effort Level** | high |
| **Data Source** | BigQuery (monday_campaign_data table) |
| **Learning Data** | 200 oldest briefs (1 portfolio, sorted by date ASC) |
| **Held-out Test Set** | 42 most recent briefs (not used in learning) |
| **Test Data** | 1 brief from held-out set (same portfolio, lightly different criteria) |
| **Output** | Standardized JSON contract |
| **Downstream** | Sizing tool (existing) + future tools |
| **Est. Monthly Cost** | ~$3–30 (depending on volume) |
| **Est. One-time Learning Cost** | ~$3–4 (analyze 200 briefs once) |
| **Primary Risk** | Brief document access (URL format/permissions) |
| **Success Metric** | ≥90% data rule correctness on test brief |

---

*This document is intended to be shared with Claude Code for feasibility assessment. Claude Code should work through Section 12 (Feasibility Assessment Questions) systematically and provide findings, recommendations, and any blockers before development begins.*
