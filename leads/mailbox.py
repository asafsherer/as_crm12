"""Reading email into the CRM: parse a message, match it to a contact, classify it, keep its attachments.

Everything here is plain code (no AI). A message is stored and SUGGESTIONS are made (which contact, which case, what kind of
document an attachment looks like). Filing an attachment into a case is always done by a person (see mail_views).

Two ways in:
  * ingest_bytes(raw)  - one RFC822 message (an .eml file uploaded on the Mail screen, or fetched over IMAP)
  * fetch_imap()       - pulls recent messages from the business mailbox (`python manage.py fetch_mail`)
"""
import email
import imaplib
import os
import re
from datetime import timedelta
from email import policy
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from html import unescape

from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone

from . import doc_rules
from .models import Case, Document, EmailAttachment, EmailMessage, Lead

MAX_ATTACHMENT = Document.MAX_BYTES
MAX_BODY = 60000
OWN_DOMAINS_SETTING = "MAIL_OWN_ADDRESSES"

# words that point to a kind of attachment (checked against the file name and the subject). First match wins.
ATTACHMENT_RULES = [
    ("payslip", ("תלוש", "payslip", "salary")),
    ("assessment", ("שומה", "שומת", "assessment")),
    ("bank_cert", ("ניהול חשבון", "ניהול_חשבון", "אישור חשבון", "bank_cert")),
    ("ni_employers", ("מעסיקים", "ביטוח לאומי", "ביטוח_לאומי", "bituach")),
    ("tax_exempt", ("פטור",)),
    ("amort", ("סילוקין", "לוח סילוקין", "amort")),
    ("credit", ("נתוני אשראי", "נתוני_אשראי", "דוח אשראי", "credit")),
    ("balances", ("יתרות", "balances")),
    ("pension", ("פנסיה", "קופת גמל", "הר הביטוח", "pension")),
    ("id_front", ("צד קדמי", "קדימה", "front")),
    ("id_back", ("צד אחורי", "אחורה", "back")),
    ("id", ("תעודת זהות", "תעודת_זהות", "ת.ז", "תז", "ת״ז", "id_card")),
    ("poa", ("ייפוי כוח", "יפוי כוח", "הסכם שכר")),
    ("receipt", ("קבלה", "חשבונית", "receipt", "invoice")),
    ("bank", ("דף בנק", "דפי בנק", "עובר ושב", "statement")),
]
RECEIPT_WORDS = ("קבלה", "חשבונית", "receipt", "invoice", "אסמכתא לתשלום", "תשלום התקבל")
AUTO_SENDERS = ("no-reply", "noreply", "do-not-reply", "donotreply", "mailer-daemon", "newsletter")


def _clean(s, limit):
    return re.sub(r"[\x00-\x08\x0b-\x1f]", "", str(s or "")).strip()[:limit]


