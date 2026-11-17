import gzip

from django.core.management.base import BaseCommand

from leads.crypto import decrypt_bytes


class Command(BaseCommand):
    help = "Decrypt a backup file to a .json you can load with: python manage.py loaddata file.json"

    def add_arguments(self, parser):
        parser.add_argument("source")
        parser.add_argument("target")

    def handle(self, *args, **opts):
        data = gzip.decompress(decrypt_bytes(open(opts["source"], "rb").read()))
        open(opts["target"], "wb").write(data)
        self.stdout.write("Decrypted. Delete the target file after loading it: it contains readable personal data.")
