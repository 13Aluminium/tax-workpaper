"""
workpaper.py
------------
The "business logic" of the workpaper. This file combines:
    the packet (what the engine computed, read-only)
  + the database (what humans did: adjustments, sign-offs, notes...)
and answers questions like:
  - What is the CURRENT value of line SchC.1 after our adjustments?
  - Is the reviewer's sign-off on Schedule C still valid, or did something change?
  - What is the estimated tax effect of our adjustments?
  - Can we file yet? If not, what is blocking us?
"""

import hashlib
import json

from database import query, get_setting
from packet import get_packet

# Categories that are not "income" but money paid or credits.
# A $1 change here changes the balance due by $1 (in the opposite direction
# for payments: more payments = less to pay).
PAYMENT_CATEGORIES = {
    "federal_payment": "federal",
    "california_payment": "california",
}
# Expense categories borrow the rate of the income they reduce (sign flipped).
EXPENSE_CATEGORIES = {
    "schc_expense": "schc_profit",
    "rental_expense": "ordinary_income",
}
CREDIT_CATEGORIES = {
    "federal_credit": "federal",
    "california_credit": "california",
}


# ---------------------------------------------------------------------------
# Effective values (engine value + our adjustments)
# ---------------------------------------------------------------------------
def active_adjustments(target_type=None, target_id=None):
    """All adjustments that are not reverted (pending ones count, but are flagged)."""
    sql = "SELECT * FROM adjustments WHERE status != 'reverted'"
    args = []
    if target_type:
        sql += " AND target_type = ?"
        args.append(target_type)
    if target_id:
        sql += " AND target_id = ?"
        args.append(target_id)
    sql += " ORDER BY id"
    rows = query(sql, args)
    for r in rows:
        r["old_value"] = json.loads(r["old_value"]) if r["old_value"] is not None else None
        r["new_value"] = json.loads(r["new_value"]) if r["new_value"] is not None else None
    return rows


def latest_adjustment_by_target(target_type):
    """{target_id: newest active adjustment} - the newest one wins."""
    result = {}
    for adj in active_adjustments(target_type):
        result[adj["target_id"]] = adj
    return result


def effective_line_value(line_key, version=None, adj_map=None):
    """The value we currently stand behind: our adjustment if any, else the engine's."""
    packet = get_packet()
    version = version or packet.latest_version
    line = packet.line(line_key, version)
    if adj_map is None:
        adj_map = latest_adjustment_by_target("line")
    if line_key in adj_map:
        return adj_map[line_key]["new_value"]
    return line["value"] if line else None


def effective_household():
    """Household data with any corrections applied (e.g. a fixed SSN)."""
    packet = get_packet()
    people = json.loads(json.dumps(packet.raw["household"]["people"]))  # deep copy
    for adj in active_adjustments("household"):
        # target_id looks like "theo.ssn"
        person_id, field = adj["target_id"].split(".", 1)
        for p in people:
            if p["id"] == person_id:
                p[field] = adj["new_value"]
    return people


