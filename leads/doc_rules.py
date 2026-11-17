"""Which documents each case needs, and what is still missing.

Every requirement has one of three states, per case:
  required  - נדרש: counts as missing until a matching document is uploaded
  optional  - אם יש: shown, but never counted as missing
  off       - לא נדרש: hidden from the missing list

The defaults below depend on the case type. A person can change any requirement on any case
(the clerk only suggests; people decide what is really missing). Overrides live in Case.doc_settings.
This module has no database access of its own and does not import models.
"""
from collections import OrderedDict
from datetime import datetime, timezone as _tz

REQUIRED, OPTIONAL, OFF = "required", "optional", "off"
STATE_LABELS = OrderedDict([(REQUIRED, "נדרש"), (OPTIONAL, "אם יש"), (OFF, "לא נדרש")])

# key -> (label shown to people and used in client messages, document categories that satisfy it)
REQ = OrderedDict([
    ("id_front", ("ת״ז - צד קדמי", ("id_front", "id"))),
    ("id_back", ("ת״ז - צד אחורי", ("id_back", "id"))),
    ("bank_cert", ("אישור ניהול חשבון", ("bank_cert",))),
    ("tax_exempt", ("אישור פטור ממס", ("tax_exempt",))),
    ("ni_employers", ("דוח סיום מעסיקים מביטוח לאומי", ("ni_employers",))),
    ("income", ("תלושי שכר או שומות", ("payslip", "assessment"))),
    ("credit_or_amort", ("דוח נתוני אשראי או דוח סילוקין משכנתא", ("credit", "amort"))),
    ("credit_or_balances", ("דוח יתרות או דוח נתוני אשראי", ("balances", "credit"))),
    ("bank_statements", ("דפי בנק", ("bank",))),
    ("loans_list", ("פירוט הלוואות", ("loans",))),
    ("property", ("מסמכי נכס", ("property",))),
    ("pension", ("דוח פנסיה / קופות", ("pension",))),
    ("poa", ("ייפוי כוח / הסכם שכר טרחה חתום", ("poa",))),
    ("debt_title", ("אסמכתא לחוב (שטר, פסק דין, חוזה או חשבונית)", ("debt_title",))),
    ("debt_calc", ("חישוב חוב מעודכן (קרן, ריבית והצמדה)", ("debt_calc",))),
    ("service", ("אישור מסירה / התראה לחייב", ("service",))),
    ("court", ("החלטות ומסמכי הוצל״פ / בית משפט", ("court",))),
    ("correspondence", ("תכתובת עם החייב", ("correspondence",))),
    ("fee_receipt", ("קבלת תשלום אגרה", ("fee_receipt",))),
    ("contract", ("חוזה חתום", ("contract",))),
])

FINANCE_KEYS = ["id_front", "id_back", "bank_cert", "tax_exempt", "ni_employers", "income", "credit_or_amort",
                "credit_or_balances", "bank_statements", "loans_list", "property", "pension", "contract"]
LEGAL_KEYS = ["id_front", "id_back", "poa", "debt_title", "debt_calc", "service", "court", "correspondence",
              "fee_receipt", "bank_cert", "contract"]

_FIN_COMMON = ["id_front", "id_back", "bank_cert", "ni_employers"]
# case_type -> (required keys, optional keys)
DEFAULTS = {
    "pension": (_FIN_COMMON + ["pension", "contract"], ["tax_exempt"]),
    "consolidation": (_FIN_COMMON + ["income", "credit_or_balances", "contract"], ["tax_exempt"]),
    "property_loan": (_FIN_COMMON + ["income", "credit_or_amort", "property", "contract"], ["tax_exempt"]),
    "refinance": (_FIN_COMMON + ["income", "credit_or_amort", "property", "contract"], ["tax_exempt"]),
    "enforcement": (["id_front", "id_back", "poa", "debt_title", "debt_calc"],
                    ["service", "court", "correspondence", "fee_receipt", "bank_cert"]),
    "other": (["id_front", "id_back", "contract"], []),
}


def catalog(workspace):
    return LEGAL_KEYS if workspace == "legal" else FINANCE_KEYS


def defaults(case_type):
    req, opt = DEFAULTS.get(case_type, DEFAULTS["other"])
    d = {k: REQUIRED for k in req}
    d.update({k: OPTIONAL for k in opt})
    return d


def states(case):
    """key -> state for every requirement relevant to this case's workspace (defaults + the person's overrides)."""
    base = defaults(case.case_type)
    over = (case.doc_settings or {}).get("states", {})
    keys = list(catalog(case.workspace))
    for k in list(base) + list(over):  # never lose a requirement a person set or the type defaults to
        if k in REQ and k not in keys:
            keys.append(k)
    return OrderedDict((k, over.get(k) if over.get(k) in STATE_LABELS else base.get(k, OFF)) for k in keys)


def evaluate(case, docs):
    """Checklist rows for the requirements that are not switched off: required first, then optional."""
    have = {d.category for d in docs}
    rows = []
    for key, state in states(case).items():
        if state == OFF:
            continue
        label, cats = REQ[key]
        rows.append({"key": key, "label": label, "state": state, "ok": any(c in have for c in cats)})
    rows.sort(key=lambda r: 0 if r["state"] == REQUIRED else 1)
    return rows


def settings_rows(case):
    """Every requirement with its state, for the editing form."""
    default = defaults(case.case_type)
    return [{"key": k, "label": REQ[k][0], "state": s, "is_default": k in default} for k, s in states(case).items()]


def missing(rows):
    return [r for r in rows if r["state"] == REQUIRED and not r["ok"]]


def save_states(case, posted):
    """posted: {key: state}. Stores only what differs from the case-type defaults."""
    base = defaults(case.case_type)
    current = states(case)
    over = {}
    for key in current:
        new = posted.get(key)
        if new not in STATE_LABELS:
            new = current[key]
        if new != base.get(key, OFF):
            over[key] = new
    s = dict(case.doc_settings or {})
    s["states"] = over
    case.doc_settings = s


def confirm(case, missing_keys):
    s = dict(case.doc_settings or {})
    s["confirmed_at"] = datetime.now(_tz.utc).isoformat()
    s["confirmed_missing"] = sorted(missing_keys)
    case.doc_settings = s


def is_confirmed(case, missing_keys):
    """True when a person approved exactly this list of missing documents."""
    s = case.doc_settings or {}
    return bool(s.get("confirmed_at")) and sorted(s.get("confirmed_missing", [])) == sorted(missing_keys)