def _html_to_text(html):
    html = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", html)
    return re.sub(r"[ \t]+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).strip()


def _addr(value):
    return (parseaddr(value or "")[1] or "").strip().lower()[:254]


def _has_word(hay, word):
    """Short tokens (like "תז") must stand alone, so they do not match inside other words (e.g. "תזכורת")."""
    if len(word) > 3:
        return word in hay
    return re.search(r"(?<![\w\u0590-\u05FF])" + re.escape(word) + r"(?![\w\u0590-\u05FF])", hay) is not None


def _match(hay):
    for cat, words in ATTACHMENT_RULES:
        if any(_has_word(hay, w) for w in words):
            return cat
    return None


def category_for_name(name, subject=""):
    """Looks at the file name first. The subject is only a fallback, so a subject like "תלושי שכר ואישור ניהול חשבון"
    does not make every attachment a payslip."""
    return _match(str(name).lower()) or _match(str(subject).lower()) or "other"


def classify(subject, body, attachments, from_addr):
    """receipt | documents | client | other"""
    text = f"{subject} {body[:1500]}".lower()
    names = [a["name"] for a in attachments]
    if any(category_for_name(n) == "receipt" for n in names) or any(w in text for w in RECEIPT_WORDS):
        return EmailMessage.Category.RECEIPT
    if any(category_for_name(n) not in ("other",) for n in names) or attachments:
        return EmailMessage.Category.DOCUMENTS
    if any(x in from_addr for x in AUTO_SENDERS):
        return EmailMessage.Category.OTHER
    return EmailMessage.Category.OTHER


def match_lead(addresses):
    """The contact whose email address appears among these addresses (exact, case-insensitive). None when unknown or ambiguous."""
    addrs = {a for a in addresses if a}
    own = {a.lower() for a in getattr(settings, OWN_DOMAINS_SETTING, [])}
    addrs -= own
    if not addrs:
        return None
    found = []
    for a in addrs:
        found.extend(Lead.objects.filter(email__iexact=a)[:3])
    ids = {l.pk for l in found}
    if len(ids) == 1:
        return found[0]
    if len(ids) > 1:  # same address on several leads (e.g. the same person in both workspaces): prefer finance, else the first
        return sorted(found, key=lambda l: (l.workspace != "finance", l.pk))[0]
    return None


def suggest_case(lead):
    """The single open case of this contact, when there is exactly one."""
    if not lead:
        return None
    open_cases = list(lead.cases.exclude(stage__in=[Case.Stage.CLOSED, Case.Stage.CANCELLED])[:3])
    return open_cases[0] if len(open_cases) == 1 else None


def would_fill(case, categories):
    """Labels of the case's still-missing required documents that these attachment categories would satisfy."""
    if not case:
        return []
    have = set(case.documents.values_list("category", flat=True))
    out = []
    for r in doc_rules.evaluate(case, [type("D", (), {"category": c}) for c in have]):
        if r["state"] == doc_rules.REQUIRED and not r["ok"]:
            cats = doc_rules.REQ[r["key"]][1]
            if any(c in cats for c in categories):
                out.append(r["label"])
    return out


def ingest_bytes(raw, default_workspace="finance"):
    """Store one message. Returns (EmailMessage, created). Duplicates (same Message-ID) are ignored."""
    msg = email.message_from_bytes(raw, policy=policy.default)
    mid = _clean(msg.get("Message-ID"), 255) or ""
    if not mid:
        import hashlib
        mid = "no-id-" + hashlib.sha256(raw).hexdigest()
    existing = EmailMessage.objects.filter(message_id=mid).first()
    if existing:
        return existing, False

    from_addr = _addr(msg.get("From"))
    to_list = [a for _, a in getaddresses(msg.get_all("To", []) + msg.get_all("Cc", [])) if a]
    own = {a.lower() for a in getattr(settings, OWN_DOMAINS_SETTING, [])}
    direction = EmailMessage.Direction.OUT if from_addr in own else EmailMessage.Direction.IN
    subject = _clean(msg.get("Subject"), 300)
    try:
        sent_at = parsedate_to_datetime(msg.get("Date")) if msg.get("Date") else None
        if sent_at and timezone.is_naive(sent_at):
            sent_at = timezone.make_aware(sent_at, timezone.utc)
    except (TypeError, ValueError):
        sent_at = None

    body = ""
    part = msg.get_body(preferencelist=("plain",))
    if part:
        body = part.get_content()
    else:
        part = msg.get_body(preferencelist=("html",))
        if part:
            body = _html_to_text(part.get_content())
    body = _clean(body, MAX_BODY)

    atts = []
    for p in msg.iter_attachments():
        name = _clean(p.get_filename() or "", 200)
        ext = os.path.splitext(name)[1].lower()
        if not name or ext not in Document.ALLOWED_EXT:
            continue
        data = p.get_payload(decode=True) or b""
        if not data or len(data) > MAX_ATTACHMENT:
            continue
        atts.append({"name": name, "data": data})

    # a contact can be the sender (incoming) or a recipient (outgoing)
    lead = match_lead([from_addr] if direction == "in" else to_list) or match_lead([from_addr] + to_list)
    case = suggest_case(lead)
    category = classify(subject, body, atts, from_addr)
    workspace = lead.workspace if lead else default_workspace

    # checks that apply to every upload (real content matches extension, antivirus when configured)
    from . import uploads
    clean_atts, skipped = [], 0
    for a in atts:
        f = ContentFile(a["data"], name=a["name"])
        try:
            uploads.validate_upload(f)
            clean_atts.append(a)
        except uploads.UploadRejected:
            skipped += 1

    cats = [category_for_name(a["name"], subject) for a in clean_atts]
    hints = []
    if case:
        filled = would_fill(case, cats)
        if filled:
            hints.append({"type": "fills_missing", "labels": filled})
    if skipped:
        hints.append({"type": "skipped_attachments", "count": skipped})

    rec = EmailMessage.objects.create(
        message_id=mid, workspace=workspace, lead=lead, case=case, direction=direction, from_addr=from_addr,
        to_addrs=", ".join(to_list)[:1000], subject=subject, body=body, sent_at=sent_at, category=category, hints=hints)
    for a, c in zip(clean_atts, cats):
        att = EmailAttachment(email=rec, original_name=a["name"], size=len(a["data"]), suggested_category=c)
        att.file.save(a["name"], ContentFile(a["data"]), save=False)
        att.save()
    return rec, True


def mail_configured():
    return bool(getattr(settings, "MAIL_IMAP_USER", "") and getattr(settings, "MAIL_IMAP_PASSWORD", ""))


def fetch_imap(days=14, limit=200, default_workspace="finance"):
    """Pull recent messages from the business mailbox. Read-only (BODY.PEEK): nothing is marked read or deleted there."""
    if not mail_configured():
        raise RuntimeError("תיבת המייל לא הוגדרה (MAIL_IMAP_USER ו-MAIL_IMAP_PASSWORD).")
    since = (timezone.now() - timedelta(days=days)).strftime("%d-%b-%Y")
    created = seen = 0
    box = imaplib.IMAP4_SSL(settings.MAIL_IMAP_HOST, settings.MAIL_IMAP_PORT)
    try:
        box.login(settings.MAIL_IMAP_USER, settings.MAIL_IMAP_PASSWORD)
        box.select(settings.MAIL_IMAP_FOLDER, readonly=True)
        status, data = box.search(None, "SINCE", since)
        ids = (data[0] or b"").split()[-limit:]
        for num in ids:
            status, parts = box.fetch(num, "(BODY.PEEK[])")
            if status != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            _, is_new = ingest_bytes(parts[0][1], default_workspace)
            created += is_new
            seen += 1
    finally:
        try:
            box.logout()
        except Exception:
            pass
    return {"seen": seen, "created": created}
