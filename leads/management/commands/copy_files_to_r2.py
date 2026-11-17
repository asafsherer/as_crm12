from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from leads.storage import R2EncryptedStorage


class Command(BaseCommand):
    help = "Copies the files already stored on this server's disk (already encrypted) to the R2 bucket. Run once after setting the R2_* variables."

    def handle(self, *args, **opts):
        if not settings.USE_R2:
            raise CommandError("הגדר את R2_ENDPOINT, R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY קודם.")
        store, n = R2EncryptedStorage(), 0
        for path in settings.MEDIA_ROOT.rglob("*"):
            if path.is_file():
                key = path.relative_to(settings.MEDIA_ROOT).as_posix()
                store.client.put_object(Bucket=store.bucket, Key=key, Body=path.read_bytes())  # already ciphertext
                n += 1
        self.stdout.write(f"הועתקו {n} קבצים. אחרי בדיקה אפשר למחוק את התיקייה המקומית.")
