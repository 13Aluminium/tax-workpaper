"""
checks.py
---------
Independent checks that DO NOT TRUST THE ENGINE.

The brief says: "Run checks that don't trust the engine" and "the packet
contains confident mistakes". The engine already gives us some warnings
(diagnostics), but it can't warn about its own blind spots. So these checks
go back to the raw evidence (the printed page, the client's answers, the
IRS transcript...) and compare it with what the engine did.

Every check returns zero or more ISSUES. An issue is a plain dict:
{
  "id":        stable id, e.g. "dup-page-D23-3" (so its status can be saved),
  "title":     one line,
  "severity":  "error" (blocks filing until resolved) | "warning" | "info",
  "area":      review area (business, california, ...),
  "explanation": plain English: what we found and why it matters,
  "evidence":  list of refs (ev-..., D.., qa-.., agency:/..) the UI can open,
  "lines":     return line keys affected,
  "suggestions": list of proposed adjustments the user can apply in one click,
  "effect":    {"federal": x, "california": y} estimated change in balance due,
  "source":    "independent" (ours) or "engine" (the engine's diagnostics),
}

A note on honesty: some checks are fully generic (e.g. "is the value the AI
read actually printed on the page?"). Others are rule-style checks written for
patterns that appear in this packet (e.g. "a 1099-K amount that the books
already include"). In a real product each of those becomes a reusable rule.
"""

import re

from database import query
from packet import get_packet, guess_tax_effect_category
from workpaper import estimate_tax_effect, effective_line_value, latest_adjustment_by_target, effective_household


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def money(text):
    """'1,234.50' -> 1234.5"""
    return float(text.replace(",", ""))


def numbers_on_page(text):
    """All numbers printed on a page, as floats."""
    found = set()
    for n in re.findall(r"-?\d[\d,]*\.?\d*", text):
        try:
            found.add(abs(money(n.rstrip("."))))
        except ValueError:
            pass
    return found


def dates_on_page(text):
    """All dates printed on a page, normalised to YYYY-MM-DD."""
    found = set(re.findall(r"\d{4}-\d{2}-\d{2}", text))
    for m, d, y in re.findall(r"(\d{1,2})/(\d{1,2})/(\d{4})", text):
        found.add(f"{y}-{int(m):02d}-{int(d):02d}")
    return found


def make_issue(issue_id, title, severity, area, explanation, evidence=None, lines=None,
               suggestions=None, effect=None, source="independent"):
    return {
        "id": issue_id, "title": title, "severity": severity, "area": area,
        "explanation": explanation, "evidence": evidence or [], "lines": lines or [],
        "suggestions": suggestions or [], "effect": effect or {"federal": 0, "california": 0},
        "source": source,
    }


def suggestion(line_key, new_value, reason, category=None, effect=None):
    """
    A one-click adjustment the user can apply from the Issues screen.
    effect=(federal, california) replaces the simple "change x rate" estimate
    when we know better (for example from the engine's own what-if result).
    """
    packet = get_packet()
    line = packet.line(line_key)
    old = line["value"] if line else None
    if category is None:
        category = guess_tax_effect_category(line_key)
    fed, ca = (0, 0)
    if isinstance(old, (int, float)) and isinstance(new_value, (int, float)):
        fed, ca = estimate_tax_effect(new_value - old, category)
    if effect is not None:
        fed, ca = effect
    return {"target_type": "line", "target_id": line_key, "old_value": old, "new_value": new_value,
            "category": category, "reason": reason, "federal_effect": fed, "california_effect": ca,
            "use_effect": effect is not None}


def total_effect(suggestions):
    return {"federal": round(sum(s["federal_effect"] for s in suggestions), 2),
            "california": round(sum(s["california_effect"] for s in suggestions), 2)}


def docs_of_type(doc_type):
    return [d for d in get_packet().raw["documents"] if d["document_type"] == doc_type]


def full_text(doc):
    return " ".join(p["printed_content"] for p in doc["pages"])


# ---------------------------------------------------------------------------
# CHECK 1 (generic): is what the AI read actually printed on the page?
# ---------------------------------------------------------------------------
def check_values_against_pages():
    packet = get_packet()
    issues = []
    for ev in packet.raw["extracted_values"]:
        page = packet.page_text(ev["document_id"], ev["page"])
        if page is None:
            continue
        value, vtype = ev["value"], ev["value_type"]
        problem = None
        if vtype in ("money", "integer", "quantity") and isinstance(value, (int, float)) and not isinstance(value, bool):
            if abs(value) not in numbers_on_page(page) and value != 0:
                problem = f"the number {value:,} does not appear on page {ev['page']}"
        elif vtype == "date" and value:
            if value not in dates_on_page(page):
                problem = f"the date {value} does not appear on page {ev['page']}"
        elif vtype in ("ein", "ssn", "account_number") and value:
            if str(value) not in page:
                problem = f"the identifier {value} is not printed on page {ev['page']}"
        if problem:
            used = packet.value_used_by[packet.latest_version].get(ev["id"], [])
            # Did the client say anything about this document's issuer? (e.g. a phone confirmation)
            generic = {"parish", "center", "learning", "services", "company", "statement", "client"}
            issuer_words = [w.strip(".,").lower() for w in packet.documents[ev["document_id"]]["issuer"].split()
                            if len(w) >= 5 and w.strip(".,").lower() not in generic]
            related = [m["id"] for m in packet.raw["client"]["messages"]
                       if any(w in m["text"].lower() for w in issuer_words)]
            if vtype == "ein":
                printed = re.findall(r"\b\d{2}-\d{7}\b", page)
                problem += f" (printed: {', '.join(printed)})"
            issues.append(make_issue(
                f"misread-{ev['id']}",
                f"AI read '{ev['text_as_read']}' for {ev['label']} ({ev['document_id']}) but {problem}",
                "error" if used else "warning",
                "intake",
                f"The pipeline was {int(ev['confidence'] * 100)}% confident, but the printed page says otherwise. "
                f"Printed page: \"{page[:260]}\" Used by: {', '.join(used) or 'no return line'}."
                + (f" Related client message(s): {', '.join(related)}." if related else ""),
                evidence=[ev["id"], ev["document_id"]] + related, lines=used))
        # Low confidence reads are worth a human look even if they match.
        elif ev["confidence"] < 0.6:
            used = packet.value_used_by[packet.latest_version].get(ev["id"], [])
            issues.append(make_issue(
                f"lowconf-{ev['id']}",
                f"Low-confidence read ({int(ev['confidence'] * 100)}%): {ev['label']} = {ev['text_as_read']} ({ev['document_id']})",
                "warning", "intake",
                "Verify against the document or a second source (bank statement, client confirmation). "
                "Page text: " + page[:300],
                evidence=[ev["id"], ev["document_id"]], lines=used))
    return issues


