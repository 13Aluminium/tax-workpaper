"""
app.py
------
The web server. It does two things:
  1. Serves the single web page (templates/index.html + static/app.js).
  2. Offers a JSON API that the page calls (all URLs start with /api/).

Core demo version: 5 tabs (Overview, Issues, Return lines, Documents, Filing)
and 3 roles (preparer, reviewer, partner).

How "logging in" works in this prototype:
  The page has a ROLE SWITCHER (a dropdown of the firm's staff). The chosen
  user id is sent with every request in the header "X-User". A real product
  would use proper login (SSO / passwords); the permission checks below
  would stay exactly the same.

Every route follows the same 3 steps:  check permission -> call logic -> return JSON.
The real thinking lives in workpaper.py (values, sign-offs, tax effect,
filing gate) and checks.py (finding the mistakes).

Run it:   pip install -r requirements.txt   then   python app.py
"""

import json
import os
import uuid
from functools import wraps

from flask import Flask, jsonify, request, render_template, abort, Response

import checks
from database import init_db, close_db, query, execute, log_action, now_iso, set_setting
from packet import get_packet, FORM_TO_AREA, guess_tax_effect_category
from permissions import can, mask_text, mask_value, SENSITIVE_VALUE_TYPES, WORK_ROLES
from workpaper import (effective_line_value, latest_adjustment_by_target, active_adjustments,
                       fingerprint, signoff_status, estimate_tax_effect, adjusted_summary,
                       filing_blockers, return_status, effective_household)

app = Flask(__name__)
app.teardown_appcontext(close_db)

# Firm rule: a fix whose estimated tax effect is over this amount needs a partner.
PARTNER_THRESHOLD = 1000


# ===========================================================================
# Helpers
# ===========================================================================
def staff():
    """The people who can use the workpaper: preparers, reviewers, partners."""
    return [u for u in get_packet().staff_and_clients() if u["role"] in WORK_ROLES]


def current_user():
    """Read the X-User header and look the user up in the staff list."""
    user_id = request.headers.get("X-User") or "st-01"
    for u in staff():
        if u["id"] == user_id:
            return u
    return None


