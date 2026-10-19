"""
permissions.py
--------------
Who is allowed to do what. Roles come from the packet (firm.staff[].role).

Core demo version: only the three roles that do the work on a return.
  preparer  -> does the work (fixes numbers, signs as preparer)
  reviewer  -> checks the work (signs as reviewer)
  partner   -> owns the risk (approves big fixes, signs as partner, files)

We check permissions on the SERVER, not only in the browser. The browser
can hide buttons for a nicer experience, but anyone could call the API
directly, so the server is the real gatekeeper.
"""

import re

# Each role gets a set of "actions" it may perform.
ROLE_PERMISSIONS = {
    "preparer": {
        "view_workpaper", "adjust", "bulk", "update_issue", "signoff_preparer",
    },
    "reviewer": {
        "view_workpaper", "adjust", "bulk", "update_issue", "signoff_reviewer",
    },
    "partner": {
        "view_workpaper", "adjust", "bulk", "update_issue", "signoff_partner",
        "approve_adjustment", "file_return",
    },
}

# The roles that appear in the "Acting as" dropdown.
WORK_ROLES = ("preparer", "reviewer", "partner")


def can(user, action):
    """True if this user's role allows this action."""
    if user is None:
        return False
    return action in ROLE_PERMISSIONS.get(user["role"], set())


# ---------------------------------------------------------------------------
# Sensitive data masking
# ---------------------------------------------------------------------------
# Social Security numbers, bank/account numbers and PINs are always shown
# masked (***-**-1234). The core demo has no "reveal" button.

SSN_PATTERN = re.compile(r"\b\d{3}-\d{2}-(\d{4})\b")
# Long digit runs (bank accounts, IBAN-like numbers). 8+ digits possibly with spaces.
ACCOUNT_PATTERN = re.compile(r"\b(?:[A-Z]{2}\d{2}[ ]?)?(?:\d[ ]?){8,}\d\b")

PIN_PATTERN = re.compile(r"(PIN[^0-9]{0,60})(\d{5,6})")

SENSITIVE_VALUE_TYPES = {"ssn", "account_number"}


def mask_text(text):
    """Mask SSNs and long account numbers inside any free text."""
    if not isinstance(text, str):
        return text
    text = SSN_PATTERN.sub(lambda m: "***-**-" + m.group(1), text)
    text = ACCOUNT_PATTERN.sub(lambda m: "****" + m.group(0).replace(" ", "")[-4:], text)
    # IP PINs and signature PINs: "PIN ... 482915" -> "PIN ... ******"
    text = PIN_PATTERN.sub(lambda m: m.group(1) + "*" * len(m.group(2)), text)
    return text


def mask_value(value, value_type):
    """Mask one extracted value if its type is sensitive."""
    if value_type in SENSITIVE_VALUE_TYPES and value:
        s = str(value)
        return "****" + s[-4:]
    return value