# ---------------------------------------------------------------------------
# CHECK 2 (generic): the same page scanned twice inside one document
# ---------------------------------------------------------------------------
def _normalise_page(text):
    # Remove a leading "(Second scan ...)" style remark, then compare.
    return re.sub(r"^\(.*?\)\s*", "", text).strip()


def check_duplicate_pages():
    packet = get_packet()
    params = packet.raw["engine_reference"]["parameters"]
    issues = []
    for doc in packet.raw["documents"]:
        seen = {}
        for p in doc["pages"]:
            norm = _normalise_page(p["printed_content"])
            if len(norm) < 80:          # skip tiny boilerplate pages
                continue
            if norm in seen:
                first = seen[norm]
                dup_values = [v for v in packet.values_by_doc[doc["id"]] if v["page"] == p["page"]]
                dup_ids = {v["id"] for v in dup_values}
                suggestions = []
                affected = set()
                # Any line that sums values from the duplicate page is overstated.
                for key, line in packet.lines[packet.latest_version].items():
                    hit = [s for s in line.get("sources", []) if s in dup_ids]
                    if hit and isinstance(line["value"], (int, float)):
                        overstated = sum(packet.values[s]["value"] for s in hit
                                         if isinstance(packet.values[s]["value"], (int, float)))
                        new_value = line["value"] - overstated
                        affected.add(key)
                        suggestions.append(suggestion(key, new_value,
                                                      f"Remove duplicate page {p['page']} of {doc['id']} (same as page {first})"))
                        # Downstream: standard mileage deduction = miles x rate
                        for down in packet.line_used_by[packet.latest_version].get(key, []):
                            dline = packet.line(down)
                            if dline["rule"] == "R-STANDARD-MILEAGE":
                                affected.add(down)
                                suggestions.append(suggestion(
                                    down, round(new_value * params["mileage_rate"]),
                                    f"Recompute: {new_value:,} miles x {params['mileage_rate']} per mile"))
                issues.append(make_issue(
                    f"dup-page-{doc['id']}-{p['page']}",
                    f"{doc['id']} page {p['page']} is a second scan of page {first} and was counted twice",
                    "error" if affected else "warning",
                    "business" if affected else "intake",
                    f"'{doc['title']}': page {p['page']} has the same content as page {first}. "
                    f"Values read from it ({len(dup_values)}) were added again by the engine.",
                    evidence=[doc["id"]] + sorted(dup_ids)[:6], lines=sorted(affected),
                    suggestions=suggestions, effect=total_effect(suggestions)))
            else:
                seen[norm] = p["page"]
    return issues


# ---------------------------------------------------------------------------
# CHECK 3: same interest reported by two documents for the same account
# ---------------------------------------------------------------------------
def check_duplicate_interest():
    packet = get_packet()
    lines = packet.lines[packet.latest_version]
    interest_lines = [l for k, l in lines.items() if k.startswith("SchB.1.") and isinstance(l["value"], (int, float))]
    issues = []
    for i, a in enumerate(interest_lines):
        for b in interest_lines[i + 1:]:
            if a["value"] != b["value"] or not a["sources"] or not b["sources"]:
                continue
            doc_a = packet.documents[packet.values[a["sources"][0]]["document_id"]]
            doc_b = packet.documents[packet.values[b["sources"][0]]["document_id"]]
            acct_a = set(re.findall(r"account ending (\d{4})", full_text(doc_a)))
            acct_b = set(re.findall(r"account ending (\d{4})", full_text(doc_b)))
            shared = acct_a & acct_b
            if shared:
                s = suggestion(b["key"], 0, f"Same interest already reported on {a['key']} ({doc_a['id']}); both forms are for account ending {shared.pop()}")
                issues.append(make_issue(
                    f"dup-interest-{a['key']}-{b['key']}", f"Interest of {b['value']:,} counted twice ({a['key']} and {b['key']})",
                    "error", "investments_and_digital_assets",
                    f"{doc_b['id']} ({doc_b['title']}) is the bank-sweep 1099-INT for the same brokerage account as the "
                    f"composite {doc_a['id']}. The composite already includes it. Counting both overstates interest.",
                    evidence=[a["sources"][0], b["sources"][0], doc_a["id"], doc_b["id"]],
                    lines=[a["key"], b["key"], "SchB.2", "F1040.2b"],
                    suggestions=[s], effect=total_effect([s])))
    return issues