# ---------------------------------------------------------------------------
# Fingerprints for sign-offs
# ---------------------------------------------------------------------------
def _hash(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def fingerprint(target_type, target_id):
    """
    A short hash of "everything this sign-off covers".
    If any covered value changes later, the hash changes, and the sign-off
    automatically shows as REOPENED. No one has to remember to reopen it.
    """
    packet = get_packet()
    version = packet.latest_version
    adj_map = latest_adjustment_by_target("line")
    lines = packet.lines[version]

    if target_type == "line":
        return _hash([version, effective_line_value(target_id, version, adj_map)])

    if target_type == "return":
        covered = {k: effective_line_value(k, version, adj_map) for k in lines}
        return _hash([version, covered, effective_household()])

    return _hash([target_type, target_id])


def signoff_status(target_type, target_id):
    """
    For each level (preparer, reviewer, partner) return the latest
    sign-off and whether it is still valid ('signed') or 'reopened'.
    """
    rows = query(
        "SELECT * FROM signoffs WHERE target_type=? AND target_id=? AND status='active' ORDER BY id",
        (target_type, target_id),
    )
    current = fingerprint(target_type, target_id)
    result = {}
    for r in rows:  # later rows overwrite earlier ones -> newest wins
        result[r["level"]] = {
            "by": r["created_by"],
            "at": r["created_at"],
            "state": "signed" if r["fingerprint"] == current else "reopened",
        }
    return result


# ---------------------------------------------------------------------------
# Tax effect estimates
# ---------------------------------------------------------------------------
def estimate_tax_effect(delta, category, version=None):
    """
    We cannot re-run the engine. The packet gives "tax per $1" rates by
    category, so: effect = change in dollars x rate.
    Returns (federal_effect, california_effect) = change in BALANCE DUE.
    """
    packet = get_packet()
    version = version or packet.latest_version
    if delta is None or category in (None, "", "none"):
        return 0.0, 0.0
    if category in PAYMENT_CATEGORIES:
        # More payments -> lower balance due
        return (-delta, 0.0) if PAYMENT_CATEGORIES[category] == "federal" else (0.0, -delta)
    if category in CREDIT_CATEGORIES:
        return (-delta, 0.0) if CREDIT_CATEGORIES[category] == "federal" else (0.0, -delta)
    # Expenses work in the opposite direction of income: a $1 smaller expense
    # is a $1 bigger profit. So we use the income rate with the sign flipped.
    sign = 1
    if category in EXPENSE_CATEGORIES:
        category = EXPENSE_CATEGORIES[category]
        sign = -1
    rates = packet.versions[version]["tax_effect_rates"].get(category)
    if not rates:
        return 0.0, 0.0
    return (round(sign * delta * rates["federal_per_dollar"], 2),
            round(sign * delta * rates["california_per_dollar"], 2))


def adjusted_summary(version=None):
    """
    Engine summary for a version + the estimated effect of our adjustments.
    Clearly an ESTIMATE: the real system would recompute.
    """
    packet = get_packet()
    version = version or packet.latest_version
    base = json.loads(json.dumps(packet.versions[version]["summary"]))
    adjusted = json.loads(json.dumps(base))
    fed_tax = ca_tax = fed_pay = ca_pay = 0.0
    for adj in active_adjustments("line"):
        if adj["category"] in PAYMENT_CATEGORIES:
            if PAYMENT_CATEGORIES[adj["category"]] == "federal":
                fed_pay += -adj["federal_effect"]
            else:
                ca_pay += -adj["california_effect"]
        else:
            fed_tax += adj["federal_effect"] or 0
            ca_tax += adj["california_effect"] or 0
    adjusted["federal"]["total_tax"] = round(base["federal"]["total_tax"] + fed_tax)
    adjusted["federal"]["payments"] = round(base["federal"]["payments"] + fed_pay)
    # The engine's balance_due = total_tax - payments (the penalty is shown separately)
    adjusted["federal"]["balance_due"] = adjusted["federal"]["total_tax"] - adjusted["federal"]["payments"]
    adjusted["california"]["total_tax"] = round(base["california"]["total_tax"] + ca_tax)
    adjusted["california"]["payments"] = round(base["california"]["payments"] + ca_pay)
    adjusted["california"]["balance_due"] = adjusted["california"]["total_tax"] - adjusted["california"]["payments"]
    return {"version": version, "engine": base, "adjusted": adjusted,
            "note": "Adjusted figures are estimates: change x tax_effect_rates. The engine itself is not re-run."}


# ---------------------------------------------------------------------------
# Filing gates: can we file yet?
# ---------------------------------------------------------------------------
def filing_blockers(issues):
    """
    Returns a list of plain-English reasons we cannot file yet.
    An empty list means the return is ready to file.
    `issues` is the output of checks.run_all_checks().
    """
    blockers = []

    # 1. Big fixes still waiting for the partner
    pending = query("SELECT id, target_id FROM adjustments WHERE status='pending_partner'")
    for a in pending:
        blockers.append(f"Adjustment #{a['id']} on {a['target_id']} needs partner approval")

    # 2. Errors found by the checks that nobody handled yet
    for issue in issues:
        if issue["severity"] == "error" and issue["status"] == "open":
            blockers.append(f"Unresolved check: {issue['title']}")

    # 3. All three sign-offs on the whole return, and none of them reopened
    status = signoff_status("return", "2025")
    for level in ("preparer", "reviewer", "partner"):
        s = status.get(level)
        if not s:
            blockers.append(f"Missing {level} sign-off on the whole return")
        elif s["state"] == "reopened":
            blockers.append(f"{level.capitalize()} sign-off was REOPENED because numbers changed after signing")

    return blockers


def return_status():
    """in_preparation | filed - stored in settings."""
    return get_setting("return_status", "in_preparation")


def is_frozen():
    """After filing the return is frozen: no more changes."""
    return return_status() == "filed"
