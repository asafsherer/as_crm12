import gzip
import io
from datetime import datetime

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand

from leads.crypto import encrypt_bytes


class Command(BaseCommand):
    help = "Dump all data to an ENCRYPTED file (R2 bucket under backups/ when R2 is configured, else BACKUP_DIR). Keeps the latest 14."

    def handle(self, *args, **opts):
        buf = io.StringIO()
        call_command("dumpdata", "--natural-foreign", "--natural-primary",
                     "-e", "contenttypes", "-e", "sessions", "-e", "admin.logentry", "-e", "leads.auditlog",
                     "--indent", "1", stdout=buf)
        blob = encrypt_bytes(gzip.compress(buf.getvalue().encode("utf-8")))
        fname = f"backup-{datetime.now():%Y%m%d-%H%M%S}.json.gz.enc"
        if settings.USE_R2:
            from leads.storage import R2EncryptedStorage
            st = R2EncryptedStorage()
            st.client.put_object(Bucket=st.bucket, Key=f"backups/{fname}", Body=blob)
            keys = []
            for page in st.client.get_paginator("list_objects_v2").paginate(Bucket=st.bucket, Prefix="backups/backup-"):
                keys += [o["Key"] for o in page.get("Contents", [])]
            for old in sorted(keys)[:-14]:
                st.client.delete_object(Bucket=st.bucket, Key=old)
            self.stdout.write(f"Encrypted backup saved to R2: backups/{fname}")
            return
        settings.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        name = settings.BACKUP_DIR / fname
        name.write_bytes(blob)
        for old in sorted(settings.BACKUP_DIR.glob("backup-*.json.gz.enc"))[:-14]:
            old.unlink()
        self.stdout.write(f"Encrypted backup saved: {name}")