# ---------------------------------------------------------------------------
# CHECK 4: Form 1099-K amounts that the books already include
# ---------------------------------------------------------------------------
def check_1099k_already_in_books():
    packet = get_packet()
    issues = []
    books_docs = docs_of_type("books_transaction_detail")
    schc1 = packet.line("SchC.1")
    for k in docs_of_type("1099-K"):
        filer = re.search(r"Filer ([A-Z][a-z]+)", full_text(k))
        amount = re.search(r"Box 1a [^0-9]*([\d,]+\.\d\d)", full_text(k))
        if not filer or not amount:
            continue
        name, amt = filer.group(1), money(amount.group(1))
        for b in books_docs:
            text = full_text(b)
            if name in text and f"{amt:,.2f}" in text:
                s = suggestion("SchC.1", schc1["value"] - amt,
                               f"1099-K ({k['id']}) {amt:,.2f} is already inside books revenue per {b['id']}")
                issues.append(make_issue(
                    f"1099k-in-books-{k['id']}", f"Form 1099-K {amt:,.0f} added on top of books that already include it",
                    "error", "business",
                    f"The engine rule R-SCHC-GROSS adds 1099-K amounts it cannot match to a books deposit. But {b['id']} says "
                    f"the {name} card payments ({amt:,.2f}) were deposited gross and recorded as sales. Gross receipts are "
                    f"double counted. Keep a reconciliation: 1099-NEC + 1099-K vs books (the IRS asks for this in 2028).",
                    evidence=[k["id"], b["id"], "ev-0109", "ev-0110"] , lines=["SchC.1"],
                    suggestions=[s], effect=total_effect([s])))
    return issues


# ---------------------------------------------------------------------------
# CHECK 5: broker pages the pipeline did not read
# ---------------------------------------------------------------------------
def check_unread_pages():
    packet = get_packet()
    issues = []
    for doc in packet.raw["documents"]:
        reason = doc["extraction"].get("reason") or ""
        pages = [int(n) for n in re.findall(r"Page (\d+)", reason)]
        if doc["extraction"]["status"] != "partial" or not pages:
            continue
        if doc["id"] in packet.versions[packet.latest_version]["documents_superseded"]:
            continue  # the corrected version is what matters
        for pg in pages:
            text = packet.page_text(doc["id"], pg) or ""
            lots = re.findall(r"proceeds ([\d,]+\.\d\d) basis ([\d,]+\.\d\d)", text)
            if not lots:
                continue
            gain = sum(money(p) - money(b) for p, b in lots)
            fed, ca = estimate_tax_effect(gain, "stcg")
            issues.append(make_issue(
                f"unread-{doc['id']}-{pg}", f"{doc['id']} page {pg} was never read: {len(lots)} sale(s) missing from the return",
                "error", "investments_and_digital_assets",
                f"Extraction note: '{reason}'. The page says: {text} This explains the summary vs detail difference "
                f"the engine flagged. Net gain/(loss) missing: {gain:,.2f}. Add the lot to Form 8949.",
                evidence=[doc["id"]], lines=["SchD.7"], effect={"federal": fed, "california": ca}))
    return issues


# ---------------------------------------------------------------------------
# CHECK 6: noncovered stock sales with zero basis when the plan statement has the basis
# ---------------------------------------------------------------------------
def check_noncovered_basis():
    packet = get_packet()
    issues = []
    supplements = docs_of_type("stock_plan_supplement")
    # e.g. "Lot RSU-2502: 400 sh acquired 02/15/2025 (vest FMV 47.75) adjusted basis 19,100.00"
    known = {}
    for doc in supplements:
        for shares, acq, basis in re.findall(r"(\d+) sh (?:acquired|purchased) (\d\d/\d\d/\d{4}).*?adjusted basis ([\d,]+\.\d\d)", full_text(doc)):
            m, d, y = acq.split("/")
            known[(int(shares), f"{y}-{m}-{d}")] = (money(basis), doc["id"])
    for key, line in packet.lines[packet.latest_version].items():
        if line["form"] != "F8949" or "basis" not in line["label"]:
            continue
        m = re.search(r"\((?:acquired )?(\d{4}-\d\d-\d\d), sold .*?proceeds ([\d,]+\.\d\d), basis (?:\(not reported\)|0\.00)", line["label"])
        if not m:
            continue
        acq, proceeds = m.group(1), money(m.group(2))
        sh = re.search(r"(\d+) sh", packet.values[line["sources"][0]]["text_as_read"] if line["sources"] else "")
        for (shares, date), (basis, doc_id) in known.items():
            if date == acq and abs(line["value"] - proceeds) < 1:
                s = suggestion(key, round(proceeds - basis, 2),
                               f"Use adjusted basis {basis:,.2f} from {doc_id} (shares already taxed as wages on the W-2)")
                issues.append(make_issue(
                    f"basis-{key}", f"{key}: RSU sale taxed on full proceeds {proceeds:,.0f} (basis treated as zero)",
                    "error", "investments_and_digital_assets",
                    f"The broker left basis blank because the lot is noncovered. {doc_id} gives the adjusted basis "
                    f"{basis:,.2f} (vest value, already in W-2 box 1). Without it the same income is taxed twice. "
                    f"Note: the engine DID use the supplement for the ESPP lot, but not for the RSU lots.",
                    evidence=[doc_id, "ev-0027"] + line["sources"][:1], lines=[key],
                    suggestions=[s], effect=total_effect([s])))
    return issues


