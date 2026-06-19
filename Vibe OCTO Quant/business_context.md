# Business Context — Mobility Sizing Tool (MVP)

SQL patterns for sizing the mobility subscriber base. Assumes plain-English
prompts using the terminology below. For ambiguous or campaign-driven sizing,
use the structured-JSON path instead.

---

## bq_fda_mob_mobility_base

One row per mobility subscriber line. Lowercase column names.

### MANDATORY template — every sizing query starts here

```sql
SELECT COUNT(DISTINCT ban)
FROM `bi-srv-hsmdet-pr-7b9def.adobe.bq_fda_mob_mobility_base`
WHERE sub_status = 'A'
  AND standard_exclusions = 0
  AND primary_sub = 1
  AND stop_sell = 0
  AND control_group_flg = 'N'
  -- additional filters appended below
```

Always include the five clauses above. Append one `AND` clause per dimension the user mentions. Never drop a dimension. Never invent one.

### TWO CRITICAL STICKY RULES — apply EVERY time, no exceptions

**STICKY RULE #1 — "Eligible for X" is a TELUS DOMAIN TERM with a fixed meaning.**

In this team's vocabulary, "eligible for X" does NOT mean "the eligibility flag is on". It is a campaign-targeting term that means TWO things together:
  (a) the customer does NOT have X yet, AND
  (b) the customer qualifies for X.

So whenever you see ANY of these phrases:
  "eligible for X", "X campaign candidates", "campaign-eligible for X",
  "cross-sell X", "upsell to X", "X opportunity"

You MUST emit BOTH clauses, joined by `AND`:
  `AND <root>_ind = 0 AND <root>_elig = 1`

Wrong output for "eligible for HSIA": `AND hsia_ind = 1`        ← FORBIDDEN
Wrong output for "eligible for HSIA": `AND hsia_elig = 1`        ← FORBIDDEN (missing _ind = 0)
Correct output for "eligible for HSIA": `AND hsia_ind = 0 AND hsia_elig = 1`  ← REQUIRED

Same applies to every product: Optik, SHS, HP, LWC, Stream+, TOS, Smart Energy, PFE_HSIA, PFE_TV.

**STICKY RULE #2 — Quebec is ALWAYS BOTH `'PQ'` AND `'QC'`.**
Any mention of Quebec by any name ("QC", "PQ", "Quebec") in any list (positive `IN` or negative `NOT IN`)
→ MUST include BOTH `'PQ'` AND `'QC'` in the list.
Examples:
- "in QC" → `province IN ('PQ','QC')` (BOTH)
- "outside AB/BC and QC" → `province NOT IN ('AB','BC','PQ','QC')` (BOTH)
- "in ON and QC" → `province IN ('ON','PQ','QC')` (BOTH)

Before emitting SQL, verify:
1. Every "eligible for X" phrase in the question produced TWO clauses (`_ind = 0 AND _elig = 1`).
2. Every Quebec reference produced BOTH `'PQ'` AND `'QC'` in the province list.

---

### Counting IDs
- "accounts" / "customers" → `COUNT(DISTINCT ban)`
- "subscribers" / "lines" → `COUNT(DISTINCT subscriber_no)`

### Lines of business — `lob_desc`

Specific brand named → `AND lob_desc = '<exact value>'`
- "Telus Postpaid", "Koodo Postpaid", "Telus EPP",
  "Telus Prepaid", "Koodo Prepaid", "Public Mobile"

Aggregate group (no carrier prefix) → `AND lob_desc IN (...)`
- "postpaid" → `IN ('Telus Postpaid','Koodo Postpaid','Telus EPP')`
- "prepaid"  → `IN ('Telus Prepaid','Koodo Prepaid','Public Mobile')`

Multiple specifics → `AND lob_desc IN ('A','B', ...)`.
No brand mentioned → omit the lob_desc filter entirely. Do NOT default to postpaid. "Mobility customers" alone does not imply postpaid.

