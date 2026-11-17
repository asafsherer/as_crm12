from .models import EmailMessage, Lead, Message, Task


def crm(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    from .perms import prof
    p = prof(user)
    from .perms import mail_for, own_leads
    extra = {}
    if p.role != "sales":
        extra["new_mail"] = mail_for(user).filter(status=EmailMessage.Status.NEW).count()
        extra["new_web_leads"] = own_leads(user).filter(intake_at__isnull=False, status=Lead.Status.NEW).count()
    return {
        **extra,
        "me": p,
        "pii_ok": p.pii_ok,
        "is_manager": p.is_manager,
        "unread_messages": Message.objects.filter(recipient=user, read_at__isnull=True).count(),
        "open_tasks": Task.objects.filter(owner=user, done=False).count(),
    }
