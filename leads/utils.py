import re
from datetime import datetime
from urllib.parse import quote


def valid_israeli_id(value: str) -> bool:
    s = (value or "").strip().zfill(9)
    if len(s) != 9 or not s.isdigit():
        return False
    total = 0
    for i, ch in enumerate(s):
        n = int(ch) * (1 if i % 2 == 0 else 2)
        if n > 9:
            n -= 9
        total += n
    return total % 10 == 0


def normalize_phone(value) -> str:
    if value is None:
        return ""
    d = re.sub(r"\D", "", str(value))
    if d.startswith("972"):
        d = "0" + d[3:]
    if len(d) == 9 and not d.startswith("0"):
        d = "0" + d
    return d if len(d) >= 9 else ""


def whatsapp_link(phone: str, text: str = "") -> str:
    p = normalize_phone(phone)
    if not p:
        return ""
    url = "https://wa.me/972" + p[1:]
    if text:
        url += "?text=" + quote(text)
    return url


def parse_date(value):
    if not value:
        return None
    s = str(value).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None
