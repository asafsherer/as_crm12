from django.core.management.base import BaseCommand

from leads import mailbox


class Command(BaseCommand):
    help = "Pull recent mail from the business mailbox (read-only) into the CRM. Schedule it every 10-15 minutes."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=14)
        parser.add_argument("--workspace", default="finance", help="workspace for mail from unknown senders")

    def handle(self, *args, **opts):
        r = mailbox.fetch_imap(days=opts["days"], default_workspace=opts["workspace"])
        self.stdout.write(f"נבדקו {r['seen']} מיילים, {r['created']} חדשים.")