### Province — `province`
Codes: `'ON'`, `'BC'`, `'AB'`, `'NS'`, `'MB'`, `'NB'`, `'NL'`.

**Quebec appears as BOTH `'PQ'` AND `'QC'`. Always include BOTH whenever the user mentions QC, PQ, or Quebec — in `IN` or `NOT IN` lists.**

- "in ON" → `AND province = 'ON'`
- "in QC" / "in PQ" / "in Quebec" → `AND province IN ('PQ','QC')`
- "in AB or BC" → `AND province IN ('AB','BC')`
- "outside AB/BC and QC" → `AND province NOT IN ('AB','BC','PQ','QC')`

### Customer type — `mnh_ffh_ban`
"Naked" is a customer-type modifier. It refers to customers WITHOUT home solutions services. It says NOTHING about brand or LOB.

CRITICAL: "naked", "naked mobility", or "MNH" alone NEVER implies a `lob_desc` filter. Do NOT default to postpaid when the user says "naked" without also naming a brand.

- "naked" / "naked mobility" / "mobility only" / "MNH" → `AND COALESCE(mnh_ffh_ban, 0) = 0`  (no lob_desc filter added)
- "naked postpaid" → TWO filters: `AND COALESCE(mnh_ffh_ban, 0) = 0 AND UPPER(lob_desc) IN ('TELUS POSTPAID','KOODO POSTPAID','TELUS EPP')`
- "MNH" / "M&H" / "has both mobility and home" → `AND COALESCE(mnh_ffh_ban, 0) > 0`

### Device — `device_type`
- "iPhone" / "Apple" → `AND device_type = 'Apple'`
- "Android" → `AND device_type = 'Android'`

### Lifecycle (date-derived)
- "BYOD" → `AND commit_start_date IS NULL`
- "T-3" / "in renewal window" → `AND commit_end_date BETWEEN CURRENT_DATE() AND DATE_ADD(CURRENT_DATE(), INTERVAL 3 MONTH)`
- "MTM" → `AND commit_end_date IS NOT NULL AND commit_end_date < CURRENT_DATE()`

### Products — two distinct patterns

Pick the pattern based on the user's exact wording.

**Pattern A — "has X" / "doesn't have X"** (current holding check)
- "has <product>" → `AND <root>_ind = 1`
- "doesn't have <product>" → `AND <root>_ind = 0`

**Pattern B — "eligible for X" (Home Solutions cross-sell convention)**
ANY of these phrases ALWAYS produces BOTH `_ind = 0` AND `_elig = 1` together — NEVER just `_ind = 1`:
- "eligible for X"
- "X campaign candidates" / "campaign-eligible for X"
- "cross-sell X" / "upsell to X"
- "X opportunity"

Result: `AND <root>_ind = 0 AND <root>_elig = 1`

**Product → column root** (same root for both patterns):
| User says | Root |
|---|---|
| "Internet" / "HSIA" | `hsia` |
| "Optik" / "Optik TV" | `optik` |
| "SHS" / "Smart Home Security" | `shs` |
| "Home Phone" / "HP" | `hp` |
| "LWC" / "Living Well Companion" | `lwc` |
| "Stream" / "Stream+" | `stream` |
| "TOS" / "TELUS Online Security" | `tos` |
| "Smart Energy" | `smart_energy` |
| "PureFibre East Internet" | `pfe_hsia` |
| "PureFibre East TV" | `pfe_tv` |

**Concrete examples across products — the pattern is identical regardless of product:**
- "has Internet" → `hsia_ind = 1`
- "doesn't have Internet" → `hsia_ind = 0`
- "eligible for Internet" → `hsia_ind = 0 AND hsia_elig = 1`
- "eligible for Stream+" → `stream_ind = 0 AND stream_elig = 1`
- "eligible for TOS" → `tos_ind = 0 AND tos_elig = 1`
- "eligible for LWC" → `lwc_ind = 0 AND lwc_elig = 1`
- "SHS campaign candidates" → `shs_ind = 0 AND shs_elig = 1`

