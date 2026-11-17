from django.core.management.base import BaseCommand

from leads import agents
from leads.models import Case, UserProfile


class Command(BaseCommand):
    help = "Run the ops manager (reviewer) and business manager for every workspace. Schedule it daily (cron / Railway cron)."

    def add_arguments(self, parser):
        parser.add_argument("--ops", action="store_true", help="also run the ops clerk on every open case with client AI consent")

    def handle(self, *args, **opts):
        for ws in UserProfile.Workspace.values:
            r = agents.run_reviewer(ws, None)
            b = agents.run_business(ws, None)
            self.stdout.write(f"{ws}: reviewer ({'AI' if r.used_ai else 'code'}), business ({'AI' if b.used_ai else 'code'})")
            if opts["ops"]:
                for case in Case.objects.filter(workspace=ws, stage__in=agents.OPEN_STAGES, ai_consent=True).select_related("lead"):
                    agents.run_ops(case, None)
