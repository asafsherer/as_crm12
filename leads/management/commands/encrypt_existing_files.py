from django.conf import settings
from django.core.management.base import BaseCommand

from leads.crypto import FILE_MAGIC, encrypt_bytes


class Command(BaseCommand):
    help = "Encrypt, in place, any stored files that were uploaded before encryption was switched on. Safe to run again."

    def handle(self, *args, **opts):
        done = skipped = 0
        for path in settings.MEDIA_ROOT.rglob("*"):
            if not path.is_file():
                continue
            raw = path.read_bytes()
            if raw.startswith(FILE_MAGIC):
                skipped += 1
                continue
            path.write_bytes(encrypt_bytes(raw))
            done += 1
        self.stdout.write(f"Encrypted {done} file(s); {skipped} already encrypted.")