# ---------------------------------------------------------------------------
# CHECK 7: estimated payments vs what the IRS and FTB actually received
# ---------------------------------------------------------------------------
def check_payments_vs_agency():
    packet = get_packet()
    issues = []
    agency = packet.raw["tax_agency_records"]
    # Federal: code 670 estimated payments for 2025 (exclude the extension payment)
    first_snapshot = agency["irs_account_transcript_2025"][0]
    irs_est = sum(t["amount"] for t in first_snapshot["transactions"]
                  if t["code"] == "670" and "extension" not in t["description"].lower())
    line = packet.line("F1040.26")
    if line and line["value"] != irs_est:
        s = suggestion("F1040.26", irs_est, "Agree estimated payments to IRS account transcript (code 670)", "federal_payment")
        issues.append(make_issue(
            "payments-federal", f"Federal estimates on return {line['value']:,} vs IRS transcript {irs_est:,}",
            "error", "summary",
            f"The engine used the client's memory for Q4 (qa-41: 'Paid January 14, 2026 - 4,000 I think'). "
            f"The IRS transcript ({first_snapshot['as_of']}) shows 3,000 on 2026-01-15. Claiming payments the IRS "
            f"does not have causes a notice.",
            evidence=["qa-41", "D67", "agency:/irs_account_transcript_2025/0"], lines=["F1040.26"],
            suggestions=[s], effect=total_effect([s])))
    # California
    ftb = agency["ftb_account"]
    ftb_2025 = sum(p["amount"] for p in ftb["tax_year_2025_payments"])
    ca_line = packet.line("F540NR.72")
    if ca_line and ca_line["value"] != ftb_2025:
        s = suggestion("F540NR.72", ftb_2025, "Agree California estimates to FTB account", "california_payment")
        held = ftb.get("tax_year_2024_payments_after_filing", [])
        issues.append(make_issue(
            "payments-california", f"California estimates on return {ca_line['value']:,} vs FTB account {ftb_2025:,}",
            "error", "california",
            "The 09/15/2025 Web Pay payment of 3,500 was designated tax year 2024 (see D68) and the FTB is holding it "
            f"as a 2024 credit ({held[0]['note'] if held else ''}). It is not a 2025 payment unless the FTB moves it. "
            "Options: ask the FTB to transfer it, or claim it on 2024.",
            evidence=["D68", "agency:/ftb_account"], lines=["F540NR.72"], suggestions=[s], effect=total_effect([s])))
    return issues


# ---------------------------------------------------------------------------
# CHECK 8: California wages after the move to Texas
# ---------------------------------------------------------------------------
def check_state_wage_sourcing():
    packet = get_packet()
    issues = []
    residency = packet.raw["household"]["residency"]
    ca_end = next((r["to"] for r in residency if r["state"] == "CA"), None)
    for stub in docs_of_type("paystub"):
        text = full_text(stub)
        moved = re.search(r"Work location changed to ([^,]+, [A-Z]{2}) effective (\d\d/\d\d/\d{4})", text)
        ca_ytd = re.search(r"CA taxable wages ([\d,]+\.\d\d)", text)
        if not moved or not ca_ytd:
            continue
        employer = stub["title"].split(" paystub")[0].split(" - ")[0]
        w2 = next((d for d in docs_of_type("W-2") if employer.split()[0] in d["title"]), None)
        if not w2:
            continue
        box16 = next((v for v in packet.values_by_doc[w2["id"]] if v["field"] == "box16"), None)
        if not box16:
            continue
        overstated = box16["value"] - money(ca_ytd.group(1))
        line = packet.line("SchCA540NR.1.E")
        s = suggestion("SchCA540NR.1.E", line["value"] - overstated,
                       f"{employer}: only wages to {moved.group(2)} are California-source (paystub {stub['id']})",
                       "ca_source_income")
        issues.append(make_issue(
            "ca-wage-source", f"California wages include {overstated:,.0f} earned after the move to Texas",
            "error", "california",
            f"Residency ended {ca_end}. {stub['id']} says work location changed to {moved.group(1)} on {moved.group(2)}, "
            f"CA wages to that date {ca_ytd.group(1)}. But the W-2 ({w2['id']}) box 16 shows {box16['value']:,.0f} because "
            f"payroll kept California until 09/30 by mistake (qa-45). The engine used box 16 as California-source. "
            f"California withholding is still refundable, so the client should not lose it.",
            evidence=[stub["id"], w2["id"], box16["id"], "qa-45", "ev-1081"], lines=["SchCA540NR.1.E"],
            suggestions=[s], effect=total_effect([s])))
    return issues


