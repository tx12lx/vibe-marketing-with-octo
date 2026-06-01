"""
Deterministic SQL builder for structured criteria.

This module bypasses the LLM entirely. When upstream tools (like a future
Monday.com campaign-study tool) produce structured JSON criteria, this builder
turns them into BigQuery SQL with 100% reliability and ~zero latency.

The same Pipeline that runs LLM-generated SQL can run this output — both
flow through the same dry-run + retry + execute path.

Expected criteria schema (all keys optional):

{
    "count_by": "ban" | "subscriber_no",                    # default "ban"
    "lob": "Telus Postpaid" | ["A","B"] | "postpaid" | "prepaid",
    "province": "ON" | ["ON","BC"],
    "province_exclude": ["AB","BC"],                        # NOT IN
    "device_type": "Apple" | "Android",
    "customer_type": "naked_mobility" | "MNH",
    "lifecycle": "BYOD" | "T-3" | "MTM",
    "eligible_for": ["hsia", "optik", ...],                 # cross-sell: ind=0 AND elig=1
    "channels_can_receive": ["email", "sms", "calls"]
}
"""
from __future__ import annotations

TABLE = "`bi-srv-hsmdet-pr-7b9def.adobe.bq_fda_mob_mobility_base`"

_BRAND_GROUPS = {
    "postpaid": ("Telus Postpaid", "Koodo Postpaid", "Telus EPP"),
    "prepaid":  ("Telus Prepaid", "Koodo Prepaid", "Public Mobile"),
}

_CHANNEL_COLUMNS = {
    "email": "em_dnc",
    "sms":   "sms_dnc",
    "calls": "ob_dnc",
    "call":  "ob_dnc",
}


def _lob_clause(value) -> str:
    if isinstance(value, str):
        if value.lower() in _BRAND_GROUPS:
            brands = _BRAND_GROUPS[value.lower()]
            quoted = ",".join(f"'{b}'" for b in brands)
            return f"lob_desc IN ({quoted})"
        return f"lob_desc = '{value}'"
    # list
    if len(value) == 1:
        return f"lob_desc = '{value[0]}'"
    quoted = ",".join(f"'{b}'" for b in value)
    return f"lob_desc IN ({quoted})"


def _expand_quebec(values: list[str]) -> list[str]:
    """If any Quebec code is present, ensure BOTH 'PQ' and 'QC' are included."""
    out = list(values)
    if "PQ" in out or "QC" in out or "Quebec" in out:
        out = [v for v in out if v != "Quebec"]
        if "PQ" not in out:
            out.append("PQ")
        if "QC" not in out:
            out.append("QC")
    return out


def _province_clause(value) -> str:
    items = [value] if isinstance(value, str) else list(value)
    items = _expand_quebec(items)
    if len(items) == 1:
        return f"province = '{items[0]}'"
    quoted = ",".join(f"'{p}'" for p in items)
    return f"province IN ({quoted})"


def _province_exclude_clause(value) -> str:
    items = _expand_quebec(list(value))
    quoted = ",".join(f"'{p}'" for p in items)
    return f"province NOT IN ({quoted})"


def _customer_type_clause(value: str) -> str:
    v = value.lower().replace("-", "_").replace(" ", "_")
    if v in ("naked", "naked_mobility", "mobility_only"):
        return "COALESCE(mnh_ffh_ban, 0) = 0"
    if v in ("mnh", "m_h", "m&h"):
        return "COALESCE(mnh_ffh_ban, 0) > 0"
    raise ValueError(f"Unknown customer_type: {value!r}")


def _lifecycle_clause(value: str) -> str:
    v = value.upper()
    if v == "BYOD":
        return "commit_start_date IS NULL"
    if v == "T-3":
        return "commit_end_date BETWEEN CURRENT_DATE() AND DATE_ADD(CURRENT_DATE(), INTERVAL 3 MONTH)"
    if v == "MTM":
        return "commit_end_date IS NOT NULL AND commit_end_date < CURRENT_DATE()"
    raise ValueError(f"Unknown lifecycle: {value!r}")


def _eligibility_clauses(products: list[str]) -> list[str]:
    return [f"{p}_ind = 0 AND {p}_elig = 1" for p in products]


def _channel_clauses(channels: list[str]) -> list[str]:
    out = []
    for ch in channels:
        col = _CHANNEL_COLUMNS.get(ch.lower())
        if not col:
            raise ValueError(f"Unknown channel: {ch!r}")
        out.append(f"{col} = 0")
    return out


def build_sql_from_criteria(criteria: dict) -> str:
    """Deterministically build a BigQuery sizing SQL from structured criteria."""
    count_col = criteria.get("count_by", "ban")
    if count_col not in ("ban", "subscriber_no"):
        raise ValueError(f"count_by must be 'ban' or 'subscriber_no', got {count_col!r}")

    where: list[str] = [
        "sub_status = 'A'",
        "standard_exclusions = 0",
        "primary_sub = 1",
        "stop_sell = 0",
        "control_group_flg = 'N'",
    ]

    if "lob" in criteria:
        where.append(_lob_clause(criteria["lob"]))
    if "province" in criteria:
        where.append(_province_clause(criteria["province"]))
    if "province_exclude" in criteria:
        where.append(_province_exclude_clause(criteria["province_exclude"]))
    if "device_type" in criteria:
        where.append(f"device_type = '{criteria['device_type']}'")
    if "customer_type" in criteria:
        where.append(_customer_type_clause(criteria["customer_type"]))
    if "lifecycle" in criteria:
        where.append(_lifecycle_clause(criteria["lifecycle"]))
    if "eligible_for" in criteria and criteria["eligible_for"]:
        where.extend(_eligibility_clauses(criteria["eligible_for"]))
    if "channels_can_receive" in criteria and criteria["channels_can_receive"]:
        where.extend(_channel_clauses(criteria["channels_can_receive"]))

    where_sql = "\n  AND ".join(where)
    return (
        f"SELECT COUNT(DISTINCT {count_col})\n"
        f"FROM {TABLE}\n"
        f"WHERE {where_sql}"
    )
