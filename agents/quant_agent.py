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

_AGENTS_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _AGENTS_DIR.parent
_QUANT_DIR = _ROOT_DIR / "Vibe OCTO Quant"  # original subdirectory for .env loading

for _p in [str(_ROOT_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

load_dotenv(_QUANT_DIR / ".env")

from pydantic_schemas import (
    AdHocSizingRequest,
    AudienceSizingRequest,
    NexusErrorPayload,
    QuantAuditLog,
    UniversalJSONSpec,
    WaterfallLayer,
)
from core.base_agent import BaseAgent
from core.thought_display import ThoughtDisplay  # noqa: E402
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from core.knowledge_context import KnowledgeContext

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
    "  WATERFALL FILTER SEQUENCE — two fixed anchors with request-determined middle steps.\n"
    "  Never collapse filters into a single flat WHERE clause.\n\n"
    "  ANCHOR 1 (always CTE 1): 'Base Universe'\n"
    "    SELECT * FROM `<mobility_base>` WHERE UPPER(lob_desc) IN (...) AND standard_exclusions = 0\n\n"
    "  ANCHOR 2 (always last CTE): 'Final Targetable Audience'\n"
    "    Always applies control_group_flg = 'N'. Never moved, merged, or renamed.\n"
    "    Label must be exactly: 'Final Targetable Audience'\n\n"
    "  DYNAMIC MIDDLE STEPS (total 3-10 CTEs including both anchors):\n"
    "  Include only steps that apply meaningful filters. Pass-through CTEs are forbidden.\n"
    "  Name each step in plain business language (no column names, no SQL identifiers).\n\n"
    "  Step types to include when applicable:\n"
    "    Active Eligible Subscribers : Always for mobility.\n"
    "      WHERE primary_sub = 1 AND sub_status = 'A' AND standard_exclusions = 0 AND stop_sell = 0\n"
    "      Combine active/standard-exclusions/stop-sell into one step when all apply.\n"
    "    Geographic Filter           : Include when province scope is specified.\n"
    "    Product Eligibility         : Include when cross-sell ownership/eligibility pairs apply.\n"
    "    Lifecycle Window            : Include when contract/tenure criteria are specified.\n"
    "    NBA Model Filter            : Include when propensity model join is required.\n"
    "    GCH Suppression             : Include when exclusion_layers contains a GCH entry.\n"
    "      Apply the LEFT JOIN anti-join pattern (TABLE 3) — see GCH SUPPRESSION RULE below.\n"
    "    Channel Governance          : Include when DNC constraints apply.\n"
    "      Four flags: em_dnc (Email), sms_dnc (SMS), ob_dnc (Outbound), dm_dnc (Direct Mail).\n"
    "      CHANNEL EXCLUSIVITY SIEVE: when 'only', 'exclusively', or 'solely' pairs with\n"
    "      a channel: named channels = 0, unnamed channels = 1.\n"
    "      Example 'only eligible to receive SMS':\n"
    "        sms_dnc = 0 AND em_dnc = 1 AND ob_dnc = 1 AND dm_dnc = 1\n"
    "      Example 'only eligible for email and SMS':\n"
    "        em_dnc = 0 AND sms_dnc = 0 AND ob_dnc = 1 AND dm_dnc = 1\n"
    "      Channel Governance and GCH Suppression always precede 'Final Targetable Audience'.\n\n"
    "  DNC flags (em_dnc, sms_dnc, ob_dnc, dm_dnc) belong exclusively in Channel Governance.\n"
    "  Never include DNC flags in any other step.\n\n"
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
    "  FIXED GCH CORE SCHEMA CONTRACT — aliases, joins, keys, and column assignments below\n"
    "  are immutable. Any deviation is an absolute compilation error.\n\n"
    "  bq_campaign_segment (ALWAYS alias as a_gch):\n"
    "    Key fields: SEGMENT_ID, CAMPAIGN_ID, DELETED_IND, CONTROL_FLG, IN_HOME_DT\n"
    "    Mandatory operational filters: a_gch.DELETED_IND = '0' AND a_gch.CONTROL_FLG = 'N'\n\n"
    "  bq_campaign_communication (ALWAYS alias as b_gch):\n"
    "    Key fields: SEGMENT_ID, MOB_BAN\n"
    "    Join to a_gch: b_gch.SEGMENT_ID = a_gch.SEGMENT_ID\n"
    "    Link to mobility base: b_gch.MOB_BAN = t.ban\n\n"
    "  bq_campaign_description (ALWAYS alias as c_gch):\n"
    "    Key fields: CAMPAIGN_ID, CAMPAIGN_CD, CAMPAIGN_SUB_CD\n"
    "    Join to a_gch: c_gch.CAMPAIGN_ID = a_gch.CAMPAIGN_ID\n\n"
    "  TARGETING KEY LAW: MOB_BAN lives exclusively in b_gch (bq_campaign_communication).\n"
    "  Any GCH subquery, CTE select, or anti-join extraction MUST compile as\n"
    "  SELECT DISTINCT b_gch.MOB_BAN. Sourcing MOB_BAN from a_gch or c_gch is an absolute\n"
    "  compilation error and will produce incorrect suppression results.\n\n"
    "  DATE CASTING LAW: a_gch.IN_HOME_DT is a DATETIME type. When evaluating lookback\n"
    "  windows or execution intervals, always wrap in DATE():\n"
    "    DATE(a_gch.IN_HOME_DT) >= DATE_SUB(CURRENT_DATE(), INTERVAL X DAY)\n"
    "  A bare comparison without DATE() is a DATETIME/DATE type mismatch and must never\n"
    "  appear in generated SQL.\n\n"
    "  GCH SUPPRESSION RULE — when the exclusions list contains a 'GCH recency suppression'\n"
    "  entry, embed the LEFT JOIN anti-join pattern in the Channel Governance step (or a\n"
    "  dedicated GCH Suppression step). Do NOT use a correlated NOT EXISTS subquery —\n"
    "  it breaks when BigQuery resolves outer CTE aliases.\n\n"
    "  Required Channel Governance CTE structure (substitute <CAMPAIGN_CD>, <CAMPAIGN_SUB_CD>, <N>, <dnc>):\n\n"
    "    after_channel_governance AS (\n"
    "      SELECT t.*\n"
    "      FROM after_targeting_criteria t\n"
    "      LEFT JOIN (\n"
    "        SELECT DISTINCT b_gch.MOB_BAN\n"
    "        FROM `bi-srv-hsmdet-pr-7b9def.gch_current.bq_campaign_segment` a_gch\n"
    "        INNER JOIN `bi-srv-hsmdet-pr-7b9def.gch_current.bq_campaign_communication` b_gch\n"
    "          ON a_gch.SEGMENT_ID = b_gch.SEGMENT_ID\n"
    "        INNER JOIN `bi-srv-hsmdet-pr-7b9def.gch_current.bq_campaign_description` c_gch\n"
    "          ON a_gch.CAMPAIGN_ID = c_gch.CAMPAIGN_ID\n"
    "        WHERE a_gch.DELETED_IND = '0'\n"
    "          AND a_gch.CONTROL_FLG = 'N'\n"
    "          AND c_gch.CAMPAIGN_CD = '<CAMPAIGN_CD>'\n"
    "          AND c_gch.CAMPAIGN_SUB_CD = '<CAMPAIGN_SUB_CD>'\n"
    "          AND DATE(a_gch.IN_HOME_DT) >= DATE_SUB(CURRENT_DATE(), INTERVAL <N> DAY)\n"
    "      ) gch ON t.ban = gch.MOB_BAN\n"
    "      WHERE <dnc_constraints>\n"
    "        AND gch.MOB_BAN IS NULL\n"
    "    )\n\n"
    "  Substitute <CAMPAIGN_CD>, <CAMPAIGN_SUB_CD>, and <N> from the suppression entry;\n"
    "  these values are resolved at runtime from the active campaign configuration.\n"
    "  Replace <dnc_constraints> with the applicable DNC flag predicates (e.g. em_dnc = 0).\n"
    "  GCH table aliases a_gch / b_gch / c_gch are fixed and must never be changed.\n\n"
    "TABLE 4 (FFH / Home Solutions Customer Profile):\n"
    "  `bi-srv-hsmdet-pr-7b9def.adobe.bq_dly_dbm_customer_profl`\n"
    "  Use for: all FFH (Fixed and Home) and Home Solutions customer queries — residential\n"
    "  internet, TV, home phone, SHS, and MNP (Mobile-to-Home / Home-to-Mobile) campaigns.\n"
    "  Sizing aggregate: COUNT(DISTINCT BACCT_NUM) — NEVER use ban (column does not exist).\n"
    "  CONFIRMED FFH columns: BACCT_NUM (account key, INT64), SERV_PROV (province, STRING),\n"
    "  MNH_MOB_BAN (mobility BAN link, INT64 — IS NULL = no linked mobility plan),\n"
    "  EX_STANDARD_EX (standard exclusions, INT64: 0 = not excluded),\n"
    "  FFH_STOPSELL_IND (stop sell flag, INT64: 0 = not stop-sold),\n"
    "  CONTROL_GROUP_FLG (STRING: 'N' = not in control group),\n"
    "  CC_DNEM (email DNC, INT64), CC_DNSM (SMS DNC), CC_DNRS (outbound DNC),\n"
    "  CC_DNDM (direct mail DNC).\n"
    "  COLUMNS ABSENT FROM FFH TABLE — DO NOT USE for FFH queries:\n"
    "    ban, province, standard_exclusions, primary_sub, sub_status, stop_sell,\n"
    "    lob_desc, em_dnc, sms_dnc, ob_dnc, dm_dnc, control_group_flg (lowercase).\n\n"
    "  TABLE SELECTION ROUTING — evaluate BEFORE writing any query:\n"
    "  - Mobility / wireless / postpaid / prepaid / Koodo / TELUS mobile → TABLE 1\n"
    "  - FFH / Home Solutions / residential / internet / TV / home phone → TABLE 4\n"
    "  Context signals for FFH: user says 'FFH', 'Home Solutions', 'internet customers',\n"
    "  'TV customers', 'MNP', 'home phone', 'residential', 'DBM', or references\n"
    "  mnh_mob_ban / bq_dly_dbm_customer_profl directly.\n"
    "  NEVER use bq_fda_mob_mobility_base for FFH customer queries — it lacks mnh_mob_ban\n"
    "  and will return zero results or incorrect counts.\n\n"
    "=== BUSINESS RULE MATRICES ===\n\n"
    "LINE OF BUSINESS (lob_desc) MAPPING — always use UPPER(lob_desc) IN (...):\n"
    "  'Postpaid'  -> UPPER(lob_desc) IN ('TELUS POSTPAID', 'KOODO POSTPAID', 'TELUS EPP')\n"
    "               CRITICAL: 'TELUS EPP' is MANDATORY for every generic Postpaid filter.\n"
    "               Omitting it silently under-counts the audience. The IN list must contain\n"
    "               all three values — 'TELUS POSTPAID', 'KOODO POSTPAID', and 'TELUS EPP'.\n"
    "  'TELUS Postpaid' / 'TELUS Mobility' (TELUS brand only — no Koodo, no EPP):\n"
    "               -> WHERE UPPER(lob_desc) = 'TELUS POSTPAID'\n"
    "               Use equality (=) when the brief explicitly targets TELUS subscribers\n"
    "               only. This excludes KOODO POSTPAID and TELUS EPP by design.\n"
    "  'Prepaid'   -> UPPER(lob_desc) IN ('TELUS PREPAID', 'KOODO PREPAID')\n"
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
    "NAKED DISAMBIGUATION — two structurally opposite populations, two different tables:\n"
    "  'naked mobility' = mobility customer with NO linked FFH household\n"
    "    Table  : TABLE 1 — `bq_fda_mob_mobility_base`\n"
    "    Filter : mnh_ffh_ban = 0\n"
    "  'naked FFH' = FFH/Home Solutions customer with NO linked mobility plan\n"
    "    Table  : TABLE 4 — `bq_dly_dbm_customer_profl`\n"
    "    Filter : MNH_MOB_BAN IS NULL   [NOTE: FFH table uses UPPERCASE column names]\n"
    "    Size   : COUNT(DISTINCT BACCT_NUM)  [NOT ban — FFH table uses BACCT_NUM]\n"
    "  Context signals — choose TABLE 4 (naked FFH) when request contains: 'FFH customers',\n"
    "  'Home Solutions customers', 'naked FFH', 'naked home', 'residential customers'.\n"
    "  Choose TABLE 1 (naked mobility) when: 'mobility customers', 'wireless customers',\n"
    "  'postpaid customers', or plain 'naked customers' with no home-service context.\n\n"
    "NAKED MOBILITY — when a request targets 'naked' MOBILITY customers (mobility-only, no\n"
    "  bundled FFH household), apply exactly one filter: mnh_ffh_ban = 0.\n"
    "  mnh_ffh_ban is an INT64 flag: 0 = no linked FFH household, 1 = has FFH bundle.\n"
    "  CRITICAL: never substitute individual product indicator columns (shs_ind, optik_ind,\n"
    "  stream_ind, tos_ind, smart_energy_ind, lwc_ind, hp_ind) for this filter — they are\n"
    "  cross-sell eligibility flags and are not equivalent to the household bundle status.\n\n"
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
    "- UNION ALL alias consistency — ABSOLUTE REQUIREMENT: every SELECT arm in the\n"
    "  final UNION ALL reporting block MUST carry both explicit column aliases on every\n"
    "  row. Never rely on positional resolution — BigQuery UNION ALL requires consistent\n"
    "  column schemas across all arms. The number of arms equals the number of CTEs (3-10).\n"
    "  Schema identical for every arm:\n"
    "    SELECT '<step_label>' AS layer_name, COUNT(DISTINCT ban) AS audience_count FROM <cte_name>\n"
    "  First arm is always 'Base Universe'; last arm is always 'Final Targetable Audience'.\n"
    "  Each layer_name string must exactly match the CTE label chosen for that step.\n"
    "  Omitting AS layer_name or AS audience_count on any arm causes a BigQuery schema\n"
    "  mismatch — treat every arm as a standalone SELECT with no inherited aliases.\n"
    "- Linear SELECT * inheritance — MANDATORY: every CTE carries ALL columns forward\n"
    "  from the immediately preceding stage so downstream WHERE clauses can reference\n"
    "  any column (control_group_flg, em_dnc, stop_sell, etc.) without ambiguity.\n"
    "  Never project only ban; never go back to the raw mobility_base table after CTE 1.\n"
    "  Pattern for every middle and anchor CTE:\n"
    "    <cte_name> : SELECT * FROM <prior_cte> WHERE <this_step_filter>\n"
    "  EXCEPTION: when joining Table 2 (model scores) or performing a Table 1 self-join\n"
    "  (AAL behavioral), alias the preceding CTE as t and use SELECT t.* to carry all\n"
    "  columns while joining the extra table.\n"
    "  EXCEPTION: when GCH suppression is required, use SELECT t.* FROM <prior> t\n"
    "  LEFT JOIN ... per the GCH SUPPRESSION RULE template above.\n"
    "  Each CTE adds exactly one new predicate layer on top of the prior CTE output.\n"
    "  ban is always present in every CTE because it is inherited through each SELECT *;\n"
    "  COUNT(DISTINCT ban) in the final UNION ALL is therefore unambiguous.\n"
    "- Use a WITH clause CTE waterfall; each CTE builds cumulatively on the previous\n"
    "- Waterfall CTE sequence — two fixed anchors with dynamic middle (3-10 total CTEs):\n"
    "    CTE 1 'Base Universe'                : UPPER(lob_desc) IN (...) AND standard_exclusions = 0\n"
    "    CTEs 2 to N-1 (dynamic middle)       : request-specific steps in business-language order\n"
    "                                           Active subscriber, stop sell, geographic, product\n"
    "                                           eligibility, lifecycle, NBA model, GCH suppression,\n"
    "                                           channel governance — include only when applicable.\n"
    "                                           Every step must filter meaningfully; no pass-throughs.\n"
    "    Last CTE 'Final Targetable Audience' : cumulative + control_group_flg = 'N' — always last\n"
    "- control_group_flg = 'N' must appear ONLY in 'Final Targetable Audience' and nowhere above it\n"
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
    "  EXCEPTION — lob_desc must NEVER be filtered with LIKE. Always use the exact mapping\n"
    "  from the LOB MAPPING above. Using LIKE '%POSTPAID%' is forbidden. Two valid forms:\n"
    "    Generic Postpaid (all brands)     : UPPER(lob_desc) IN ('TELUS POSTPAID', 'KOODO POSTPAID', 'TELUS EPP')\n"
    "    TELUS Mobility only (brand-scoped): WHERE UPPER(lob_desc) = 'TELUS POSTPAID'\n\n"
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

Waterfall structure required — two fixed anchors with request-determined middle steps.
Total CTEs: 3-10 (choose based on which filters actually apply; no pass-throughs allowed):

  CTE 1 (always): "Base Universe"
    SELECT * FROM `{bq_project}.{bq_dataset}.<table>` WHERE UPPER(lob_desc) IN (...) AND standard_exclusions = 0

  Dynamic middle CTEs — include only when the filter applies:
    Active Eligible Subscribers  : WHERE primary_sub = 1 AND sub_status = 'A' AND standard_exclusions = 0 AND stop_sell = 0
                                   Combine into one step when all apply.
    Geographic Filter            : province scope (when specified in Filters/Exclusions)
    Product Eligibility          : ownership/eligibility pairs (when cross-sell in Filters)
    Lifecycle Window             : commit_end_date / T-X window (when specified in Filters)
    NBA Model Filter             : Table 2 join (when propensity model in Filters)
    GCH Suppression              : LEFT JOIN anti-join per system rules (when GCH in Exclusions)
                                   Resolve CAMPAIGN_CD, CAMPAIGN_SUB_CD, interval days from
                                   the suppression entry in Exclusions above.
    Channel Governance           : DNC flag constraints; must precede "Final Targetable Audience"
                                   Apply channel exclusivity sieve when 'only', 'exclusively',
                                   or 'solely' pairs with a channel: named = 0, unnamed = 1.
                                   Four governed flags: em_dnc, sms_dnc, ob_dnc, dm_dnc.
                                   When GCH in Exclusions, embed GCH LEFT JOIN anti-join here
                                   using SELECT t.* FROM <prior> t LEFT JOIN ... pattern.

  Last CTE (always): "Final Targetable Audience"
    SELECT * FROM <prior_cte> WHERE control_group_flg = 'N'
    Label must be exactly "Final Targetable Audience"

The final SELECT is a UNION ALL of COUNT(DISTINCT ban) from each CTE in sequence order.
Every arm MUST carry explicit column aliases — no arm may omit AS layer_name or AS audience_count.
Schema identical for every arm:
  SELECT '<step_label>' AS layer_name, COUNT(DISTINCT ban) AS audience_count FROM <cte_name>
  UNION ALL ...
First arm is always "Base Universe"; last arm is always "Final Targetable Audience".
control_group_flg = 'N' must NOT appear in any CTE above "Final Targetable Audience".

CTE structure rules — non-negotiable:
- Linear SELECT * inheritance: every CTE selects ALL columns from the immediately preceding
  CTE so that downstream WHERE clauses can reference any column without ambiguity.
    <any_cte>  : SELECT * FROM <prior_cte> WHERE <this_step_filter>
    EXCEPTION: when joining Table 2 or a self-join, alias the preceding CTE as t and use
    SELECT t.* to carry all columns while joining.
    EXCEPTION: GCH suppression uses SELECT t.* FROM <prior> t LEFT JOIN ... per system rules.

Constraints:
- Never SELECT any customer identifier values in output — only aggregate counts
- Apply filters cumulatively (each CTE builds on the previous WHERE clause)
- Use ONLY the filter criteria listed above — do not add extra WHERE conditions from
  historical campaign knowledge or assumed targeting patterns not present in Filters
- If the schema does not contain an expected field, use the closest available field
  and add a comment explaining the substitution

OUTPUT: return raw SQL only — no prose, no fences, no semicolon.
The first character must be 'W' (WITH). Any explanatory text causes a pipeline parse failure."""

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

FINAL SELECT: UNION ALL of COUNT(DISTINCT BACCT_NUM) from each CTE.
First arm label: "Base Universe". Last arm label: "Final Targetable Audience".
"""


def _is_ffh_request(
    request: "AudienceSizingRequest",
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

The final SELECT is a UNION ALL of COUNT(DISTINCT ban) from each CTE in sequence order.
Every arm MUST carry explicit column aliases — no arm may omit AS layer_name or AS audience_count.
Schema identical for every arm:
  SELECT '<step_label>' AS layer_name, COUNT(DISTINCT ban) AS audience_count FROM <cte_name>
  UNION ALL ...
First arm is always "Base Universe"; last arm is always "Final Targetable Audience".
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


class QuantAgent(BaseAgent):
    WORKER_ID = "quant_v1"
    HANDLED_INTENTS: frozenset[str] = frozenset({"sizing_request", "campaign_execution"})
    INPUT_SCHEMA = UniversalJSONSpec
    OUTPUT_SCHEMA = QuantAuditLog

    def __init__(self) -> None:
        self._api_key = os.getenv("FUELIX_API_KEY")
        if not self._api_key:
            raise RuntimeError("FUELIX_API_KEY not set in Vibe OCTO Quant/.env")
        self._model = os.getenv("FUELIX_MODEL", _DEFAULT_MODEL)
        self._default_project = os.getenv("BQ_PROJECT_ID", "bi-srv-hsmdet-pr-7b9def")
        self._default_dataset = os.getenv("BQ_DATASET", "adobe")
        self._schema_cache = _QUANT_DIR / ".schema_cache.json"
        self._last_sql: str = ""
        self._session_context: str = ""
        self._runtime_schema: str = ""
        self._knowledge_ctx: Optional["KnowledgeContext"] = None

    def set_knowledge_context(self, ctx: "KnowledgeContext") -> None:
        """Bind the centralised KnowledgeContext built at startup."""
        self._knowledge_ctx = ctx

    # ------------------------------------------------------------------
    # BaseAgent contract
    # ------------------------------------------------------------------

    def subscribe(self, spec: UniversalJSONSpec) -> None:
        """Store the UniversalJSONSpec for the current execution cycle."""
        self._pending_spec: Optional[UniversalJSONSpec] = spec

    def execute(self) -> QuantAuditLog:
        """Execute the audit pipeline against the subscribed spec.

        Delegates to audit_from_spec(). If no spec has been subscribed,
        returns a NexusErrorPayload-equivalent wrapped as an audit failure.
        """
        pending = getattr(self, "_pending_spec", None)
        if pending is None:
            raise RuntimeError("subscribe() must be called before execute()")
        return self.audit_from_spec(pending)

    def set_session_context(self, context: str) -> None:
        """Receive dynamic glossary/catalog context from the orchestrator for prompt injection."""
        self._session_context = context

    def set_runtime_schema(self, schema_str: str) -> None:
        """Receive the live INFORMATION_SCHEMA snapshot injected by the orchestrator (Pillar 2).

        Stored for use in SQL generation prompts. The injected string contains
        live column metadata from INFORMATION_SCHEMA.COLUMNS, distinct from the
        VIEW DDL fetched by _fetch_schema(). Both can be used together: VIEW DDL
        provides field types for SQL generation; the snapshot provides structural
        coverage for zero-shot BRONZE path reasoning.
        """
        self._runtime_schema = schema_str

    # ------------------------------------------------------------------
    # Public API — strict gateway, never raises to orchestrator
    # ------------------------------------------------------------------

    def audit_from_spec(
        self, spec: UniversalJSONSpec
    ) -> Union[QuantAuditLog, NexusErrorPayload]:
        """Accept a UniversalJSONSpec and delegate to the existing audit() pipeline.

        Downcasts spec to AudienceSizingRequest via to_audience_sizing_request().
        The 7-step CTE waterfall, PII masking, optimization notes, and error
        boundary logic are entirely unchanged.
        """
        return self.audit(spec.to_audience_sizing_request().model_dump())

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
            raw_rows = self._execute_query(sql, request.bq_project)
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
        ThoughtDisplay.sql_generation(
            request.campaign_name,
            len(request.filters or []),
            len(request.exclusion_layers or []),
        )
        schema = self._fetch_schema(request.bq_project, request.bq_dataset)
        sql = self._generate_waterfall_sql(request, schema)
        ThoughtDisplay.progress("Audience blueprint ready. Running the analysis now...")
        raw_rows = self._execute_query(sql, request.bq_project)
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
        # Append FFH column override when the request targets the Home Solutions table.
        if _is_ffh_request(request):
            ffh_project = request.bq_project or self._default_project
            prompt = prompt + "\n" + _FFH_WATERFALL_OVERRIDE.format(bq_project=ffh_project)
        # Apply optimization_context corrections when present.
        opt_ctx = (request.optimization_context or "").strip()
        if opt_ctx:
            prompt = (
                prompt
                + f"\n\nCOLUMN NAME OVERRIDES — supersede all schema and waterfall definitions above."
                f" Apply these substitutions exactly as stated:\n{opt_ctx}\n"
            )
        sql = self._call_sql(prompt)
        self._last_sql = sql
        if os.getenv("QUANT_DEBUG_SQL"):
            print(f"[Quant SQL — waterfall]\n{sql}\n", file=sys.stderr)
        return sql

    def _generate_adhoc_waterfall_sql(self, request: AdHocSizingRequest, schema: str) -> str:
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

        use_thinking = bool(os.getenv("FUELIX_EXTENDED_THINKING"))
        thinking_payload: dict = (
            {"thinking": {"type": "enabled", "budget_tokens": 10000}} if use_thinking else {}
        )
        temperature = 1 if use_thinking else 0
        beta_headers = (
            "prompt-caching-2024-07-31,interleaved-thinking-2025-05-14"
            if use_thinking
            else "prompt-caching-2024-07-31"
        )

        if self._knowledge_ctx is not None:
            cached_block = {
                "type": "text",
                "text": (
                    "VIBE OCTO PROVEN SQL PATTERNS\n"
                    "(Column patterns and business rules from all GOLD campaigns)\n\n"
                    + self._knowledge_ctx.quant_context
                ),
                "cache_control": {"type": "ephemeral"},
            }
            query_block = {"type": "text", "text": prompt}
            resp = requests.post(
                f"{_FUELIX_BASE}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "anthropic-beta": beta_headers,
                },
                json={
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": [cached_block, query_block]},
                    ],
                    "max_tokens": 4096,
                    "temperature": temperature,
                    **thinking_payload,
                },
                timeout=180,
            )
        else:
            resp = requests.post(
                f"{_FUELIX_BASE}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    **({"anthropic-beta": "interleaved-thinking-2025-05-14"} if use_thinking else {}),
                },
                json={
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 4096,
                    "temperature": temperature,
                    **thinking_payload,
                },
                timeout=180,
            )

        resp.raise_for_status()
        return _clean_sql(resp.json()["choices"][0]["message"]["content"].strip())

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