# ---------------------------------------------------------------------------
# CHECK 9: donated vehicle claimed above what the charity sold it for
# ---------------------------------------------------------------------------
def check_vehicle_donation():
    packet = get_packet()
    issues = []
    line = packet.line("F8283.A1.vehicle")
    for doc in docs_of_type("1098-C"):
        proceeds = next((v for v in packet.values_by_doc[doc["id"]] if v["field"] == "box4c"), None)
        if line and proceeds and line["value"] > proceeds["value"]:
            s = suggestion("F8283.A1.vehicle", proceeds["value"],
                           "Vehicle sold by the charity: deduction limited to gross proceeds (1098-C box 4c)", "itemized")
            issues.append(make_issue(
                "vehicle-donation", f"Car donation claimed at {line['value']:,} but the charity sold it for {proceeds['value']:,.0f}",
                "error", "deductions_and_credits",
                "The engine took the client's Kelley Blue Book estimate (qa-39). When the charity sells the vehicle, "
                "the deduction is limited to the sale proceeds shown on Form 1098-C. This item is later examined by the IRS (Letter 566).",
                evidence=[doc["id"], proceeds["id"], "qa-39", "call-01"], lines=["F8283.A1.vehicle", "SchA.12"],
                suggestions=[s], effect=total_effect([s])))
    return issues


# ---------------------------------------------------------------------------
# CHECK 10: mortgage interest counted from both the 1098 and the payoff letter
# ---------------------------------------------------------------------------
def check_mortgage_interest_double():
    packet = get_packet()
    issues = []
    line = packet.line("SchA.8a")
    for doc in docs_of_type("payoff_statement"):
        if "reported on the 2025 Form 1098" not in full_text(doc):
            continue
        used = [s for s in line["sources"] if s.startswith("ev-") and packet.values[s]["document_id"] == doc["id"]]
        if used:
            amt = sum(packet.values[s]["value"] for s in used)
            s = suggestion("SchA.8a", line["value"] - amt, f"Payoff interest already included in Form 1098 box 1 ({doc['id']} says so)", "itemized")
            issues.append(make_issue(
                "mortgage-payoff-double", f"Payoff interest {amt:,.0f} counted on top of the Form 1098 that already includes it",
                "error", "deductions_and_credits",
                f"{doc['id']}: '(Interest is reported on the 2025 Form 1098.)'. The Lonestar 1098 box 1 already includes interest paid at payoff.",
                evidence=[doc["id"]] + used + ["ev-1029"], lines=["SchA.8a"], suggestions=[s], effect=total_effect([s])))
    return issues


# ---------------------------------------------------------------------------
# CHECK 11: real estate tax includes the seller's share credited at closing
# ---------------------------------------------------------------------------
def check_property_tax_proration():
    packet = get_packet()
    issues = []
    params = packet.raw["engine_reference"]["parameters"]
    for doc in docs_of_type("closing_statement_buyer"):
        credit = next((v for v in packet.values_by_doc[doc["id"]] if v["field"] == "tax_credit"), None)
        if not credit:
            continue
        a5b = packet.line("SchA.5b")
        a5a = packet.line("SchA.5a")
        new_5b = a5b["value"] - credit["value"]
        new_total = a5a["value"] + new_5b
        new_limited = min(new_total, params["salt_cap"])
        s1 = suggestion("SchA.5b", new_5b, f"Remove seller's share of 2025 taxes credited at closing ({doc['id']})", "salt")
        s2 = suggestion("SchA.5e", new_limited, "SALT total now below the 40,000 cap, so the reduction counts federally", "itemized")
        # California has no SALT cap and already counts line 5b, so s2 has no California effect.
        s2 = {**s2, "california_effect": 0.0, "use_effect": True}
        issues.append(make_issue(
            "property-tax-proration", f"Home property tax includes {credit['value']:,.0f} that the seller paid (credited at closing)",
            "error", "deductions_and_credits",
            f"The Travis County bill (13,420) covers all of 2025, but the seller credited the buyers {credit['value']:,.0f} "
            f"for 01/01-07/07. The buyers may deduct only their share. After the fix, SALT drops to {new_total:,.0f}, under "
            f"the cap, so the federal deduction also falls (the engine's 'salt' rate is 0 because it was capped).",
            evidence=[doc["id"], credit["id"], "ev-1036", "D58"], lines=["SchA.5b", "SchA.5e"],
            suggestions=[s1, s2], effect=total_effect([s1, s2])))
    return issues


# ---------------------------------------------------------------------------
# CHECK 12: a "repair" that is really an improvement
# ---------------------------------------------------------------------------
def check_repair_vs_improvement():
    packet = get_packet()
    issues = []
    for alt in packet.raw["what_if"]["alternatives"]:
        if "improvement" not in alt["description"].lower():
            continue
        issues.append(make_issue(
            f"whatif-{alt['id']}", "HVAC replacement (11,000) expensed as a repair; it is a whole-system replacement",
            "error", "rentals_and_sale",
            "The invoice (D47) says the old furnace and condenser were removed and a new heat pump system with air handler "
            "and line set was installed; the client confirmed (call-01, qa-37). Replacing a whole building system is "
            "generally an improvement depreciated over 27.5 years, not a repair. The engine computed this alternative: "
            f"federal tax +{alt['federal']['total_tax_change']:,}, California +{alt['california']['total_tax_change']:,}. "
            "Record the decision as a judgment with authority (Reg. 1.263(a)-3).",
            evidence=["D47", "D46", "ev-1000", "qa-37", "call-01"], lines=["SchE.14A"],
            suggestions=[suggestion("SchE.14A", packet.line("SchE.14A")["value"] - 11000,
                                    "Capitalise HVAC 11,000 (engine what-if hvac_improvement)", "rental_expense",
                                    effect=(alt["federal"]["total_tax_change"], alt["california"]["total_tax_change"]))],
            effect={"federal": alt["federal"]["total_tax_change"], "california": alt["california"]["total_tax_change"]}))
    return issues


