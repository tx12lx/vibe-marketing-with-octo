"""
Vibe OCTO Quant — Technical Auditor Agent

Receives a validated AudienceSizingRequest from Nexus and:
  1. Strictly rejects any payload that does not conform to AudienceSizingRequest.
  2. Generates a BigQuery waterfall SQL query via Fuel iX Claude.
  3. Executes the query against BigQuery using ADC credentials.
  4. Masks customer PII in Python before returning results.
  5. Parses the waterfall rows and computes an optimization note.
  6. Returns a QuantAuditLog on success, or a NexusErrorPayload on any failure —
     never raises raw exceptions to the orchestrator.
"""
from __future__ import annotations

import json
import os
import re
import sys
import warnings
from pathlib import Path
from typing import Optional, Union

import requests
from dotenv import load_dotenv
from pydantic import ValidationError

_QUANT_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _QUANT_DIR.parent

for _p in [str(_QUANT_DIR), str(_ROOT_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

load_dotenv(_QUANT_DIR / ".env")

from pydantic_schemas import (
    AdHocSizingRequest,
    AudienceSizingRequest,
    NexusErrorPayload,
    QuantAuditLog,
    WaterfallLayer,
)

_FUELIX_BASE = "https://api.fuelix.ai"
_DEFAULT_MODEL = "claude-sonnet-4"

_QUANT_SYSTEM = (
    "You are Vibe OCTO Quant, a BigQuery technical auditor for a Canadian telecom "
    "marketing team. "
    "You translate audience sizing requests into precise BigQuery Standard SQL waterfall "
    "queries.\n\n"
    "=== GROUNDED DATA ENVIRONMENT ===\n\n"
    "TABLE 1 (Primary Mobility Spine):\n"
    "  `bi-srv-hsmdet-pr-7b9def.adobe.bq_fda_mob_mobility_base`\n"
    "  Sizing aggregate: always COUNT(DISTINCT ban) — no other sizing aggregate.\n"
    "  Confirmed columns: ban, lang_pref, curr_pplan, province, device_name, device_type,\n"
    "  tenure, curr_credit_class_cd, sub_count, start_service_date, lob_desc, mnh_ffh_ban,\n"
    "  stop_sell, hp_ind, hsia_ind, shs_ind, optik_ind, lwc_ind, stream_ind, tos_ind,\n"
    "  smart_energy_ind, pfe_hsia_ind, pfe_tv_ind, shs_elig, pfe_hsia_elig, pfe_tv_elig,\n"
    "  stream_elig, tos_elig, smart_energy_elig, optik_elig, hsia_elig, control_group_flg,\n"
    "  msf, mrc, arpu, ban_arpu, soc_desc_e, soc_desc_f, em_dnc, ob_dnc, sms_dnc, dm_dnc,\n"
    "  handset_tenure, prizm_socialgrp_cd, prizm_socialgrp_nm, prizm_lifestage_cd,\n"
    "  prizm_lifestage_nm, allowance_qty, unit_of_measure_cd, standard_exclusions,\n"
    "  pending_order_ind, primary_sub, commit_start_date, commit_end_date, sub_status,\n"
    "  init_activation_date\n\n"
    "  WATERFALL FILTER SEQUENCE — apply as sequential cumulative CTE layers in this\n"
    "  exact order. Never collapse them into a single flat WHERE clause.\n"
    "  Step 1 — Base Universe / LOB   : UPPER(lob_desc) IN (...) only — no other filters\n"
    "  Step 2 — Primary Subscriber    : adds primary_sub = 1\n"
    "  Step 3 — Standard Exclusions   : adds standard_exclusions = 0 AND sub_status = 'A'\n"
    "  Step 4 — Stop Sell             : adds stop_sell = 0\n"
    "  Step 5 — Targeting Criteria    : houses the intersection of all custom parameters\n"
    "                                   extracted from the brief or NL request — service\n"
    "                                   exclusions (e.g., excluding EPP), multi-province\n"
    "                                   boundaries, behavioral metrics (e.g., adding a line\n"
    "                                   in the past 3 months), lifecycle windows, cross-sell\n"
    "                                   pairs, model scores, AAL behavioral self-join\n"
    "                                   exclusions / triggers, NBA model joins.\n"
    "                                   DNC flags belong exclusively in Step 6 — never here.\n"
    "  Step 6 — Channel Governance    : dedicated exclusively to communication preference\n"
    "                                   and DNC flag isolation. Four team-governed outbound\n"
    "                                   channels tracked via INT64 flags (0=Allowed,\n"
    "                                   1=Suppressed): em_dnc (Email), sms_dnc (SMS),\n"
    "                                   ob_dnc (Outbound Dialing), dm_dnc (Direct Mail).\n"
    "                                   CHANNEL EXCLUSIVITY SIEVE: when a request specifies\n"
    "                                   that an audience is 'only', 'exclusively', or 'solely'\n"
    "                                   eligible for a particular channel or combination:\n"
    "                                     Named channels   : set their DNC flag = 0\n"
    "                                     Unnamed channels : force their DNC flag = 1\n"
    "                                   Example 'only eligible to receive SMS':\n"
    "                                     sms_dnc = 0 AND em_dnc = 1 AND ob_dnc = 1 AND dm_dnc = 1\n"
    "                                   Example 'only eligible for email and SMS':\n"
    "                                     em_dnc = 0 AND sms_dnc = 0 AND ob_dnc = 1 AND dm_dnc = 1\n"
    "                                   If no channel exclusivity is specified, apply only the\n"
    "                                   DNC constraints explicitly stated in the request.\n"
    "                                   GCH RECENCY SUPPRESSION: when the exclusions list\n"
    "                                   contains a 'GCH recency suppression' entry, apply\n"
    "                                   the LEFT JOIN anti-join pattern (TABLE 3 above)\n"
    "                                   alongside DNC constraints — see GCH SUPPRESSION\n"
    "                                   RULE below for the required CTE 6 structure.\n"
    "  Step 7 — Universal Control Group : adds control_group_flg = 'N' — MUST be the\n"
    "                                    absolute last CTE layer, never moved or merged\n"
    "                                    upward; this step yields the final targetable\n"
    "                                    list volume\n\n"
    "  Strict typing rules:\n"
    "  - INT64 flags (1=True/Active, 0=False/Inactive): ALL columns matching *_ind, *_elig,\n"
    "    plus primary_sub, standard_exclusions, and stop_sell.\n"
    "    Example: WHERE primary_sub = 1 AND standard_exclusions = 0 AND stop_sell = 0\n"
    "             AND shs_ind = 1\n"
    "  - STRING flags: sub_status (use 'A' for active targeting) and control_group_flg\n"
    "    ('Y'=Yes, 'N'=No).\n"
    "    Example: WHERE sub_status = 'A' AND control_group_flg = 'N'\n\n"
    "TABLE 2 (Propensity / NBA Scores):\n"
    "  `bi-srv-hsmdet-pr-7b9def.adobe.bq_fda_current_model_score_master_view`\n"
    "  Join to Table 1 exclusively via:\n"
    "    FROM `bi-srv-hsmdet-pr-7b9def.adobe.bq_fda_mob_mobility_base` t1\n"
    "    INNER JOIN `bi-srv-hsmdet-pr-7b9def.adobe.bq_fda_current_model_score_master_view` t2\n"
    "      ON t1.ban = t2.ban\n"
    "  Confirmed columns: ban, predict_modl_id, classn_nm (product model identifier),\n"
    "  seg_nm (reco score tier), part_load_dt\n\n"
    "  Model-isolated partition rule: when filtering Table 2 for a specific predict_modl_id,\n"
    "  the MAX(part_load_dt) subquery MUST filter by that exact same predict_modl_id inside\n"
    "  the subquery to isolate per-model load schedules. Required structure:\n"
    "    WHERE t2.predict_modl_id = [TARGET_ID]\n"
    "      AND t2.part_load_dt = (\n"
    "        SELECT MAX(inner_t2.part_load_dt)\n"
    "        FROM `bi-srv-hsmdet-pr-7b9def.adobe.bq_fda_current_model_score_master_view` inner_t2\n"
    "        WHERE inner_t2.predict_modl_id = [TARGET_ID]\n"
    "      )\n\n"
    "TABLE 3 (Global Contact History — Recency Suppression):\n"
    "  Dataset: bi-srv-hsmdet-pr-7b9def.gch_current\n"
    "  Three tables used exclusively for recency suppression anti-joins inside CTE 6.\n\n"
    "  bq_campaign_segment (alias 'a'):\n"
    "    MOB_BAN, SEGMENT_ID, CAMPAIGN_ID, DELETED_IND, CONTROL_FLG, IN_HOME_DT\n"
    "    Mandatory operational filters: a.DELETED_IND = '0' AND a.CONTROL_FLG = 'N'\n"
    "    Link to mobility base: a.MOB_BAN = t.ban\n\n"
    "  bq_campaign_communication (alias 'b'):\n"
    "    SEGMENT_ID — joined via: a.SEGMENT_ID = b.SEGMENT_ID\n\n"
    "  bq_campaign_description (alias 'c'):\n"
    "    CAMPAIGN_ID, CAMPAIGN_CD, CAMPAIGN_SUB_CD — joined via: a.CAMPAIGN_ID = c.CAMPAIGN_ID\n\n"
    "  GCH SUPPRESSION RULE — when the exclusions list contains a 'GCH recency suppression'\n"
    "  entry, restructure CTE 6 using the LEFT JOIN anti-join pattern below. Do NOT use a\n"
    "  correlated NOT EXISTS subquery — it breaks when BigQuery resolves outer CTE aliases.\n\n"
    "  Required CTE 6 structure (substitute <CAMPAIGN_CD>, <CAMPAIGN_SUB_CD>, <N>, <dnc>):\n\n"
    "    after_channel_governance AS (\n"
    "      SELECT t.ban\n"
    "      FROM `bi-srv-hsmdet-pr-7b9def.adobe.bq_fda_mob_mobility_base` t\n"
    "      INNER JOIN after_targeting_criteria tg ON t.ban = tg.ban\n"
    "      LEFT JOIN (\n"
    "        SELECT DISTINCT a_gch.MOB_BAN\n"
    "        FROM `bi-srv-hsmdet-pr-7b9def.gch_current.bq_campaign_segment` a_gch\n"
    "        INNER JOIN `bi-srv-hsmdet-pr-7b9def.gch_current.bq_campaign_communication` b_gch\n"
    "          ON a_gch.SEGMENT_ID = b_gch.SEGMENT_ID\n"
    "        INNER JOIN `bi-srv-hsmdet-pr-7b9def.gch_current.bq_campaign_description` c_gch\n"
    "          ON a_gch.CAMPAIGN_ID = c_gch.CAMPAIGN_ID\n"
    "        WHERE a_gch.DELETED_IND = '0'\n"
    "          AND a_gch.CONTROL_FLG = 'N'\n"
    "          AND c_gch.CAMPAIGN_CD = '<CAMPAIGN_CD>'\n"
    "          AND c_gch.CAMPAIGN_SUB_CD = '<CAMPAIGN_SUB_CD>'\n"
    "          AND a_gch.IN_HOME_DT >= DATE_SUB(CURRENT_DATE(), INTERVAL <N> DAY)\n"
    "      ) gch ON t.ban = gch.MOB_BAN\n"
    "      WHERE t.primary_sub = 1\n"
    "        AND t.standard_exclusions = 0\n"
    "        AND t.sub_status = 'A'\n"
    "        AND t.stop_sell = 0\n"
    "        AND <dnc_constraints>\n"
    "        AND gch.MOB_BAN IS NULL\n"
    "    )\n\n"
    "  Substitute <CAMPAIGN_CD>, <CAMPAIGN_SUB_CD>, and <N> from the suppression entry.\n"
    "  Default: CAMPAIGN_CD = 'AAL', CAMPAIGN_SUB_CD = 'AALBAU', N = 30.\n"
    "  Replace <dnc_constraints> with the applicable DNC flag predicates (e.g. em_dnc = 0).\n"
    "  GCH aliases a_gch / b_gch / c_gch avoid conflicts with any outer CTE alias.\n\n"
    "=== BUSINESS RULE MATRICES ===\n\n"
    "LINE OF BUSINESS (lob_desc) MAPPING — always use UPPER(lob_desc) IN (...):\n"
    "  'Postpaid'  -> UPPER(lob_desc) IN ('TELUS POSTPAID', 'KOODO POSTPAID', 'TELUS EPP')\n"
    "               CRITICAL: 'TELUS EPP' is MANDATORY for every Postpaid filter. Omitting\n"
    "               it silently under-counts the audience. The IN list must contain all three\n"
    "               values — 'TELUS POSTPAID', 'KOODO POSTPAID', and 'TELUS EPP' — every time.\n"
    "  'Prepaid'   -> UPPER(lob_desc) IN ('TELUS PREPAID', 'KOODO PREPAID')\n"
    "  TWA (Telus Wireless Ambassador) is EXCLUDED from all queries by default unless\n"
    "  explicitly requested — never add TWA values to any IN list unprompted.\n"
    "  Combinations are supported: union the relevant IN lists when multiple LOBs are\n"
    "  requested (e.g., 'Postpaid and Prepaid' merges both value sets into one IN clause).\n\n"
    "CUSTOMER LIFECYCLE / TIME LOGIC — translate these shorthand terms exactly:\n"
    "  T-X renewal window (e.g., T-3 means within 3 months of contract end):\n"
    "    commit_end_date >= CURRENT_DATE()\n"
    "    AND commit_end_date <= DATE_ADD(CURRENT_DATE(), INTERVAL X MONTH)\n"
    "  MTM (Month-to-Month, no active contract):\n"
    "    commit_end_date < CURRENT_DATE()\n"
    "  BYOD (Bring Your Own Device, no device financing):\n"
    "    commit_start_date IS NULL\n\n"
    "PROVINCE CODES — the province column uses 2-letter codes. Most provinces have a\n"
    "  single canonical code. Quebec is the exception — it exists under TWO legacy codes\n"
    "  in this dataset: 'QC' and 'PQ'. Whenever a request includes or excludes Quebec\n"
    "  (referred to by any name: 'Quebec', 'QC', or 'PQ'), the generated SQL must handle\n"
    "  both codes together using an IN or NOT IN list — never filter on one alone:\n"
    "    Include Quebec  : UPPER(province) IN ('QC', 'PQ')\n"
    "    Exclude Quebec  : UPPER(province) NOT IN ('QC', 'PQ')\n"
    "  Filtering only UPPER(province) = 'QC' or LIKE '%QC%' silently drops 'PQ' records.\n\n"
    "CROSS-SELL / FFH PRODUCT TARGETING — when a request targets customers for\n"
    "  cross-sell or promotion of an FFH product, ALWAYS pair the ownership index\n"
    "  (= 0, does not currently have the product) with the eligibility index\n"
    "  (= 1, is technically eligible). Never filter on eligibility or ownership alone\n"
    "  for cross-sell use cases. Required pairs by product:\n"
    "    hsia        : hsia_ind = 0 AND hsia_elig = 1\n"
    "    shs         : shs_ind = 0 AND shs_elig = 1\n"
    "    optik       : optik_ind = 0 AND optik_elig = 1\n"
    "    stream      : stream_ind = 0 AND stream_elig = 1\n"
    "    tos         : tos_ind = 0 AND tos_elig = 1\n"
    "    smart_energy: smart_energy_ind = 0 AND smart_energy_elig = 1\n"
    "    pfe_hsia    : pfe_hsia_ind = 0 AND pfe_hsia_elig = 1\n"
    "    pfe_tv      : pfe_tv_ind = 0 AND pfe_tv_elig = 1\n\n"
    "ADD-A-LINE (AAL) TARGETING — two distinct trigger patterns; both belong exclusively\n"
    "  in the Step 5 Targeting Criteria CTE layer. Apply whichever pattern the request invokes.\n\n"
    "  BEHAVIORAL AAL EXCLUSION / TRIGGER (recent-subscriber self-join on Table 1):\n"
    "  Trigger: request isolates or excludes accounts that recently Added a Line.\n"
    "  Technique: self-join Table 1 as t1 (primary_sub = 1) and t2 (primary_sub = 0) on the\n"
    "  same BAN. Compare t2.init_activation_date against CURRENT_DATE() for the lookback.\n"
    "  Required join structure:\n"
    "    FROM `bi-srv-hsmdet-pr-7b9def.adobe.bq_fda_mob_mobility_base` t1\n"
    "    INNER JOIN `bi-srv-hsmdet-pr-7b9def.adobe.bq_fda_mob_mobility_base` t2\n"
    "      ON  t1.ban  = t2.ban\n"
    "      AND t1.primary_sub = 1\n"
    "      AND t2.primary_sub = 0\n"
    "      AND t2.init_activation_date > t1.init_activation_date\n"
    "  Apply the user's lookback window on t2.init_activation_date. Example for 6 months:\n"
    "    AND t2.init_activation_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 6 MONTH)\n"
    "  To EXCLUDE these BANs from the audience: use NOT IN / NOT EXISTS against the BAN set\n"
    "  returned by the self-join. To TARGET them: use IN / EXISTS.\n"
    "  Never infer a lookback period — use only the period the user explicitly states.\n\n"
    "  PREDICTIVE AAL NBA MODEL (Table 2 propensity join):\n"
    "  Trigger: request targets based on the Add-A-Line recommendation model, propensity\n"
    "  tiers, or NBA scores.\n"
    "  Fixed identifiers — always use BOTH together, never one alone:\n"
    "    t2.predict_modl_id = 2008\n"
    "    t2.classn_nm = 'ADD_A_LINE'\n"
    "  Default tier filter (top 5 propensity deciles — apply unless user requests more):\n"
    "    t2.seg_nm IN ('reco_1', 'reco_2', 'reco_3', 'reco_4', 'reco_5')\n"
    "  Dynamic scaling — expand ONLY on explicit user instruction; never expand unprompted:\n"
    "    Moderate expansion : IN ('reco_1', 'reco_2', 'reco_3', 'reco_4', 'reco_5', 'reco_6')\n"
    "    Broad expansion    : up to 'reco_8' maximum — hard ceiling, never exceed reco_8\n"
    "  Model-isolated partition subquery (mandatory — always scope to predict_modl_id = 2008):\n"
    "    AND t2.part_load_dt = (\n"
    "      SELECT MAX(inner_t2.part_load_dt)\n"
    "      FROM `bi-srv-hsmdet-pr-7b9def.adobe.bq_fda_current_model_score_master_view` inner_t2\n"
    "      WHERE inner_t2.predict_modl_id = 2008\n"
    "    )\n\n"
    "=== BRIEF EXTRACTION SIEVE — TARGETING vs. SEGMENTATION ===\n\n"
    "CRITICAL: Campaign briefs contain two structurally distinct sections. Apply this sieve\n"
    "before translating any filter criterion into SQL. Failure to separate them produces\n"
    "phantom WHERE clauses that have no effect on COUNT(DISTINCT ban) but corrupt the\n"
    "logical integrity of the waterfall.\n\n"
    "SEGMENTATION CRITERIA — mathematical impact ZERO on audience volume. IGNORE entirely.\n"
    "  These describe post-sizing list operations: how the extracted list is divided for\n"
    "  creative or copy versioning. They never gate who enters the audience.\n"
    "  Recognised patterns — treat any of these as inert and do not translate into SQL:\n"
    "    - Language split ratios (e.g. '60% EN / 40% FR')\n"
    "    - A/B or multivariate test splits\n"
    "    - Creative version matrices (Version A / B / C)\n"
    "    - Copy version counts or segment sub-allocation percentages\n"
    "    - Control group split percentages (the control_group_flg = 'N' step handles this)\n"
    "    - English / French sub-segment headcounts or allocation tables\n"
    "  DO NOT translate any segmentation field into a WHERE clause predicate.\n\n"
    "TARGETING CRITERIA — 100% cognitive weight. Extract exclusively into CTE 5.\n"
    "  These define structural macro inclusions and exclusions that gate audience membership:\n"
    "    - Province codes (UPPER(province) IN / NOT IN)\n"
    "    - NBA model deciles (seg_nm IN ('reco_1', ..., 'reco_N'))\n"
    "    - Behavioral line age limits (init_activation_date lookback windows)\n"
    "    - LOB scope (UPPER(lob_desc) IN (...))\n"
    "    - Product ownership and eligibility pairs (X_ind = 0 AND X_elig = 1)\n"
    "    - Lifecycle windows (commit_end_date, T-X renewal, MTM)\n"
    "    - Service or EPP exclusions\n"
    "  DNC / channel flags (em_dnc, sms_dnc, ob_dnc, dm_dnc) belong exclusively in CTE 6,\n"
    "  not in CTE 5, regardless of how the brief labels them.\n\n"
    "=== GENERATION RULES ===\n"
    "- Never select customer PII (names, addresses, emails, phone numbers, IMEI)\n"
    "- Size audiences using COUNT(DISTINCT ban) — no other sizing aggregate\n"
    "- Always return exactly two output columns: layer_name STRING, audience_count INT64\n"
    "- Use a WITH clause CTE waterfall; each CTE builds cumulatively on the previous\n"
    "- Seven-step waterfall sequence is mandatory and NON-NEGOTIABLE for every query:\n"
    "    CTE 1 'Base Universe'              : LOB filter only (UPPER(lob_desc) IN (...))\n"
    "    CTE 2 'After: Primary Subscriber'  : cumulative + primary_sub = 1\n"
    "    CTE 3 'After: Standard Exclusions' : cumulative + standard_exclusions = 0 AND sub_status = 'A'\n"
    "    CTE 4 'After: Stop Sell'           : cumulative + stop_sell = 0\n"
    "    CTE 5 'After: Targeting Criteria'  : cumulative + intersection of all custom\n"
    "                                         parameters — service exclusions, province filters,\n"
    "                                         behavioral metrics, lifecycle windows, cross-sell\n"
    "                                         pairs, model scores; AAL behavioral self-join or\n"
    "                                         NBA model join lives here. DNC flags must NOT\n"
    "                                         appear here — they belong exclusively in CTE 6.\n"
    "    CTE 6 'After: Channel Governance'  : cumulative + DNC flag logic; also embed GCH\n"
    "                                         LEFT JOIN anti-join (TABLE 3) when exclusions\n"
    "                                         contain a 'GCH recency suppression' entry.\n"
    "                                         Apply channel exclusivity sieve when 'only',\n"
    "                                         'exclusively', or 'solely' pairs with a channel:\n"
    "                                         named channels = 0, all unnamed channels = 1.\n"
    "                                         Four governed flags: em_dnc, sms_dnc, ob_dnc, dm_dnc.\n"
    "    CTE 7 'After: Universal Control Group' : cumulative + control_group_flg = 'N' — always last\n"
    "- control_group_flg = 'N' must appear ONLY in CTE 7 and nowhere above it\n"
    "- Do not reference columns absent from the confirmed schema above\n"
    "- Use Standard SQL syntax; backtick-quote all table refs as `project.dataset.table`\n"
    "- Return ONLY the raw SQL — no markdown, no explanation, no trailing semicolon\n"
    "- Case-sensitive STRING columns — province, device_type, device_name — must\n"
    "  always be wrapped in UPPER() and matched with LIKE wildcards to prevent 0-count case\n"
    "  mismatches. Required pattern examples:\n"
    "    UPPER(province) LIKE '%BC%'\n"
    "    UPPER(device_type) LIKE '%SMARTPHONE%'\n"
    "    UPPER(device_name) LIKE '%IPHONE%'\n"
    "  Never use bare equality (=) on these columns.\n"
    "  EXCEPTION — lob_desc must NEVER be filtered with LIKE. Always use the exact IN list\n"
    "  from the LOB MAPPING above. Using LIKE '%POSTPAID%' silently excludes 'TELUS EPP'\n"
    "  customers and must never appear in generated SQL. Postpaid requires:\n"
    "    UPPER(lob_desc) IN ('TELUS POSTPAID', 'KOODO POSTPAID', 'TELUS EPP')\n\n"
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

_WATERFALL_SQL_PROMPT = """Generate a BigQuery audience waterfall query for this sizing request.

Campaign   : {campaign_name} ({campaign_code} / {campaign_sub_code})
Population : {target_population}
Filters    : {filters_json}
Exclusions : {exclusions_json}
BQ Project : {bq_project}
BQ Dataset : {bq_dataset}

Note: Filters and Exclusions above have been sieved by Nexus to contain only Targeting
Criteria (province scope, propensity deciles, lifecycle windows, GCH recency suppression,
product pairs, channel governance flags). Segmentation criteria — copy splits, language
ratios, creative version rules — have been discarded upstream. Do not reintroduce them.

Available schema:
{schema_context}

Waterfall structure required — seven mandatory layers in this exact sequence:
  CTE 1 'Base Universe'              : LOB filter only — UPPER(lob_desc) IN (...)
  CTE 2 'After: Primary Subscriber'  : cumulative + primary_sub = 1
  CTE 3 'After: Standard Exclusions' : cumulative + standard_exclusions = 0 AND sub_status = 'A'
  CTE 4 'After: Stop Sell'           : cumulative + stop_sell = 0
  CTE 5 'After: Targeting Criteria'  : cumulative + intersection of all custom parameters
                                       from Filters and Exclusions above — service exclusions,
                                       province filters, behavioral metrics, lifecycle windows,
                                       cross-sell pairs, model scores; for AAL use cases
                                       apply the behavioral self-join rule
                                       (init_activation_date + lookback window) or the
                                       predictive NBA model join (predict_modl_id = 2008,
                                       classn_nm = 'ADD_A_LINE') per the system rules.
                                       DNC flags must NOT appear here.
  CTE 6 'After: Channel Governance'  : cumulative + DNC flag constraints; also embed the
                                       GCH LEFT JOIN anti-join (TABLE 3) when Exclusions
                                       contains a 'GCH recency suppression' entry — resolve
                                       CAMPAIGN_CD, CAMPAIGN_SUB_CD, and interval days from
                                       the suppression string (default AAL/AALBAU, 30 days).
                                       Apply channel exclusivity sieve when 'only',
                                       'exclusively', or 'solely' pairs with a channel:
                                       named channels = 0, all unnamed channels = 1.
                                       Four governed flags: em_dnc, sms_dnc, ob_dnc, dm_dnc.
  CTE 7 'After: Universal Control Group' : cumulative + control_group_flg = 'N' — absolute last

The final SELECT is a UNION ALL of COUNT(DISTINCT ban) from each CTE in sequence order.
control_group_flg = 'N' must NOT appear in any CTE above CTE 7.

Constraints:
- Never SELECT any customer identifier values in output — only aggregate counts
- Apply filters cumulatively (each CTE builds on the previous WHERE clause)
- Use ONLY the filter criteria listed above — do not add extra WHERE conditions from
  historical campaign knowledge or assumed targeting patterns not present in Filters
- If the schema does not contain an expected field, use the closest available field
  and add a comment explaining the substitution

OUTPUT: return raw SQL only — no prose, no fences, no semicolon.
The first character must be 'W' (WITH). Any explanatory text causes a pipeline parse failure."""

_EXTREME_DROP = 0.60
_HIGH_SCRUB_RATE = 0.80

# Canonical seven-step display order.  _parse_waterfall sorts by position in this
# list so the waterfall is always chronological regardless of BQ row return order.
_WATERFALL_STEP_ORDER = [
    "Base Universe",
    "After: Primary Subscriber",
    "After: Standard Exclusions",
    "After: Stop Sell",
    "After: Targeting Criteria",
    "After: Channel Governance",
    "After: Universal Control Group",
]

_ADHOC_WATERFALL_PROMPT = """Generate a BigQuery audience waterfall query for this ad-hoc sizing request.

Population : {target_population}
Filters    : {filters_json}
BQ Project : {bq_project}
BQ Dataset : {bq_dataset}

Available schema:
{schema_context}

Waterfall structure required — seven mandatory layers in this exact sequence:
  CTE 1 'Base Universe'              : LOB filter only — UPPER(lob_desc) IN (...)
  CTE 2 'After: Primary Subscriber'  : cumulative + primary_sub = 1
  CTE 3 'After: Standard Exclusions' : cumulative + standard_exclusions = 0 AND sub_status = 'A'
  CTE 4 'After: Stop Sell'           : cumulative + stop_sell = 0
  CTE 5 'After: Targeting Criteria'  : cumulative + intersection of all custom parameters
                                       from the Filters list — service exclusions, province
                                       filters, behavioral metrics, lifecycle windows,
                                       cross-sell pairs, model scores; for AAL use cases
                                       apply the behavioral self-join rule
                                       (init_activation_date + lookback window) or the
                                       predictive NBA model join (predict_modl_id = 2008,
                                       classn_nm = 'ADD_A_LINE') per the system rules.
                                       DNC flags must NOT appear here.
  CTE 6 'After: Channel Governance'  : cumulative + DNC flag constraints only.
                                       Apply channel exclusivity sieve when 'only',
                                       'exclusively', or 'solely' pairs with a channel:
                                       named channels = 0, all unnamed channels = 1.
                                       Four governed flags: em_dnc, sms_dnc, ob_dnc, dm_dnc.
  CTE 7 'After: Universal Control Group' : cumulative + control_group_flg = 'N' — absolute last

The final SELECT is a UNION ALL of COUNT(DISTINCT ban) from each CTE in sequence order.
control_group_flg = 'N' must NOT appear in any CTE above CTE 7.
Apply filters cumulatively — each CTE re-applies all prior WHERE conditions plus the new one.
Use ONLY the filter criteria listed above.

OUTPUT: return raw SQL only — no prose, no fences, no semicolon.
The first character must be 'W' (WITH). Any explanatory text causes a pipeline parse failure."""


class QuantAgent:
    def __init__(self) -> None:
        self._api_key = os.getenv("FUELIX_API_KEY")
        if not self._api_key:
            raise RuntimeError("FUELIX_API_KEY not set in Vibe OCTO Quant/.env")
        self._model = os.getenv("FUELIX_MODEL", _DEFAULT_MODEL)
        self._default_project = os.getenv("BQ_PROJECT_ID", "bi-srv-hsmdet-pr-7b9def")
        self._default_dataset = os.getenv("BQ_DATASET", "adobe")
        self._schema_cache = _QUANT_DIR / ".schema_cache.json"
        self._last_sql: str = ""

    # ------------------------------------------------------------------
    # Public API — strict gateway, never raises to orchestrator
    # ------------------------------------------------------------------

    def audit(self, payload: dict) -> Union[QuantAuditLog, NexusErrorPayload]:
        """Validate payload and run the full audit pipeline.

        Returns QuantAuditLog on success, NexusErrorPayload on any failure.
        Raw exceptions are suppressed — Nexus receives structured error context.
        """
        try:
            request = AudienceSizingRequest(**payload)
        except ValidationError as exc:
            field_errors = "; ".join(
                f"{'.'.join(str(l) for l in e['loc'])}: {e['msg']}"
                for e in exc.errors()[:3]
            )
            return NexusErrorPayload(
                error_type="validation_error",
                error_summary=(
                    f"Payload rejected — {exc.error_count()} field error(s). {field_errors}"
                ),
                original_request=payload,
                retry_hint=(
                    "Ensure all required fields are present and correctly typed. "
                    "Required: campaign_name (str), campaign_code (str), "
                    "campaign_sub_code (str), cadence (str), medium (str), "
                    "target_population (str), filters (list[str] — at least one entry), "
                    "bq_project (str), bq_dataset (str)."
                ),
            )

        self._last_sql = ""
        try:
            return self._run_audit(request)
        except Exception as exc:
            return NexusErrorPayload(
                error_type="database_error",
                error_summary=str(exc)[:400],
                original_request=payload,
                failed_sql=self._last_sql or None,
                retry_hint=(
                    "Check that target_population and filters use standard telecom "
                    "marketing terminology recognisable in the BQ schema. "
                    "Verify ADC credentials are active for the BQ project."
                ),
            )

    def direct_count(self, request: AdHocSizingRequest) -> Union[QuantAuditLog, NexusErrorPayload]:
        """Path 2 — execute a seven-step waterfall count query for an ad-hoc sizing request."""
        print("[QUANT AGENT] -> Strategizing SQL translation and BigQuery optimization...")
        print("  Constructing multi-stage sequential CTE blocks. Injecting optimized partition")
        print("  filters for Model tables and applying baseline marketing exclusions...\n")
        try:
            schema = self._fetch_schema(request.bq_project, request.bq_dataset)
            sql = self._generate_adhoc_waterfall_sql(request, schema)
            _print_sql_block(sql)
            raw_rows = self._execute_query(sql, request.bq_project)
            masked_rows = _mask_pii(raw_rows)
            waterfall = _parse_waterfall(masked_rows)
            note = _optimization_note(waterfall)
            final_count = _final_audience_count(waterfall)
            return QuantAuditLog(
                request=request,
                sql=sql,
                waterfall=waterfall,
                final_count=final_count,
                optimization_note=note,
            )
        except Exception as exc:
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

    # ------------------------------------------------------------------
    # Audit pipeline
    # ------------------------------------------------------------------

    def _run_audit(self, request: AudienceSizingRequest) -> QuantAuditLog:
        print("[QUANT AGENT] -> Strategizing SQL translation and BigQuery optimization...")
        print("  Constructing multi-stage sequential CTE blocks. Injecting optimized partition")
        print("  filters for Model tables and applying baseline marketing exclusions...\n")
        schema = self._fetch_schema(request.bq_project, request.bq_dataset)
        sql = self._generate_waterfall_sql(request, schema)
        _print_sql_block(sql)
        raw_rows = self._execute_query(sql, request.bq_project)
        masked_rows = _mask_pii(raw_rows)
        waterfall = _parse_waterfall(masked_rows)
        note = _optimization_note(waterfall)
        final_count = _final_audience_count(waterfall)

        return QuantAuditLog(
            request=request,
            sql=sql,
            waterfall=waterfall,
            final_count=final_count,
            optimization_note=note,
        )

    def _fetch_schema(self, project: str, dataset: str) -> str:
        try:
            from bq_reporter.bq_client import get_schema  # type: ignore

            return get_schema(
                project,
                [dataset],
                cache_path=self._schema_cache,
            )
        except Exception:
            return f"-- Schema unavailable for {project}.{dataset}"

    def _generate_waterfall_sql(
        self, request: AudienceSizingRequest, schema: str
    ) -> str:
        prompt = _WATERFALL_SQL_PROMPT.format(
            campaign_name=request.campaign_name,
            campaign_code=request.campaign_code,
            campaign_sub_code=request.campaign_sub_code,
            target_population=request.target_population,
            filters_json=json.dumps(request.filters, ensure_ascii=False),
            exclusions_json=json.dumps(request.exclusion_layers or [], ensure_ascii=False),
            bq_project=request.bq_project,
            bq_dataset=request.bq_dataset,
            schema_context=schema[:6000] if schema else "(not available)",
        )
        resp = requests.post(
            f"{_FUELIX_BASE}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": _QUANT_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 4096,
                "temperature": 0,
            },
            timeout=180,
        )
        resp.raise_for_status()
        sql = _clean_sql(resp.json()["choices"][0]["message"]["content"].strip())
        self._last_sql = sql
        if os.getenv("QUANT_DEBUG_SQL"):
            print(f"[Quant SQL — waterfall]\n{sql}\n", file=sys.stderr)
        _validate_sql_structure(sql)
        return sql

    def _generate_adhoc_waterfall_sql(self, request: AdHocSizingRequest, schema: str) -> str:
        prompt = _ADHOC_WATERFALL_PROMPT.format(
            target_population=request.target_population or "UNSPECIFIED",
            filters_json=json.dumps(
                [f for f in (request.filters or []) if f is not None],
                ensure_ascii=False,
            ),
            bq_project=request.bq_project or self._default_project,
            bq_dataset=request.bq_dataset or self._default_dataset,
            schema_context=schema[:6000] if schema else "(not available)",
        )
        resp = requests.post(
            f"{_FUELIX_BASE}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": _QUANT_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 4096,
                "temperature": 0,
            },
            timeout=180,
        )
        resp.raise_for_status()
        sql = _clean_sql(resp.json()["choices"][0]["message"]["content"].strip())
        self._last_sql = sql
        if os.getenv("QUANT_DEBUG_SQL"):
            print(f"[Quant SQL — ad-hoc]\n{sql}\n", file=sys.stderr)
        _validate_sql_structure(sql)
        return sql

    def _execute_query(self, sql: str, project: str) -> list[dict]:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from google.cloud import bigquery  # type: ignore

            client = bigquery.Client(project=project)

        rows = [dict(r) for r in client.query(sql).result()]
        return rows


# ------------------------------------------------------------------
# Pure functions — PII masking, waterfall parsing, audit note
# ------------------------------------------------------------------

def _mask_pii(rows: list[dict]) -> list[dict]:
    """Remove hidden PII columns and mask filter-only PII values in-place."""
    try:
        from bq_reporter.bq_client import _is_pii, _is_filter_only_pii  # type: ignore
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


def _print_sql_block(sql: str) -> None:
    sep = "-" * 40
    print(sep)
    print("[QUANT] -> GENERATING PRODUCTION SQL")
    print(sep)
    print(sql)
    print()


def _log_waterfall(waterfall: list[WaterfallLayer]) -> None:
    if not waterfall:
        return
    width_label = max(len(l.layer_name) for l in waterfall)
    divider = "-" * (width_label + 18)
    print(f"\n{'AUDIENCE WATERFALL':^{width_label + 18}}")
    print(divider)
    for layer in waterfall:
        print(f"  {layer.layer_name:<{width_label}}  {layer.audience_count:>12,}")
    print(divider)


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
    layers.sort(key=lambda l: _waterfall_step_index(l.layer_name))
    return layers


def _waterfall_step_index(name: str) -> int:
    try:
        return _WATERFALL_STEP_ORDER.index(name)
    except ValueError:
        pass
    # Fuzzy fallback: match by containment after CTE prefix has been stripped.
    name_lower = name.lower()
    for i, canonical in enumerate(_WATERFALL_STEP_ORDER):
        if canonical.lower() in name_lower:
            return i
    return len(_WATERFALL_STEP_ORDER)


def _final_audience_count(waterfall: list[WaterfallLayer]) -> int:
    """Return the count from the Universal Control Group step (Step 7).

    Falls back to the last layer in the sorted waterfall if the canonical name
    is not found, so behaviour degrades gracefully if the model emits a
    non-standard label.
    """
    ucg = next(
        (l for l in waterfall if "Universal Control Group" in l.layer_name),
        waterfall[-1] if waterfall else None,
    )
    return ucg.audience_count if ucg else 0


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


def _validate_sql_structure(sql: str) -> None:
    """Raise ValueError with a diagnostic message if the SQL is structurally incomplete.

    Called immediately after _clean_sql so that truncated or malformed output is
    caught before it reaches BigQuery and produces a cryptic 400 error.
    """
    if not re.match(r"^\s*WITH\b", sql, re.IGNORECASE):
        raise ValueError(
            "Generated SQL does not begin with WITH — response may have been "
            "prefixed with prose or truncated before the query start."
        )

    # A trailing comma at EOF means the UNION ALL execution block never arrived.
    if re.search(r",\s*$", sql):
        raise ValueError(
            "Generated SQL ends with a trailing comma — the UNION ALL execution "
            "block is absent. Increase max_tokens and retry."
        )

    union_count = len(re.findall(r"\bUNION\s+ALL\b", sql, re.IGNORECASE))
    if union_count < 6:
        raise ValueError(
            f"Generated SQL contains {union_count} UNION ALL clause(s); a 7-step "
            "waterfall requires exactly 6. The query appears truncated — "
            "increase max_tokens."
        )

    open_parens = sql.count("(")
    close_parens = sql.count(")")
    if open_parens != close_parens:
        raise ValueError(
            f"Generated SQL has unbalanced parentheses ({open_parens} open vs "
            f"{close_parens} close) — likely truncated inside a subquery or "
            "DATE_ADD/DATE_SUB expression."
        )

    if "control_group_flg" not in sql:
        raise ValueError(
            "Generated SQL is missing the mandatory control_group_flg = 'N' filter "
            "(step 6). The final CTE was not generated."
        )