def require(action):
    """
    Decorator: put @require("adjust") above a route and only users whose
    role allows "adjust" can call it. Everyone else gets HTTP 403.
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = current_user()
            if not can(user, action):
                return jsonify({"error": f"Your role ({user['role'] if user else 'unknown'}) cannot do '{action}'."}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def user_name(user_id):
    for u in staff():
        if u["id"] == user_id:
            return u["name"]
    return user_id


def frozen_error():
    """After filing, nothing may change."""
    if return_status() == "filed":
        return jsonify({"error": "The return is filed and frozen. No more changes."}), 409
    return None


def serialize_value(ev, version=None):
    """An extracted value, masked if sensitive, plus the return lines that use it."""
    packet = get_packet()
    version = version or packet.latest_version
    sensitive = ev["value_type"] in SENSITIVE_VALUE_TYPES or "pin" in ev["field"].lower()
    return {
        **ev,
        "value": mask_value(ev["value"], "ssn") if sensitive else ev["value"],
        "text_as_read": mask_value(ev["text_as_read"], "ssn") if sensitive else ev["text_as_read"],
        "sensitive": sensitive,
        "used_by": packet.value_used_by[version].get(ev["id"], []),
    }


# ===========================================================================
# Page + bootstrap (everything the page needs to start)
# ===========================================================================
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/bootstrap")
def bootstrap():
    packet = get_packet()
    user = current_user()
    all_actions = ["view_workpaper", "adjust", "bulk", "update_issue", "signoff_preparer",
                   "signoff_reviewer", "signoff_partner", "approve_adjustment", "file_return"]
    return jsonify({
        "household_name": packet.raw["packet"]["name"],
        "users": staff(),
        "me": user,
        "permissions": [a for a in all_actions if can(user, a)],
        "latest_version": packet.latest_version,
        "forms": sorted({l["form"] for l in packet.lines[packet.latest_version].values()}),
        "tax_effect_categories": list(packet.versions[packet.latest_version]["tax_effect_rates"].keys())
                                 + ["schc_expense", "rental_expense", "federal_payment", "california_payment",
                                    "federal_credit", "california_credit", "none"],
        "return_status": return_status(),
    })


# ===========================================================================
# Overview
# ===========================================================================
@app.route("/api/summary")
@require("view_workpaper")
def summary():
    packet = get_packet()
    issues = checks.run_all_checks()
    todo = [i for i in issues if i["status"] in ("open", "waiting_partner")]
    return jsonify({
        "summary": adjusted_summary(),
        "status": return_status(),
        "blockers": filing_blockers(issues),
        "issue_counts": {
            "error": sum(1 for i in todo if i["severity"] == "error"),
            "warning": sum(1 for i in todo if i["severity"] == "warning"),
            "info": sum(1 for i in todo if i["severity"] == "info"),
            "resolved": len(issues) - len(todo),
        },
        "return_signoffs": signoff_status("return", "2025"),
        "adjustment_count": len(active_adjustments()),
        "household": {"filing_status": packet.raw["household"]["filing_status"],
                      "residency": packet.raw["household"]["residency"],
                      "people": [{"id": p["id"], "name": p["name"], "role": p["role"]} for p in effective_household()]},
    })


# ===========================================================================
# Return lines
# ===========================================================================
@app.route("/api/lines")
@require("view_workpaper")
def list_lines():
    packet = get_packet()
    version = packet.latest_version
    q = (request.args.get("q") or "").lower()
    form = request.args.get("form")
    only = request.args.get("only")  # flagged | adjusted
    adj_map = latest_adjustment_by_target("line")
    # Which lines are touched by an open check? Show a badge on them.
    flagged = {}
    for issue in checks.run_all_checks():
        if issue["status"] == "open":
            for key in issue["lines"]:
                flagged.setdefault(key, []).append(issue["severity"])
    rows = []
    for line in packet.versions[version]["lines"]:
        key = line["key"]
        if form and line["form"] != form:
            continue
        if q and q not in key.lower() and q not in line["label"].lower():
            continue
        row = {
            "key": key, "form": line["form"], "label": line["label"],
            "engine_value": line["value"],
            "value": adj_map[key]["new_value"] if key in adj_map else line["value"],
            "adjusted": key in adj_map,
            "pending": key in adj_map and adj_map[key]["status"] == "pending_partner",
            "flags": flagged.get(key, []),
        }
        if only == "adjusted" and not row["adjusted"]:
            continue
        if only == "flagged" and not row["flags"]:
            continue
        rows.append(row)
    # Hide the ~300 individual stock-sale lots (Form 8949) unless asked for; show their totals.
    if not form and not q and not only:
        rows = [r for r in rows if r["form"] != "F8949" or ".total" in r["key"]]
    return jsonify({"version": version, "lines": rows})


@app.route("/api/lines/<path:key>")
@require("view_workpaper")
def line_detail(key):
    packet = get_packet()
    version = packet.latest_version
    line = packet.line(key, version)
    if not line:
        abort(404)
    adj_map = latest_adjustment_by_target("line")
    # How it was built: the lines it uses (inputs) and the lines that use it
    inputs = []
    for inp in line.get("inputs", []):
        l2 = packet.line(inp, version)
        inputs.append({"key": inp, "label": l2["label"] if l2 else "?",
                       "value": effective_line_value(inp, version, adj_map) if l2 else None})
    used_by = [{"key": k, "label": packet.line(k, version)["label"]} for k in packet.line_used_by[version].get(key, [])]
    # Where it came from: the extracted values (ev-...) and other references
    sources = []
    for src in line.get("sources", []):
        info = packet.describe_ref(src, version)
        if src.startswith("ev-") and src in packet.values:
            info["value_detail"] = serialize_value(packet.values[src], version)
        sources.append(info)
    adjustments = query("SELECT * FROM adjustments WHERE target_type='line' AND target_id=? ORDER BY id", (key,))
    for a in adjustments:
        a["old_value"] = json.loads(a["old_value"]) if a["old_value"] else None
        a["new_value"] = json.loads(a["new_value"]) if a["new_value"] else None
        a["created_by_name"] = user_name(a["created_by"])
    return jsonify({
        "line": line,
        "area": FORM_TO_AREA.get(line["form"], "other"),
        "effective_value": effective_line_value(key, version, adj_map),
        "inputs": inputs,
        "used_by": used_by,
        "sources": sources,
        "adjustments": adjustments,
        "signoffs": signoff_status("line", key),
        "suggested_category": guess_tax_effect_category(key),
        "issues": [i for i in checks.run_all_checks() if key in i["lines"]],
        "engine_rule": packet.raw["engine_reference"]["rules"].get(line["rule"]),
    })


# ===========================================================================
# Documents and extracted values (the evidence)
# ===========================================================================
@app.route("/api/documents")
@require("view_workpaper")
def list_documents():
    return jsonify({"documents": checks.document_accounting()})


@app.route("/api/documents/<doc_id>")
@require("view_workpaper")
def document_detail(doc_id):
    """
    The SIMULATED source document. There are no real PDFs in the packet, so
    each page is built from printed_content (the truth) and the extracted
    values are drawn as boxes at their recorded positions.
    """
    packet = get_packet()
    doc = packet.documents.get(doc_id)
    if not doc:
        abort(404)
    version = packet.latest_version
    pages = []
    for p in doc["pages"]:
        fields = [serialize_value(v, version) for v in packet.values_by_doc[doc_id] if v["page"] == p["page"]]
        pages.append({
            "page": p["page"],
            "render": {"kind": "simulated_text", "text": mask_text(p["printed_content"])},
            "fields": fields,
        })
    accounting = next((d for d in checks.document_accounting() if d["id"] == doc_id), None)
    return jsonify({
        "document": {k: v for k, v in doc.items() if k != "pages"},
        "pages": pages,
        "accounting": accounting,
        "lines": packet.doc_used_by[version].get(doc_id, []),
        "in_versions": [v for v in packet.version_order if doc_id in packet.versions[v]["documents_used"]],
    })


@app.route("/api/values/<ev_id>")
@require("view_workpaper")
def value_detail(ev_id):
    """Used when someone clicks an ev-... link: tells the page which document to open."""
    packet = get_packet()
    ev = packet.values.get(ev_id)
    if not ev:
        abort(404)
    return jsonify({"value": serialize_value(ev)})


@app.route("/api/client-ref/<ref>")
@require("view_workpaper")
def client_ref(ref):
    """Open other evidence (client answers qa-.., messages msg-.., IRS records agency:/..) as text."""
    packet = get_packet()
    info = packet.describe_ref(ref)
    if ref in packet.questions:
        info["value"] = packet.questions[ref]
    elif ref in packet.messages:
        info["value"] = packet.messages[ref]
    elif ref in packet.calls:
        info["value"] = packet.calls[ref]
    return Response(mask_text(json.dumps(info, indent=2, default=str)), mimetype="application/json")


@app.route("/api/search")
@require("view_workpaper")
def search():
    """Search box in the top bar: lines, documents and extracted values."""
    q = (request.args.get("q") or "").lower().strip()
    if len(q) < 2:
        return jsonify({"results": []})
    packet = get_packet()
    results = []
    for key, line in packet.lines[packet.latest_version].items():
        if q in key.lower() or q in line["label"].lower():
            results.append({"kind": "line", "id": key, "text": f"{key} {line['label']} = {line['value']}"})
    for doc in packet.raw["documents"]:
        text = " ".join(p["printed_content"] for p in doc["pages"])
        if q in doc["title"].lower() or q in text.lower():
            results.append({"kind": "document", "id": doc["id"], "text": f"{doc['id']} {doc['title']}"})
    for ev in packet.raw["extracted_values"]:
        if q in ev["label"].lower() or q == str(ev["value"]).lower() or q in (ev["text_as_read"] or "").lower():
            results.append({"kind": "value", "id": ev["id"],
                            "text": mask_text(f"{ev['id']} {ev['label']} = {ev['text_as_read']} ({ev['document_id']})")})
    return jsonify({"results": results[:200]})


# ===========================================================================
# Adjustments (change a number without losing the original)
# ===========================================================================
def _parse_number(value):
    """Accept numbers typed with commas: '1,234' -> 1234. Leave text alone."""
    if isinstance(value, (int, float)):
        return value
    try:
        cleaned = str(value).replace(",", "").strip()
        return float(cleaned) if "." in cleaned else int(cleaned)
    except ValueError:
        return value


def create_adjustment(user, target_type, target_id, new_value, reason, category=None, issue_id=None, batch_id=None,
                      effect_override=None):
    """
    Save one adjustment. effect_override=(federal, california) lets a check pass
    a better estimate than "change x rate" (e.g. the engine's own what-if result).
    """
    packet = get_packet()
    if target_type == "line":
        if not packet.line(target_id):
            raise ValueError(f"No line {target_id}")
        old_value = effective_line_value(target_id)
    else:  # household field e.g. theo.ssn
        person_id, field = target_id.split(".", 1)
        old_value = next(p.get(field) for p in effective_household() if p["id"] == person_id)
    new_value = _parse_number(new_value)
    if category is None:
        category = guess_tax_effect_category(target_id) if target_type == "line" else "none"
    # Estimated tax effect = change x rate (the engine can't be re-run)
    fed = ca = 0.0
    if isinstance(old_value, (int, float)) and isinstance(new_value, (int, float)):
        fed, ca = estimate_tax_effect(new_value - old_value, category)
    if effect_override is not None:
        fed, ca = effect_override
    # Firm rule: tax effect over 1,000 needs partner approval (unless a partner made it)
    status = "active"
    if (abs(fed) > PARTNER_THRESHOLD or abs(ca) > PARTNER_THRESHOLD) and user["role"] != "partner":
        status = "pending_partner"
    adj_id = execute(
        "INSERT INTO adjustments (target_type, target_id, version, old_value, new_value, reason, category, "
        "federal_effect, california_effect, status, issue_id, batch_id, created_by, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (target_type, target_id, packet.latest_version, json.dumps(old_value), json.dumps(new_value), reason,
         category, fed, ca, status, issue_id, batch_id, user["id"], now_iso()))
    log_action(user["id"], "adjust", target_type, target_id,
               {"old": old_value, "new": new_value, "reason": reason, "federal_effect": fed, "california_effect": ca})
    return adj_id


@app.route("/api/adjustments", methods=["GET"])
@require("view_workpaper")
def list_adjustments():
    rows = query("SELECT * FROM adjustments ORDER BY id DESC")
    for r in rows:
        r["old_value"] = json.loads(r["old_value"]) if r["old_value"] else None
        r["new_value"] = json.loads(r["new_value"]) if r["new_value"] else None
        r["created_by_name"] = user_name(r["created_by"])
        r["approved_by_name"] = user_name(r["approved_by"]) if r["approved_by"] else None
    return jsonify({"adjustments": rows})


@app.route("/api/adjustments", methods=["POST"])
@require("adjust")
def post_adjustment():
    err = frozen_error()
    if err:
        return err
    data = request.json or {}
    if not data.get("reason"):
        return jsonify({"error": "A reason is required for every adjustment."}), 400
    try:
        adj_id = create_adjustment(current_user(), data.get("target_type", "line"), data["target_id"],
                                   data["new_value"], data["reason"], data.get("category"), data.get("issue_id"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"id": adj_id})


@app.route("/api/adjustments/<int:adj_id>/revert", methods=["POST"])
@require("adjust")
def revert_adjustment(adj_id):
    err = frozen_error()
    if err:
        return err
    execute("UPDATE adjustments SET status='reverted' WHERE id=?", (adj_id,))
    log_action(current_user()["id"], "revert_adjustment", "adjustment", str(adj_id))
    return jsonify({"ok": True})


@app.route("/api/adjustments/<int:adj_id>/approve", methods=["POST"])
@require("approve_adjustment")
def approve_adjustment(adj_id):
    execute("UPDATE adjustments SET status='active', approved_by=?, approved_at=? WHERE id=? AND status='pending_partner'",
            (current_user()["id"], now_iso(), adj_id))
    log_action(current_user()["id"], "approve_adjustment", "adjustment", str(adj_id))
    return jsonify({"ok": True})


# ===========================================================================
# Sign-offs (one line, many lines, or the whole return) + undo
# ===========================================================================
@app.route("/api/signoffs", methods=["POST"])
def post_signoffs():
    data = request.json or {}
    user = current_user()
    level = data.get("level")
    allowed = {"preparer": "signoff_preparer", "reviewer": "signoff_reviewer", "partner": "signoff_partner"}
    if level not in allowed or not can(user, allowed[level]):
        return jsonify({"error": f"Your role cannot sign off at level '{level}'."}), 403
    targets = data.get("targets", [])
    if len(targets) > 1 and not can(user, "bulk"):
        return jsonify({"error": "Bulk actions not allowed for your role"}), 403
    batch_id = uuid.uuid4().hex[:10]
    refused = []
    for t in targets:
        # Firm rule: you may not review or sign off on work you prepared.
        if level in ("reviewer", "partner"):
            # "Prepared" = signed it as preparer, or changed the number yourself.
            prepared = query("SELECT 1 FROM signoffs WHERE target_type=? AND target_id=? AND level='preparer' "
                             "AND created_by=? AND status='active'", (t["type"], t["id"], user["id"]), one=True)
            if t["type"] == "line" and not prepared:
                prepared = query("SELECT 1 FROM adjustments WHERE target_type='line' AND target_id=? AND created_by=? "
                                 "AND status!='reverted'", (t["id"], user["id"]), one=True)
            if prepared:
                refused.append(t["id"])
                continue
        # Save a fingerprint of what was signed: if it changes later, the sign-off REOPENS
        execute("INSERT INTO signoffs (target_type, target_id, level, fingerprint, batch_id, created_by, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (t["type"], t["id"], level, fingerprint(t["type"], t["id"]), batch_id, user["id"], now_iso()))
    log_action(user["id"], "signoff", "batch", batch_id, {"level": level, "count": len(targets) - len(refused)})
    return jsonify({"batch_id": batch_id, "refused": refused})


@app.route("/api/undo", methods=["POST"])
def undo_batch():
    """Undo a bulk action: everything with this batch id is marked undone (not deleted)."""
    err = frozen_error()
    if err:
        return err
    batch_id = (request.json or {}).get("batch_id")
    user = current_user()
    execute("UPDATE signoffs SET status='undone' WHERE batch_id=? AND created_by=?", (batch_id, user["id"]))
    execute("UPDATE adjustments SET status='reverted' WHERE batch_id=? AND created_by=?", (batch_id, user["id"]))
    log_action(user["id"], "undo_batch", "batch", batch_id)
    return jsonify({"ok": True})


# ===========================================================================
# Issues (the independent checks that find the mistakes)
# ===========================================================================
@app.route("/api/issues")
@require("view_workpaper")
def list_issues():
    return jsonify({"issues": checks.run_all_checks()})


@app.route("/api/issues/<path:issue_id>/status", methods=["POST"])
@require("update_issue")
def set_issue_status(issue_id):
    """Accept an issue as is, dismiss it, or reopen it. Accept/dismiss need a reason."""
    data = request.json or {}
    if data.get("status") in ("dismissed", "accepted") and not data.get("note"):
        return jsonify({"error": "Explain why (a note is required to accept or dismiss)."}), 400
    if data.get("status") == "open":
        execute("DELETE FROM issue_status WHERE issue_id=?", (issue_id,))
    else:
        execute("INSERT INTO issue_status (issue_id, status, note, updated_by, updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(issue_id) DO UPDATE SET status=excluded.status, note=excluded.note, "
                "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (issue_id, data["status"], data.get("note"), current_user()["id"], now_iso()))
    log_action(current_user()["id"], "issue_status", "issue", issue_id, data)
    return jsonify({"ok": True})


@app.route("/api/issues/<path:issue_id>/apply", methods=["POST"])
@require("adjust")
def apply_issue(issue_id):
    """Apply all suggested adjustments of one issue in a single batch (undoable)."""
    err = frozen_error()
    if err:
        return err
    issue = next((i for i in checks.run_all_checks() if i["id"] == issue_id), None)
    if not issue or not issue["suggestions"]:
        return jsonify({"error": "Nothing to apply"}), 400
    batch_id = uuid.uuid4().hex[:10]
    user = current_user()
    for s in issue["suggestions"]:
        override = (s["federal_effect"], s["california_effect"]) if s.get("use_effect") else None
        create_adjustment(user, s["target_type"], s["target_id"], s["new_value"], s["reason"],
                          s.get("category"), issue_id, batch_id, override)
    return jsonify({"batch_id": batch_id})


# ===========================================================================
# Filing
# ===========================================================================
def snapshot():
    """A frozen copy of everything that matters at filing time."""
    packet = get_packet()
    adj_map = latest_adjustment_by_target("line")
    return {
        "taken_at": now_iso(),
        "engine_version": packet.latest_version,
        "lines": {k: effective_line_value(k, packet.latest_version, adj_map) for k in packet.lines[packet.latest_version]},
        "summary": adjusted_summary()["adjusted"],
        "household": effective_household(),
        "adjustment_ids": [a["id"] for a in active_adjustments()],
    }


@app.route("/api/filing")
@require("view_workpaper")
def filing_state():
    rows = query("SELECT id, kind, jurisdiction, status, detail, created_by, created_at FROM filings ORDER BY id")
    for r in rows:
        r["created_by_name"] = user_name(r["created_by"])
    return jsonify({"status": return_status(), "filings": rows,
                    "blockers": filing_blockers(checks.run_all_checks())})


@app.route("/api/filing/efile", methods=["POST"])
@require("file_return")
def efile():
    """
    Simulated e-file. We check the return like the IRS e-file system would:
    a dependent's SSN must match the documents. If not, the submission is
    REJECTED with the same error code the packet shows (R0000-504-02).
    """
    if return_status() == "filed":
        return jsonify({"error": "Already filed."}), 409
    blockers = filing_blockers(checks.run_all_checks())
    if blockers:
        return jsonify({"error": "Cannot file yet", "blockers": blockers}), 409
    user = current_user()
    # Agency-side validation (re-run the SSN check even if someone dismissed the issue)
    if checks.check_dependent_ssn(use_corrections=True):
        packet_reject = get_packet().raw["tax_agency_records"]["efile"][0]
        detail = (f"REJECTED {packet_reject['errors'][0]['code']}: {packet_reject['errors'][0]['message']} "
                  "(California held: federal rejected)")
        execute("INSERT INTO filings (kind, jurisdiction, status, detail, snapshot, created_by, created_at) "
                "VALUES (?,?,?,?,?,?,?)", ("original", "federal", "rejected", detail, None, user["id"], now_iso()))
        log_action(user["id"], "efile_rejected", "filing", "original", detail)
        return jsonify({"status": "rejected", "detail": detail})
    execute("INSERT INTO filings (kind, jurisdiction, status, detail, snapshot, created_by, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("original", "both", "accepted", "Federal and California accepted", json.dumps(snapshot()),
             user["id"], now_iso()))
    set_setting("return_status", "filed")
    log_action(user["id"], "efile_accepted", "filing", "original")
    return jsonify({"status": "accepted"})


# ===========================================================================
# Start
# ===========================================================================
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("DEBUG") == "1")
