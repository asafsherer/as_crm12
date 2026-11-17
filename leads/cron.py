"""Scheduled jobs for hosts without a cron service (free tier).

A free external pinger (cron-job.org) calls:
  /cron/tick/    every 10 minutes  -> keeps the server awake + pulls new mail
  /cron/daily/   once a day        -> AI managers (run_agents) + encrypted backup
Authentication: CRON_TOKEN (header X-Cron-Token, or ?token=). Empty CRON_TOKEN = switched off (404).
"""
import hmac
import logging
import threading

from django.conf import settings
from django.core.management import call_command
from django.http import Http404, JsonResponse
from django.views.decorators.http import require_http_methods

log = logging.getLogger(__name__)
_lock = threading.Lock()


def _job(task):
    if not _lock.acquire(blocking=False):
        return  # a previous job is still running
    try:
        if task == "tick":
            if settings.MAIL_IMAP_USER and settings.MAIL_IMAP_PASSWORD:
                call_command("fetch_mail", days=2)
        elif task == "daily":
            call_command("run_agents")
            call_command("backup")
    except Exception:
        log.exception("cron task %s failed", task)
    finally:
        _lock.release()


@require_http_methods(["GET", "POST", "HEAD"])
def cron_run(request, task):
    token = settings.CRON_TOKEN
    if not token:
        raise Http404
    supplied = request.headers.get("X-Cron-Token", "") or request.GET.get("token", "")
    if not hmac.compare_digest(supplied.encode(), token.encode()):
        return JsonResponse({"ok": False}, status=401)
    if task not in ("tick", "daily", "ping"):
        raise Http404
    if task != "ping":
        threading.Thread(target=_job, args=(task,), daemon=True).start()
    return JsonResponse({"ok": True, "task": task})
