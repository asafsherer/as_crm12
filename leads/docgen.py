"""Fill .docx templates: placeholders look like {{שם_לקוח}} and are replaced with case data."""
import io
import re
from datetime import date

from docx import Document as Docx
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

PLACEHOLDER = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")

FIELDS = [
    ("שם_לקוח", "שם מלא"), ("שם_פרטי", "שם פרטי"), ("שם_משפחה", "שם משפחה"), ("תעודת_זהות", "ת״ז"),
    ("טלפון", "טלפון"), ("אימייל", "אימייל"), ("כתובת", "כתובת מלאה"), ("עיר", "עיר"),
    ("סוג_תיק", "סוג התיק"), ("סכום", "סכום העסקה"), ("עלות_תיק", "עלות תיק / עמלה"),
    ("הכנסה_חודשית", "הכנסה חודשית"), ("מספר_תיק", "מספר תיק הוצל״פ / אסמכתא"),
    ("תאריך_היום", "התאריך של היום"), ("שם_מטפל", "שם המטפל בתיק"),
]


def _money(v):
    return f"{int(v):,} ₪" if v not in (None, "") else ""


def case_context(case, user):
    lead = case.lead
    return {
        "שם_לקוח": lead.full_name, "שם_פרטי": lead.first_name, "שם_משפחה": lead.last_name,
        "תעודת_זהות": lead.national_id, "טלפון": lead.best_phone, "אימייל": lead.email,
        "כתובת": lead.address, "עיר": lead.city, "סוג_תיק": case.get_case_type_display(),
        "סכום": _money(case.amount), "עלות_תיק": _money(case.fee), "הכנסה_חודשית": _money(case.monthly_income),
        "מספר_תיק": case.file_number, "תאריך_היום": date.today().strftime("%d/%m/%Y"),
        "שם_מטפל": (user.get_full_name() or user.get_username()),
    }


def _replace_in_paragraph(par, ctx):
    full = "".join(r.text for r in par.runs)
    if "{{" not in full:
        return
    new = PLACEHOLDER.sub(lambda m: str(ctx.get(m.group(1), m.group(0))), full)
    if not par.runs:
        return
    par.runs[0].text = new
    for r in par.runs[1:]:
        r.text = ""


def _iter_paragraphs(doc):
    for p in doc.paragraphs:
        yield p
    for t in doc.tables:
        for row in t.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    yield p
    for sec in doc.sections:
        for part in (sec.header, sec.footer):
            for p in part.paragraphs:
                yield p


def fill_template(fileobj, ctx) -> bytes:
    doc = Docx(fileobj)
    for p in _iter_paragraphs(doc):
        _replace_in_paragraph(p, ctx)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def placeholders_in(fileobj):
    doc = Docx(fileobj)
    found = set()
    for p in _iter_paragraphs(doc):
        found.update(PLACEHOLDER.findall("".join(r.text for r in p.runs)))
    return found


def sample_template() -> bytes:
    """A small right-to-left example contract so users can see how placeholders look."""
    doc = Docx()

    def rtl(par):
        ppr = par._p.get_or_add_pPr()
        bidi = OxmlElement("w:bidi")
        ppr.append(bidi)
        par.alignment = 0
        for r in par.runs:
            rpr = r._r.get_or_add_rPr()
            rpr.append(OxmlElement("w:rtl"))

    lines = [
        ("הסכם שירותים", True),
        ("תאריך: {{תאריך_היום}}", False),
        ("בין A.S Finances לבין {{שם_לקוח}}, ת״ז {{תעודת_זהות}}, כתובת: {{כתובת}}, טלפון: {{טלפון}}.", False),
        ("מהות השירות: {{סוג_תיק}}.", False),
        ("סכום העסקה: {{סכום}}. עלות הטיפול בתיק: {{עלות_תיק}}.", False),
        ("מספר תיק / אסמכתא: {{מספר_תיק}}.", False),
        ("הלקוח מאשר כי קרא והבין את תנאי ההסכם.", False),
        ("חתימת הלקוח: ______________        חתימת המטפל ({{שם_מטפל}}): ______________", False),
    ]
    for text, bold in lines:
        p = doc.add_paragraph()
        run = p.add_run(text)
        run.bold = bold
        rtl(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
