import pandas as pd
from django.db import transaction
from django.db.models import Count

from .crypto import nid_hash
from .models import Lead
from .utils import normalize_phone, parse_date, valid_israeli_id

COLUMNS = {
    "Client_SSN": "national_id",
    "Client_FName": "first_name",
    "Client_LName": "last_name",
    "Client_Birth_Day": "birth_date",
    "Client_Age": "age",
    "Client_Gender": "gender",
    "Client_City": "city",
    "Client_Address": "street",
    "Client_Address_Num": "house_number",
    "Client_Zip": "zip_code",
    "Client_Phone": "phone",
    "Client_Phone_Sec": "phone2",
    "Client_Email": "email",
    "Lead_Source": "source",
}
EMPTY = {"", "nan", "none", "@", "-", "nat"}


def _clean(v):
    if v is None:
        return None
    s = str(v).strip()
    return None if s.lower() in EMPTY else s


def _read_rows(file):
    df = pd.read_excel(file, dtype=str)
    missing = [c for c in ("Client_SSN",) if c not in df.columns]
    if missing:
        raise ValueError("בקובץ חסרה העמודה Client_SSN (תעודת זהות).")
    merged = {}
    for rec in df.to_dict("records"):
        data = {}
        for src, dst in COLUMNS.items():
            data[dst] = _clean(rec.get(src))
        if not data["national_id"]:
            continue
        data["national_id"] = "".join(ch for ch in data["national_id"] if ch.isdigit()).zfill(9)[-9:]
        if not data["first_name"] and not data["last_name"]:
            full = _clean(rec.get("Client Name")) or ""
            parts = full.split(" ", 1)
            data["first_name"] = parts[0] if parts and parts[0] else None
            data["last_name"] = parts[1] if len(parts) > 1 else None
        cur = merged.setdefault(data["national_id"], {})
        for k, v in data.items():
            if v and not cur.get(k):
                cur[k] = v
    return merged, len(df)


def import_leads(file, workspace="finance", assigned_to=None):
    merged, total_rows = _read_rows(file)
    hashes = {nid: nid_hash(nid) for nid in merged}
    existing = {}
    for l in Lead.objects.filter(workspace=workspace, national_id_hash__in=list(hashes.values())):
        existing[l.national_id] = l
    to_create, to_update = [], []

    for nid, d in merged.items():
        phone = normalize_phone(d.get("phone"))
        phone2 = normalize_phone(d.get("phone2"))
        try:
            age = int(float(d["age"])) if d.get("age") else None
        except ValueError:
            age = None
        values = {
            "first_name": d.get("first_name") or "",
            "last_name": d.get("last_name") or "",
            "birth_date": parse_date(d.get("birth_date")),
            "age": age,
            "gender": d.get("gender") or "",
            "city": d.get("city") or "",
            "street": d.get("street") or "",
            "house_number": d.get("house_number") or "",
            "zip_code": d.get("zip_code") or "",
            "phone": phone,
            "phone2": phone2,
            "email": d.get("email") or "",
            "source": (d.get("source") or "")[:60],
        }
        if "@" not in values["email"]:
            values["email"] = ""
        lead = existing.get(nid)
        if lead is None:
            to_create.append(Lead(national_id=nid, national_id_hash=hashes[nid], id_valid=valid_israeli_id(nid), workspace=workspace, assigned_to=assigned_to, **values))
        else:
            changed = False
            for k, v in values.items():
                if v and not getattr(lead, k):
                    setattr(lead, k, v)
                    changed = True
            if changed:
                to_update.append(lead)

    with transaction.atomic():
        Lead.objects.bulk_create(to_create, batch_size=500)
        if to_update:
            Lead.objects.bulk_update(
                to_update,
                ["first_name", "last_name", "birth_date", "age", "gender", "city", "street",
                 "house_number", "zip_code", "phone", "phone2", "email", "source"],
                batch_size=500,
            )
        mark_shared_phones(workspace)

    return {
        "rows": total_rows,
        "unique": len(merged),
        "created": len(to_create),
        "updated": len(to_update),
        "duplicates_removed": total_rows - len(merged),
        "no_phone": Lead.objects.filter(workspace=workspace, phone="", phone2="").count(),
        "invalid_id": Lead.objects.filter(workspace=workspace, id_valid=False).count(),
    }


def mark_shared_phones(workspace="finance"):
    counts = {}
    for lid, p1, p2 in Lead.objects.filter(workspace=workspace).values_list("id", "phone", "phone2"):
        for p in {p1, p2}:
            if p:
                counts.setdefault(p, []).append(lid)
    shared_ids = {i for ids in counts.values() if len(ids) > 1 for i in ids}
    Lead.objects.filter(workspace=workspace).update(shared_phone=False)
    Lead.objects.filter(id__in=shared_ids).update(shared_phone=True)
