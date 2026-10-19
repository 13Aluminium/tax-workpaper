"""
packet.py
---------
Loads the JSON packet that Miraflores' pipeline produces and builds some
lookup tables ("indexes") so the rest of the app can find things quickly.

IMPORTANT IDEA: the packet is READ-ONLY.
We never change it. Everything a human does (notes, adjustments, sign-offs)
is stored separately in the SQLite database (see database.py). This way we
can always show "what the engine said" next to "what we changed".
"""

import json
import os
import re
from collections import defaultdict

# Where the packet lives. You can override it with an environment variable,
# e.g.  PACKET_PATH=/some/other/packet.json python app.py
DEFAULT_PACKET_PATH = os.path.join(os.path.dirname(__file__), "data", "packet.json")
PACKET_PATH = os.environ.get("PACKET_PATH", DEFAULT_PACKET_PATH)


# Each return line belongs to a "form" (F1040, SchC, ...).
# We group forms into review AREAS. Areas are used for:
#   - sign-offs on a whole area ("I reviewed all of Schedule C")
#   - assignments and time budgets (the firm budgets hours per area)
# The area names match firm.budget_hours in the packet.
FORM_TO_AREA = {
    "F1040": "summary",
    "Sch1": "summary",
    "Sch2": "summary",
    "Sch3": "summary",
    "F2210": "summary",
    "F8959": "wages_and_stock_compensation",
    "F6251": "wages_and_stock_compensation",
    "F8606": "deductions_and_credits",
    "F8889": "deductions_and_credits",
    "SchB": "investments_and_digital_assets",
    "SchD": "investments_and_digital_assets",
    "F8949": "investments_and_digital_assets",
    "F8960": "investments_and_digital_assets",
    "F8938": "investments_and_digital_assets",
    "SchC": "business",
    "SchSE": "business",
    "F4562-C": "business",
    "F8995": "business",
    "SchE": "rentals_and_sale",
    "F4797": "rentals_and_sale",
    "F8582": "rentals_and_sale",
    "F4562-E": "rentals_and_sale",
    "F7203": "pass_throughs_and_basis",
    "SchA": "deductions_and_credits",
    "F8283": "deductions_and_credits",
    "F8812": "deductions_and_credits",
    "F8863": "deductions_and_credits",
    "F2441": "deductions_and_credits",
    "F8867": "deductions_and_credits",
    "SchCA540NR": "california",
    "F540NR": "california",
    "FTB3885A": "california",
    "FTB5805": "california",
    "ScheduleS": "california",
}


def guess_tax_effect_category(line_key):
    """
    The engine cannot be re-run. To estimate the tax effect of a change we
    multiply the dollar change by a rate from return_versions[].tax_effect_rates.
    Each rate has a CATEGORY (wages, interest, schc_profit, ...).

    This function makes a best guess of the category from the line key.
    The user can always pick a different category in the UI.
    Returns None for lines that are payments/credits (those change tax 1:1).
    """
    form = line_key.split(".")[0]
    if line_key.startswith("F1040.1"):
        return "wages"
    if form == "SchB" or line_key in ("F1040.2b", "F1040.3b"):
        return "interest"
    if line_key.startswith("SchC.44") or line_key.startswith("SchC.4"):
        return "none"  # Part IV: miles and dates, not dollars
    if form == "F4562-C":
        return "schc_expense"
    if form == "SchC":
        # Lines 8 to 30 are expenses: a LOWER expense means MORE profit and more tax.
        number = re.match(r"SchC\.(\d+)", line_key)
        if number and 8 <= int(number.group(1)) <= 30:
            return "schc_expense"
        return "schc_profit"
    if form == "SchSE":
        return "none"
    if form == "F8949":
        box = line_key.split(".")[1]
        # Boxes A, B, C, G, H, I are short-term. D, E, F, J, K, L are long-term.
        return "stcg" if box in ("A", "B", "C", "G", "H", "I") else "ltcg"
    if form == "SchD":
        return "ltcg"
    if form == "F4562-E":
        return "rental_expense"
    if form == "SchE":
        # Lines 5 to 19 are rental expenses (insurance, repairs, taxes...)
        number = re.match(r"SchE\.(\d+)", line_key)
        if number and 5 <= int(number.group(1)) <= 19:
            return "rental_expense"
        return "ordinary_income"
    if form in ("F4797", "F8582", "F7203"):
        return "ordinary_income"
    if form == "Sch1":
        return "adjustment"
    if line_key.startswith("SchA.5"):
        return "salt"
    if form in ("SchA", "F8283"):
        return "itemized"
    if form == "SchCA540NR":
        return "ca_source_income"
    return None  # payments and credits: 1 dollar change = 1 dollar of tax