# ---------------------------------------------------------------------------
# CHECK 13: self-employed health insurance when a spouse's employer plan was available
# ---------------------------------------------------------------------------
def check_sehi_eligibility():
    packet = get_packet()
    q = packet.questions.get("qa-25")
    line = packet.line("Sch1.17")
    if not q or not line or not line["value"]:
        return []
    if not q["answer"].lower().startswith("yes"):
        return []
    s = suggestion("Sch1.17", 0, "Not allowed for months Erik was eligible for a subsidized employer plan (spouse's employer)", "adjustment")
    return [make_issue(
        "sehi-eligible", f"Self-employed health insurance {line['value']:,} deducted although employer coverage was available",
        "error", "business",
        f"qa-25: '{q['answer']}' The self-employed health insurance deduction is not allowed for any month the person "
        "was eligible to participate in a subsidized plan of an employer of the spouse (IRC 162(l)(2)(B)). Both of "
        "Ngozi's employers offered family coverage, so the deduction looks disallowed for 2025 (check gap months: "
        "Corvane ended 05/16, Halvorsen started 06/02).",
        evidence=["qa-25", "D25", "ev-0484", "qa-27"], lines=["Sch1.17"], suggestions=[s], effect=total_effect([s]))]


# ---------------------------------------------------------------------------
# CHECK 14: personal use inside business expenses (from the client call)
# ---------------------------------------------------------------------------
def check_mixed_use_expenses():
    packet = get_packet()
    v = packet.values
    phone = v["ev-0107"]["value"] * 12      # 180/month
    internet = v["ev-0108"]["value"] * 12   # 110/month
    spotify = v["ev-0106"]["value"]
    call = packet.calls["call-01"]["notes"]
    phone_pct = int(re.search(r"(\d+)%", call[0]).group(1)) / 100
    net_pct = int(re.search(r"(\d+)%", call[1]).group(1)) / 100
    util_line = packet.line("SchC.25")
    soft_line = packet.line("SchC.27a.1")
    new_util = round(phone * phone_pct + internet * net_pct)
    s1 = suggestion("SchC.25", new_util, f"Business share only: phone {phone:,.0f} x {phone_pct:.0%} + internet {internet:,.0f} x {net_pct:.0%} (call-01)")
    s2 = suggestion("SchC.27a.1", soft_line["value"] - spotify, "Remove Spotify Family (personal, booked by mistake per call-01)")
    return [make_issue(
        "mixed-use-expenses", "Personal share of phone, internet and Spotify deducted on Schedule C",
        "warning", "business",
        "The engine mapped book lines 1:1. On the client call Erik said the 4-line family phone plan is ~70% business, "
        "home internet ~50%, and Spotify Family is personal.",
        evidence=["call-01", "D15", "ev-0106", "ev-0107", "ev-0108"], lines=["SchC.25", "SchC.27a.1"],
        suggestions=[s1, s2], effect=total_effect([s1, s2]))]


# ---------------------------------------------------------------------------
# CHECK 15 (generic): foreign currency converted at the wrong year's rate
# ---------------------------------------------------------------------------
def check_fx_year():
    packet = get_packet()
    issues = []
    fx = packet.raw["engine_reference"]["parameters"]["fx"]
    tax_year = str(packet.raw["household"]["tax_year"])
    for key, line in packet.lines[packet.latest_version].items():
        for src in line.get("sources", []):
            m = re.match(r"ref:fx\.(\w+)\.(\d{4})_average", src)
            if m and m.group(2) != tax_year:
                cur = m.group(1)
                right = fx[cur].get(f"{tax_year}_average")
                amount_ev = next((packet.values[s] for s in line["sources"] if s.startswith("ev-")), None)
                if not right or not amount_ev:
                    continue
                new_value = round(amount_ev["value"] / right)
                s = suggestion(key, new_value, f"Convert {cur} {amount_ev['value']:,.2f} at the {tax_year} average rate {right}")
                issues.append(make_issue(
                    f"fx-{key}", f"{key} converted at the {m.group(2)} rate instead of {tax_year}",
                    "warning", "investments_and_digital_assets",
                    f"{cur} {amount_ev['value']:,.2f} / {fx[cur][m.group(2) + '_average']} (={line['value']:,}) should use the "
                    f"{tax_year} average {right} (={new_value:,}). The prior-year rate was carried forward.",
                    evidence=[amount_ev["id"], src], lines=[key], suggestions=[s], effect=total_effect([s])))
    return issues