### Channel CMP — can receive

`_dnc = 0` means CAN receive. Match the channel keyword exactly:

| Question contains | Clause |
|---|---|
| `SMS`, `text`, `MMS` | `AND sms_dnc = 0` |
| `email` | `AND em_dnc = 0` |
| `phone call`, `call`, `phonable` | `AND ob_dnc = 0` |

---

### Worked examples

**"Naked postpaid customers outside AB/BC and QC who are T-3"** — naked + brand group + Quebec dual-code + NOT IN + lifecycle
```sql
SELECT COUNT(DISTINCT ban)
FROM `bi-srv-hsmdet-pr-7b9def.adobe.bq_fda_mob_mobility_base`
WHERE sub_status = 'A' AND standard_exclusions = 0 AND primary_sub = 1
  AND stop_sell = 0
  AND control_group_flg = 'N'
  AND COALESCE(mnh_ffh_ban, 0) = 0
  AND lob_desc IN ('Telus Postpaid','Koodo Postpaid','Telus EPP')
  AND province NOT IN ('AB','BC','PQ','QC')
  AND commit_end_date BETWEEN CURRENT_DATE() AND DATE_ADD(CURRENT_DATE(), INTERVAL 3 MONTH)
```

**"Postpaid customers in AB or BC who can receive SMS and are eligible for Optik TV"** — group + multi-region + SMS CMP + eligibility
```sql
SELECT COUNT(DISTINCT ban)
FROM `bi-srv-hsmdet-pr-7b9def.adobe.bq_fda_mob_mobility_base`
WHERE sub_status = 'A' AND standard_exclusions = 0 AND primary_sub = 1
  AND stop_sell = 0
  AND control_group_flg = 'N'
  AND lob_desc IN ('Telus Postpaid','Koodo Postpaid','Telus EPP')
  AND province IN ('AB','BC')
  AND sms_dnc = 0
  AND optik_ind = 0 AND optik_elig = 1
```

**"Telus Postpaid MNH T-3 Apple users in AB or BC who can receive email and are eligible for SHS"** — single brand + MNH + lifecycle + device + multi-region + CMP + eligibility
```sql
SELECT COUNT(DISTINCT ban)
FROM `bi-srv-hsmdet-pr-7b9def.adobe.bq_fda_mob_mobility_base`
WHERE sub_status = 'A' AND standard_exclusions = 0 AND primary_sub = 1
  AND stop_sell = 0
  AND control_group_flg = 'N'
  AND lob_desc = 'Telus Postpaid'
  AND COALESCE(mnh_ffh_ban, 0) > 0
  AND commit_end_date BETWEEN CURRENT_DATE() AND DATE_ADD(CURRENT_DATE(), INTERVAL 3 MONTH)
  AND device_type = 'Apple'
  AND province IN ('AB','BC')
  AND em_dnc = 0
  AND shs_ind = 0 AND shs_elig = 1
```

---

## Output
- Only aggregate: `COUNT(...)` or `COUNT(DISTINCT ...)`.
- Always backtick the table: `` `bi-srv-hsmdet-pr-7b9def.adobe.bq_fda_mob_mobility_base` ``.
- Raw SQL only — no markdown, no explanation, no code fences, no trailing semicolon.

## Self-check before emitting SQL — verify ALL of these
1. Did the question say "eligible for <X>" / "<X> campaign candidates" / "cross-sell <X>"? If yes, your SQL MUST have `AND <X>_ind = 0 AND <X>_elig = 1`. Not `_ind = 1`. Not `_elig = 1` alone.
2. Did the question say "QC" / "PQ" / "Quebec"? If yes, your province list MUST contain BOTH `'PQ'` AND `'QC'`.
3. Did the question name a brand or say "postpaid"/"prepaid"? If yes, the SQL MUST contain a `lob_desc` clause. Do not silently drop the brand filter.
4. Are the 5 default filters present? They are ALWAYS required.
