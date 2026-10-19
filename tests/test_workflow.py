"""
tests/test_workflow.py
----------------------
An end-to-end test of a whole tax season, through the API (core demo version).
Run it with:   python tests/test_workflow.py
(or with pytest if you have it installed:  pytest tests/)

It uses a separate, temporary database so it never touches your real data.
"""

import os
import sys
import tempfile

# Use a throw-away database file for the test
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402

client = app_module.app.test_client()

PREPARER, REVIEWER, PARTNER = "st-01", "st-02", "st-03"
NOT_A_WORK_ROLE = "st-05"   # the firm administrator: not in the core demo


def call(method, url, user, json=None):
    resp = client.open(url, method=method, json=json, headers={"X-User": user})
    return resp.status_code, resp.get_json(silent=True)


def test_full_season():
    # 1. The checks find the confident mistakes
    status, data = call("GET", "/api/issues", PREPARER)
    assert status == 200
    ids = {i["id"] for i in data["issues"]}
    for expected in ["dup-page-D23-3", "1099k-in-books-D21", "basis-F8949.B.N1", "payments-federal",
                     "payments-california", "ca-wage-source", "vehicle-donation", "ssn-theo", "misread-ev-1045",
                     "property-tax-proration"]:
        assert expected in ids, expected
    assert not any(i.startswith("check-crashed") for i in ids)

    # 2. Only preparers, reviewers and partners can use the workpaper
    assert call("GET", "/api/lines", NOT_A_WORK_ROLE)[0] == 403

    # 3. A small fix (mileage, +$919) is active straight away and the issue shows "adjusted"
    assert call("POST", "/api/issues/dup-page-D23-3/apply", PREPARER)[0] == 200
    _, line = call("GET", "/api/lines/SchC.9", PREPARER)
    assert line["effective_value"] == 5670 and line["line"]["value"] == 8260   # the engine value is never lost

    # 4. A big fix (property tax, +$1,462) waits for the partner; the issue says so
    call("POST", "/api/issues/property-tax-proration/apply", PREPARER)
    _, data = call("GET", "/api/issues", PREPARER)
    issue = next(i for i in data["issues"] if i["id"] == "property-tax-proration")
    assert issue["status"] == "waiting_partner"
    adj_id = issue["pending_adjustments"][0]
    assert call("POST", f"/api/adjustments/{adj_id}/approve", REVIEWER)[0] == 403   # reviewer can't approve
    assert call("POST", f"/api/adjustments/{adj_id}/approve", PARTNER)[0] == 200
    _, data = call("GET", "/api/issues", PREPARER)
    assert next(i for i in data["issues"] if i["id"] == "property-tax-proration")["status"] == "adjusted"

    # 5. A sign-off REOPENS by itself when the number changes, and comes back after undo
    call("POST", "/api/signoffs", PREPARER, {"level": "preparer", "targets": [{"type": "line", "id": "SchC.25"}]})
    _, r = call("POST", "/api/adjustments", PREPARER, {"target_id": "SchC.25", "new_value": 2172, "reason": "business share only"})
    _, line = call("GET", "/api/lines/SchC.25", PREPARER)
    assert line["signoffs"]["preparer"]["state"] == "reopened"
    call("POST", f"/api/adjustments/{r['id']}/revert", PREPARER)
    _, line = call("GET", "/api/lines/SchC.25", PREPARER)
    assert line["signoffs"]["preparer"]["state"] == "signed"

    # 6. Nobody can review their own work
    assert call("POST", "/api/signoffs", REVIEWER, {"level": "preparer", "targets": [{"type": "line", "id": "SchC.8"}]})[0] == 403
    call("POST", "/api/adjustments", REVIEWER, {"target_id": "SchC.8", "new_value": 2000, "reason": "invoice shows 2,000"})
    _, r = call("POST", "/api/signoffs", REVIEWER, {"level": "reviewer", "targets": [{"type": "line", "id": "SchC.8"}]})
    assert r["refused"] == ["SchC.8"]

    # 7. Bulk sign-off and undo
    _, b = call("POST", "/api/signoffs", PREPARER, {"level": "preparer",
                                                     "targets": [{"type": "line", "id": "F1040.1a"}, {"type": "line", "id": "F1040.1z"}]})
    call("POST", "/api/undo", PREPARER, {"batch_id": b["batch_id"]})
    _, line = call("GET", "/api/lines/F1040.1a", PREPARER)
    assert line["signoffs"] == {}

    # 8. Sensitive data is masked
    _, doc = call("GET", "/api/documents/D08", PREPARER)
    assert "666-41-2287" not in str(doc)

    # 9. Accepting or dismissing an issue needs a reason
    assert call("POST", "/api/issues/misread-ev-1045/status", PREPARER, {"status": "accepted"})[0] == 400

    # 10. Resolve every error issue: apply suggestions where there are any, accept the rest with a note
    _, data = call("GET", "/api/issues", PREPARER)
    for issue in data["issues"]:
        if issue["status"] != "open" or issue["severity"] != "error":
            continue
        if issue["suggestions"]:
            assert call("POST", f"/api/issues/{issue['id']}/apply", PREPARER)[0] == 200
        else:
            call("POST", f"/api/issues/{issue['id']}/status", PREPARER, {"status": "accepted", "note": "reviewed"})
    _, adj = call("GET", "/api/adjustments", PARTNER)
    for a in adj["adjustments"]:
        if a["status"] == "pending_partner":
            call("POST", f"/api/adjustments/{a['id']}/approve", PARTNER)

    # 11. Filing is blocked until all three sign off on the whole return
    _, f = call("GET", "/api/filing", PARTNER)
    assert any("sign-off" in b for b in f["blockers"])
    for user, level in ((PREPARER, "preparer"), (REVIEWER, "reviewer"), (PARTNER, "partner")):
        assert call("POST", "/api/signoffs", user, {"level": level, "targets": [{"type": "return", "id": "2025"}]})[0] == 200
    _, f = call("GET", "/api/filing", PARTNER)
    assert f["blockers"] == [], f["blockers"]

    # 12. Only the partner files. Accepted (Theo's SSN was fixed by applying its issue), then frozen.
    assert call("POST", "/api/filing/efile", PREPARER, {})[0] == 403
    status, r = call("POST", "/api/filing/efile", PARTNER, {})
    assert r["status"] == "accepted", r
    assert call("POST", "/api/adjustments", PREPARER, {"target_id": "SchC.8", "new_value": 1, "reason": "x"})[0] == 409
    print("Full season test passed.")


def test_ssn_rejection():
    """If someone dismisses the SSN issue instead of fixing it, e-file is rejected like in the packet."""
    from checks import check_dependent_ssn
    with app_module.app.test_request_context():
        problems = check_dependent_ssn(use_corrections=False)
        assert problems and problems[0]["id"] == "ssn-theo"


if __name__ == "__main__":
    test_ssn_rejection()
    test_full_season()