# ---------------------------------------------------------------------------
# CHECK 16 (generic): dependent SSNs vs documents (prevents e-file rejection)
# ---------------------------------------------------------------------------
def check_dependent_ssn(use_corrections=False):
    """
    use_corrections=False: compare the ORIGINAL organizer data (so the issue stays
    visible, and shows as 'adjusted' once someone fixes it).
    use_corrections=True: compare the corrected data (what e-file would send).
    """
    packet = get_packet()
    issues = []
    people = effective_household() if use_corrections else packet.raw["household"]["people"]
    for person in people:
        if person.get("role") != "dependent":
            continue
        last = person["name"]
        doc_ssns = set()
        refs = []
        for doc in packet.raw["documents"]:
            text = full_text(doc)
            for m in re.finditer(re.escape(last) + r"[^0-9]{0,40}?(\d{3}-\d{2}-\d{4})", text):
                doc_ssns.add(m.group(1))
                refs.append(doc["id"])
        if doc_ssns and person["ssn"] not in doc_ssns:
            correct = sorted(doc_ssns)[0]
            issues.append(make_issue(
                f"ssn-{person['id']}", f"{person['name']}: SSN in the organizer does not match documents",
                "error", "intake",
                f"Organizer (entered by client) ends {person['ssn'][-4:]}; documents ({', '.join(sorted(set(refs)))}) end "
                f"{correct[-4:]}. Looks like transposed digits. E-file will be rejected (dependent TIN/name mismatch) "
                f"until corrected.",
                evidence=sorted(set(refs)) + [f"household:{person['id']}"],
                suggestions=[{"target_type": "household", "target_id": f"{person['id']}.ssn",
                              "old_value": person["ssn"], "new_value": correct, "category": "none",
                              "reason": "Correct SSN per Social Security card / prior-year return",
                              "federal_effect": 0, "california_effect": 0}]))
    return issues


# ---------------------------------------------------------------------------
# CHECK 17: crypto lot with zero basis where the client later supplied basis
# ---------------------------------------------------------------------------
def check_crypto_basis_message():
    packet = get_packet()
    line = packet.line("F8949.H.C6")
    msg = packet.messages.get("msg-07")
    if not line or not msg:
        return []
    m = re.search(r"in (\w+ \d{4}) for about ([\d,]+)", msg["text"])
    basis = money(m.group(2)) if m else None
    if basis is None:
        return []
    new_gain = line["value"] - basis
    fed = round(-line["value"] * get_packet().versions["v3"]["tax_effect_rates"]["stcg"]["federal_per_dollar"]
                + new_gain * get_packet().versions["v3"]["tax_effect_rates"]["ltcg"]["federal_per_dollar"], 2)
    ca = round((new_gain - line["value"]) * get_packet().versions["v3"]["tax_effect_rates"]["stcg"]["california_per_dollar"], 2)
    return [make_issue(
        "crypto-c6-basis", f"Coinhaven lot C6: client now says basis ~{basis:,.0f}, acquired {m.group(1)} (long-term)",
        "warning", "investments_and_digital_assets",
        f"msg-07: '{msg['text']}' Evidence is thin (one bank line). Judgment: accept with a bank statement attached, "
        f"or keep zero basis. If accepted the gain falls from {line['value']:,.0f} to {new_gain:,.0f} and becomes long-term "
        f"(Form 8949 box K instead of H).",
        evidence=["msg-07", "D35", "ev-0879"], lines=["F8949.H.C6"],
        suggestions=[suggestion("F8949.H.C6", new_gain, "Basis 3,050 per client (msg-07) + bank statement; long-term holding",
                                "ltcg", effect=(fed, ca))],
        effect={"federal": fed, "california": ca})]


# ---------------------------------------------------------------------------
# CHECK 18: client answers that contradict documents
# ---------------------------------------------------------------------------
def check_client_conflicts():
    packet = get_packet()
    issues = []
    q31 = packet.questions["qa-31"]
    issues.append(make_issue(
        "conflict-qa-31", "Client says no prior IRA basis; Form 5498 shows a 2024 contribution of 7,000",
        "info", "deductions_and_credits",
        f"qa-31: '{q31['answer']}' But D42 (Form 5498 for 2024) shows 7,000 contributed on 02/12/2025 for 2024, and the "
        "prior preparer recorded 7,000 basis. The engine correctly used 14,000 total basis (taxable conversion 26). "
        "Document that the documents override the client's memory.",
        evidence=["qa-31", "D42", "ev-0991", "D07"], lines=["F8606.2", "F8606.18"]))
    q15 = packet.questions["qa-15"]
    issues.append(make_issue(
        "conflict-qa-15", "Client says crypto was 'just moved around'; Coinhaven reports 12 sales",
        "info", "investments_and_digital_assets",
        f"qa-15: '{q15['answer']}' Form 1099-DA (D34) reports 12 dispositions with 34,833 proceeds. Explain to the client "
        "that these are taxable sales.",
        evidence=["qa-15", "D34", "D35"], lines=["SchD.3"]))
    return issues


# ---------------------------------------------------------------------------
# CHECK 19: filings outside this return that someone must own
# ---------------------------------------------------------------------------
def check_outside_filings():
    packet = get_packet()
    prior = packet.raw["prior_years"]["prior_preparer_workpaper"]
    items = [
        ("fbar", "FBAR (FinCEN 114) for the Swedish account",
         "Max balance SEK 1,452,300 is far over 10,000 USD. Outside the engagement (D01) - confirm the client filed it "
         "(prior preparer: 'FBAR filed by client separately'). Due 10/15/2026.", ["D36", "ev-0962", "D01", "D07"]),
        ("1099nec", "Forms 1099-NEC the studio should have issued",
         "Two contractors paid 4,800 and 2,600 (D27). Due 02/02/2026, now late; outside the engagement. Tell the client in writing.",
         ["D27", "qa-23", "D01"]),
        ("form3520", "Possible 2023 Form 3520 (foreign inheritance) never resolved",
         "The Swedish account came from an inheritance in 09/2023 (D36, qa-07). The prior preparer left this open: "
         f"'{prior['open_items_left_by_prior_preparer'][-1]}'. Ask the client; penalties for a missed 3520 are large.",
         ["D36", "qa-07", "D07"]),
        ("ada-return", "Ada files her own return - she must check 'can be claimed as a dependent'",
         "Ada (19, full-time student) has a 4,100 W-2 and files with consumer software. We claim her as a dependent.",
         ["household:ada"]),
        ("oregon-arizona", "Oregon and Arizona composite returns cover the K-1 income",
         "Woodworks filed an Oregon composite (D39) and Ridgeline an Arizona composite (D38). Confirm no separate "
         "individual filing is needed (engagement excludes other states).", ["D39", "D38", "D01"]),
    ]
    return [make_issue(f"outside-{i}", t, "warning", "summary", e, evidence=ev) for i, t, e, ev in items]