def resolve_json_pointer(data, pointer):
    """
    The packet uses JSON Pointers (RFC 6901) for references like
    'py:/carryovers_into_2025/capital_loss_short'.
    This walks the data step by step: /a/b/0 means data['a']['b'][0].
    Returns None if the path does not exist.
    """
    current = data
    for part in pointer.strip("/").split("/"):
        if part == "":
            continue
        # RFC 6901 escaping rules
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(current, dict):
            if part not in current:
                return None
            current = current[part]
        else:
            return None
    return current


class Packet:
    """Holds the whole packet plus handy lookup dictionaries."""

    def __init__(self, path=PACKET_PATH):
        with open(path, "r", encoding="utf-8") as f:
            self.raw = json.load(f)

        raw = self.raw

        # --- simple "id -> object" dictionaries ---------------------------
        self.documents = {d["id"]: d for d in raw["documents"]}
        self.values = {v["id"]: v for v in raw["extracted_values"]}
        self.questions = {q["id"]: q for q in raw["client"]["questionnaire"]}
        self.messages = {m["id"]: m for m in raw["client"]["messages"]}
        self.calls = {c["id"]: c for c in raw["client"]["call_notes"]}
        self.people = {p["id"]: p for p in raw["household"]["people"]}
        self.timeline = {t["id"]: t for t in raw["timeline"]}

        # Values grouped by document, so the document viewer can show them.
        self.values_by_doc = defaultdict(list)
        for v in raw["extracted_values"]:
            self.values_by_doc[v["document_id"]].append(v)

        # --- return versions ------------------------------------------------
        # versions["v3"] is the whole version object
        # lines["v3"]["F1040.1a"] is one line
        self.versions = {v["version"]: v for v in raw["return_versions"]}
        self.version_order = [v["version"] for v in raw["return_versions"]]
        self.latest_version = self.version_order[-1]  # v3 = current return
        self.lines = {}
        for ver, vobj in self.versions.items():
            self.lines[ver] = {line["key"]: line for line in vobj["lines"]}

        # --- reverse indexes (this is what makes "trace both ways" work) ----
        # value_used_by[ver][ev_id]  -> list of line keys that cite this value
        # line_used_by[ver][line_key] -> list of line keys that use this line as input
        # doc_used_by[ver][doc_id]   -> list of line keys built from this document
        self.value_used_by = {}
        self.line_used_by = {}
        self.doc_used_by = {}
        for ver, line_dict in self.lines.items():
            v_used = defaultdict(list)
            l_used = defaultdict(list)
            d_used = defaultdict(set)
            for key, line in line_dict.items():
                for src in line.get("sources", []):
                    if src.startswith("ev-"):
                        v_used[src].append(key)
                        ev = self.values.get(src)
                        if ev:
                            d_used[ev["document_id"]].add(key)
                for inp in line.get("inputs", []):
                    l_used[inp].append(key)
            self.value_used_by[ver] = v_used
            self.line_used_by[ver] = l_used
            self.doc_used_by[ver] = {k: sorted(v) for k, v in d_used.items()}

    # ------------------------------------------------------------------
    # Small helper methods
    # ------------------------------------------------------------------
    def staff_and_clients(self):
        """All the people who can log in: firm staff + the two clients."""
        users = []
        for s in self.raw["firm"]["staff"]:
            users.append({
                "id": s["id"],
                "name": s["name"],
                "role": s["role"],
                "clearance": bool(s.get("sensitive_data_clearance")),
                "credential": s.get("credential"),
                "note": s.get("note"),
            })
        for c in self.raw["firm"]["client_users"]:
            users.append({"id": c["id"], "name": c["name"], "role": "client",
                          "clearance": False, "credential": None, "note": None})
        return users

    def line(self, key, version=None):
        version = version or self.latest_version
        return self.lines.get(version, {}).get(key)

    def page_text(self, doc_id, page_no):
        doc = self.documents.get(doc_id)
        if not doc:
            return None
        for p in doc["pages"]:
            if p["page"] == page_no:
                return p["printed_content"]
        return None

    def describe_ref(self, ref, version=None):
        """
        Turn any reference string used in the packet into something the UI can
        show and click. Returns a dict like:
            {"ref": "ev-0016", "kind": "value", "label": "...", "doc": "D08"}
        Knowing the 'kind' lets the frontend decide what to open on click.
        """
        version = version or self.latest_version
        if ref.startswith("ev-"):
            ev = self.values.get(ref)
            if not ev:
                return {"ref": ref, "kind": "missing", "label": "Unknown value " + ref}
            return {"ref": ref, "kind": "value", "doc": ev["document_id"], "page": ev["page"],
                    "label": f'{ev["label"]} = {ev["text_as_read"] or ev["value"]} ({ev["document_id"]} p{ev["page"]})'}
        if ref in self.documents:
            return {"ref": ref, "kind": "document", "doc": ref, "label": self.documents[ref]["title"]}
        if ref in self.questions:
            q = self.questions[ref]
            return {"ref": ref, "kind": "client", "label": f'Questionnaire: "{q["question"]}" -> "{q["answer"]}"'}
        if ref in self.messages:
            m = self.messages[ref]
            return {"ref": ref, "kind": "client", "label": f'Message from {m["from"]}: "{m["text"][:120]}"'}
        if ref in self.calls:
            return {"ref": ref, "kind": "client", "label": "Client call notes " + self.calls[ref]["date"][:10]}
        if ref.startswith("py:"):
            value = resolve_json_pointer(self.raw["prior_years"], ref[3:])
            return {"ref": ref, "kind": "prior_year", "label": "Prior year record " + ref[3:], "value": value}
        if ref.startswith("agency:"):
            value = resolve_json_pointer(self.raw["tax_agency_records"], ref[7:])
            return {"ref": ref, "kind": "agency", "label": "Tax agency record " + ref[7:], "value": value}
        if ref.startswith("asset:"):
            return {"ref": ref, "kind": "asset", "label": "Asset " + ref[6:]}
        if ref.startswith("household:"):
            person = self.people.get(ref[10:])
            label = person["name"] if person else ref
            return {"ref": ref, "kind": "household", "label": "Household member: " + label}
        if ref.startswith("ref:"):
            # e.g. ref:param.mileage_rate or ref:fx.SEK.2024_average
            path = ref[4:].split(".")
            value = self.raw["engine_reference"]
            if path[0] == "fx":
                value = value["parameters"].get("fx", {})
                path = path[1:]
            elif path[0] == "param":
                value = value["parameters"]
                path = path[1:]
            for p in path:
                value = value.get(p) if isinstance(value, dict) else None
            return {"ref": ref, "kind": "engine_reference", "label": f"Engine parameter {ref[4:]} = {value}", "value": value}
        if ref.startswith("timeline:"):
            t = self.timeline.get(ref[9:])
            return {"ref": ref, "kind": "timeline", "label": "Timeline: " + (t["event"] if t else ref)}
        if ref in self.lines.get(version, {}):
            line = self.lines[version][ref]
            return {"ref": ref, "kind": "line", "label": f'{ref} {line["label"]}'}
        return {"ref": ref, "kind": "other", "label": ref}


# One shared packet object for the whole app (loaded once at startup).
_packet = None


def get_packet():
    global _packet
    if _packet is None:
        _packet = Packet()
    return _packet
