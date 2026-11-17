"""Upload checks: the file's real content must match its extension; optional ClamAV scan."""
import os
import socket
import struct
import zipfile

from django.conf import settings

MAX_UNZIPPED = 120 * 1024 * 1024
OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


class UploadRejected(Exception):
    pass


def _zip_ok(f, required_prefix):
    try:
        with zipfile.ZipFile(f) as z:
            names = z.namelist()
            if sum(i.file_size for i in z.infolist()) > MAX_UNZIPPED:
                raise UploadRejected("הקובץ דחוס בצורה חשודה.")
            if not any(n.startswith(required_prefix) for n in names):
                raise UploadRejected("תוכן הקובץ לא תואם לסוג שלו.")
            if any("vbaproject" in n.lower() for n in names):
                raise UploadRejected("קבצים עם מאקרו אינם מותרים.")
    except zipfile.BadZipFile:
        raise UploadRejected("הקובץ פגום או לא תואם לסוג שלו.")


def check_content(f, filename):
    ext = os.path.splitext(filename)[1].lower()
    f.seek(0)
    head = f.read(16)
    f.seek(0)
    if ext == ".pdf":
        ok = head.startswith(b"%PDF-")
    elif ext in (".jpg", ".jpeg"):
        ok = head.startswith(b"\xff\xd8\xff")
    elif ext == ".png":
        ok = head.startswith(b"\x89PNG\r\n\x1a\n")
    elif ext == ".heic":
        ok = head[4:8] == b"ftyp"
    elif ext in (".doc", ".xls"):
        ok = head.startswith(OLE)
    elif ext == ".docx":
        ok = head.startswith(b"PK")
        if ok:
            _zip_ok(f, "word/")
    elif ext == ".xlsx":
        ok = head.startswith(b"PK")
        if ok:
            _zip_ok(f, "xl/")
    else:
        ok = False
    f.seek(0)
    if not ok:
        raise UploadRejected("תוכן הקובץ לא תואם לסוג שלו.")


def av_scan(f):
    """Scan with ClamAV (clamd over TCP) when CLAMAV_HOST is set. Fails closed if it is set but unreachable."""
    host = getattr(settings, "CLAMAV_HOST", "")
    if not host:
        return
    port = getattr(settings, "CLAMAV_PORT", 3310)
    try:
        with socket.create_connection((host, port), timeout=20) as s:
            s.sendall(b"zINSTREAM\0")
            f.seek(0)
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                s.sendall(struct.pack(">I", len(chunk)) + chunk)
            s.sendall(struct.pack(">I", 0))
            resp = b""
            while not resp.endswith(b"\0") and len(resp) < 4096:
                part = s.recv(1024)
                if not part:
                    break
                resp += part
    except OSError:
        raise UploadRejected("שירות סריקת הוירוסים לא זמין, ההעלאה נחסמה זמנית.")
    finally:
        f.seek(0)
    if b"FOUND" in resp:
        raise UploadRejected("נמצא קוד זדוני בקובץ. ההעלאה נחסמה.")
    if b"OK" not in resp:
        raise UploadRejected("סריקת הוירוסים נכשלה, ההעלאה נחסמה.")


def validate_upload(f):
    check_content(f, f.name)
    av_scan(f)