# ---------------------------------------------------------------------------
# The engine's own diagnostics, shown in the same list
# ---------------------------------------------------------------------------
def engine_diagnostics():
    packet = get_packet()
    issues = []
    for d in packet.versions[packet.latest_version]["diagnostics"]:
        refs = d["refs"]
        lines = [r for r in refs if r in packet.lines[packet.latest_version]]
        issues.append(make_issue(
            f"engine-{d['code']}", d["message"][:110], "warning" if d["severity"] == "warning" else "info",
            "engine", d["message"], evidence=[r for r in refs if r not in lines], lines=lines, source="engine"))
    return issues


ALL_CHECKS = [
    check_values_against_pages, check_duplicate_pages, check_duplicate_interest,
    check_1099k_already_in_books, check_unread_pages, check_noncovered_basis,
    check_payments_vs_agency, check_state_wage_sourcing, check_vehicle_donation,
    check_mortgage_interest_double, check_property_tax_proration, check_repair_vs_improvement,
    check_sehi_eligibility, check_mixed_use_expenses, check_fx_year, check_dependent_ssn,
    check_crypto_basis_message, check_client_conflicts, check_outside_filings, engine_diagnostics,
]


# Checks that only look at the packet give the same answer every time, so we
# run them once and keep the result (a simple cache). Checks that depend on
# what humans changed (like the corrected SSN) run every time.
CHECKS_THAT_DEPEND_ON_DATABASE = set()  # none right now, but the mechanism is here
_static_cache = None


def _run(check):
    try:
        return check()
    except Exception as exc:  # a broken check should never crash the app
        return [make_issue(f"check-crashed-{check.__name__}", f"Check {check.__name__} failed: {exc}",
                           "warning", "system", "This is a bug in the check itself.")]


def run_all_checks():
    """Run every check and attach the saved status (open/adjusted/accepted/dismissed)."""
    global _static_cache
    if _static_cache is None:
        _static_cache = []
        for check in ALL_CHECKS:
            if check not in CHECKS_THAT_DEPEND_ON_DATABASE:
                _static_cache.extend(_run(check))
    # copy each dict so adding "status" below does not change the cache
    issues = [dict(i) for i in _static_cache]
    for check in CHECKS_THAT_DEPEND_ON_DATABASE:
        issues.extend(_run(check))
    saved = {r["issue_id"]: r for r in query("SELECT * FROM issue_status")}
    # An issue counts as "adjusted" automatically if any of its suggestions was applied
    applied = {a["issue_id"] for a in query("SELECT issue_id FROM adjustments WHERE status != 'reverted' AND issue_id IS NOT NULL")}
    # Big fixes (tax effect over 1,000) wait for the partner. Until she approves,
    # the issue is NOT done, so we show it as "waiting_partner" instead of "adjusted".
    waiting = {}
    for a in query("SELECT id, issue_id FROM adjustments WHERE status = 'pending_partner' AND issue_id IS NOT NULL"):
        waiting.setdefault(a["issue_id"], []).append(a["id"])
    for issue in issues:
        row = saved.get(issue["id"])
        issue["pending_adjustments"] = waiting.get(issue["id"], [])
        if issue["pending_adjustments"]:
            issue["status"] = "waiting_partner"
        elif row:
            issue["status"] = row["status"]
        else:
            issue["status"] = "adjusted" if issue["id"] in applied else "open"
        issue["status_note"] = row["note"] if row else None
        issue["status_by"] = row["updated_by"] if row else None
    order = {"error": 0, "warning": 1, "info": 2}
    # Open and waiting-for-partner issues go first: both still need someone to act.
    issues.sort(key=lambda i: (i["status"] not in ("open", "waiting_partner"), order.get(i["severity"], 3)))
    return issues


# ---------------------------------------------------------------------------
# Completeness: every document either used, superseded, or explained
# ---------------------------------------------------------------------------
def document_accounting():
    packet = get_packet()
    ver = packet.versions[packet.latest_version]
    used_docs = packet.doc_used_by[packet.latest_version]
    rows = []
    for doc in packet.raw["documents"]:
        if doc["id"] in ver["documents_superseded"]:
            state = "superseded"
            detail = "Replaced by a corrected document"
        elif doc["id"] in used_docs:
            state = "used"
            detail = f"Feeds {len(used_docs[doc['id']])} line(s)"
        elif doc["id"] in ver["documents_used"]:
            state = "used_no_line"
            detail = "Engine read it but no line cites a value from it"
        elif doc["extraction"]["status"] == "not_extracted":
            state = "reference"
            detail = doc["extraction"]["reason"]
        else:
            state = "unused"
            detail = "Not used by the engine"
        rows.append({"id": doc["id"], "title": doc["title"], "state": state, "detail": detail,
                     "received_at": doc["received_at"],
                     "lines": used_docs.get(doc["id"], [])})
    return rows
