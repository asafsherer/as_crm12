from django.core.management.base import BaseCommand
from leads.importer import import_leads


class Command(BaseCommand):
    help = "Import leads from an Excel file"

    def add_arguments(self, parser):
        parser.add_argument("path")

    def handle(self, *args, **opts):
        result = import_leads(opts["path"])
        self.stdout.write(str(result))
