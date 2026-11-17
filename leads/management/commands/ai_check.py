from django.core.management.base import BaseCommand

from leads import ai


class Command(BaseCommand):
    help = "Check that the AI key works (sends one tiny request)."

    def handle(self, *args, **opts):
        if not ai.enabled():
            self.stdout.write("NO KEY: create a file named ai_key.txt next to manage.py containing only your API key.")
            return
        try:
            data = ai._parse_json(ai._call([{"type": "text", "text": 'Reply with exactly this JSON: {"ok": true}'}],
                                           "Return only JSON.", max_tokens=50))
        except ai.AIError as exc:
            self.stdout.write(f"FAILED: {exc}")
            return
        self.stdout.write("OK: the AI connection works." if data.get("ok") else f"Unexpected reply: {data}")
