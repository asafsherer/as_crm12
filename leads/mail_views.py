"""Mail screens: inbox, one message, filing attachments into a case, routing to another person, the mail worker."""
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import agents, mailbox
from .models import AgentRun, Case, Document, EmailMessage, Lead, Note, UserProfile
from .perms import case_access, leads_for, mail_for, own_cases, prof
from .views import audit, manager_required

User = get_user_model()
MAX_EML = 25 * 1024 * 1024


def _no_sales(view):
    from functools import wraps

    @wraps(view)
    @login_required
    def inner(request, *a, **k):
        if prof(request.user).role == UserProfile.Role.SALES:
            messages.error(request, "המסך הזה זמין ליועצים ולמנהלים.")
            return redirect("home")
        return view(request, *a, **k)
    return inner


def _throttled(key, seconds):
    if cache.get(key):
        return True
    cache.set(key, 1, seconds)
    return False


@_no_sales
def mail_home(request):
    p = prof(request.user)
    qs = mail_for(request.user).select_related("lead", "case").prefetch_related("attachments")
    tab = request.GET.get("tab", "new")
    q = request.GET.get("q", "").strip()
    base = qs
    if tab == "new":
        qs = qs.filter(status=EmailMessage.Status.NEW)
    elif tab == "receipts":
        qs = qs.filter(category=EmailMessage.Category.RECEIPT)
    elif tab == "docs":
        qs = qs.filter(category=EmailMessage.Category.DOCUMENTS)
    elif tab == "unmatched":
        qs = qs.filter(lead__isnull=True)
    else:
        tab = "all"
    if q:
        qs = qs.filter(Q(from_addr__icontains=q) | Q(to_addrs__icontains=q))
    counts = {
        "new": base.filter(status=EmailMessage.Status.NEW).count(),
        "receipts": base.filter(category=EmailMessage.Category.RECEIPT).count(),
        "docs": base.filter(category=EmailMessage.Category.DOCUMENTS).count(),
        "unmatched": base.filter(lead__isnull=True).count(),
        "all": base.count(),
    }
    page = Paginator(qs, 40).get_page(request.GET.get("page"))
    run = AgentRun.objects.filter(agent=AgentRun.Agent.MAIL, workspace=p.workspace).first()
    items = {}
    if run:
        items = {i["email_id"]: i for i in run.output.get("items", [])}
    for m in page:
        m.clerk = items.get(m.pk)
        if m.clerk:
            m.clerk["text"] = agents.MAIL_ACTION_TEXT.get(m.clerk["action"], "")
    return render(request, "leads/mail_home.html", {
        "page": page, "tab": tab, "q": q, "counts": counts, "run": run, "configured": mailbox.mail_configured(),
        "tabs": [("new", "חדשים"), ("docs", "עם מסמכים"), ("receipts", "קבלות"), ("unmatched", "לא משויכים"), ("all", "הכל")],
    })


@_no_sales
@require_POST
def mail_upload(request):
    n_new = n_dup = n_bad = 0
    for f in request.FILES.getlist("eml")[:20]:
        if f.size > MAX_EML or not f.name.lower().endswith(".eml"):
            n_bad += 1
            continue
        try:
            _, created = mailbox.ingest_bytes(f.read(), prof(request.user).workspace)
        except Exception:
            n_bad += 1
            continue
        n_new += created
        n_dup += not created
    audit(request.user, "mail_upload", f"הועלו מיילים: {n_new} חדשים")
    if n_new:
        messages.success(request, f"נוספו {n_new} מיילים.")
    if n_dup:
        messages.info(request, f"{n_dup} מיילים כבר היו במערכת.")
    if n_bad:
        messages.error(request, f"{n_bad} קבצים לא נקראו. יש להעלות קבצי eml בלבד.")
    return redirect("mail_home")


@manager_required
@require_POST
def mail_fetch(request):
    if _throttled("mail:fetch", 60):
        messages.error(request, "המשיכה הורצה הרגע. נסה שוב בעוד דקה.")
        return redirect("mail_home")
    try:
        r = mailbox.fetch_imap(default_workspace=prof(request.user).workspace)
        audit(request.user, "mail_fetch", f"נמשכו מיילים: {r['created']} חדשים")
        messages.success(request, f"נבדקו {r['seen']} מיילים, {r['created']} חדשים.")
    except Exception as exc:
        messages.error(request, "משיכת המיילים נכשלה: " + str(exc)[:200])
    return redirect("mail_home")


@_no_sales
@require_POST
def mail_clerk_run(request):
    p = prof(request.user)
    if _throttled(f"agent:mail:{request.user.pk}", 20):
        messages.error(request, "המתפעל הורץ הרגע.")
    else:
        run = agents.run_mail(p.workspace, request.user, mail_for(request.user))
        audit(request.user, "agent_mail", f"הורץ מתפעל מיילים ({'AI' if run.used_ai else 'קוד בלבד'})")
        messages.success(request, "המתפעל סיים לעבור על המיילים.")
    return redirect("mail_home")


def _get(request, pk):
    return get_object_or_404(mail_for(request.user).select_related("lead", "case"), pk=pk)


@_no_sales
def mail_detail(request, pk):
    m = _get(request, pk)
    p = prof(request.user)
    lead_cases = [c for c in (m.lead.cases.all() if m.lead_id else own_cases(request.user).order_by("-updated_at")[:100])
                  if case_access(request.user, c) == "own"]
    others = User.objects.filter(is_active=True).exclude(pk=request.user.pk).exclude(profile__role="sales").select_related("profile")
    return render(request, "leads/mail_detail.html", {
        "m": m, "atts": list(m.attachments.all()), "cases": lead_cases, "others": others,
        "leads": leads_for(request.user).order_by("last_name", "first_name")[:400] if not m.lead_id else [],
        "categories": [c for c in Document.Category.choices if c[0] != "other"] + [("other", "אחר")],
        "hint_fills": next((h["labels"] for h in m.hints if h.get("type") == "fills_missing"), []),
        "hint_skipped": next((h["count"] for h in m.hints if h.get("type") == "skipped_attachments"), 0),
    })


@_no_sales
@require_POST
def mail_action(request, pk):
    m = _get(request, pk)
    user, action = request.user, request.POST.get("action")
    if action == "file":
        case = get_object_or_404(Case, pk=request.POST.get("case") or 0)
        if case_access(user, case) != "own":
            raise Http404
        made = 0
        for att in m.attachments.all():
            if att.document_id or not request.POST.get(f"use_{att.pk}"):
                continue
            cat = request.POST.get(f"cat_{att.pk}", "other")
            if cat not in Document.Category.values:
                cat = "other"
            with att.file.open("rb") as fh:
                data = fh.read()
            doc = Document(case=case, category=cat, original_name=att.original_name, size=len(data), uploaded_by=user)
            doc.file.save(att.original_name, ContentFile(data), save=False)
            doc.save()
            att.document = doc
            att.save(update_fields=["document"])
            made += 1
        m.case, m.lead, m.status = case, case.lead, EmailMessage.Status.FILED
        m.save(update_fields=["case", "lead", "status"])
        Note.objects.create(lead=case.lead, case=case, author=user, channel=Note.Channel.EMAIL,
                            text=f"מייל נשמר בתיק ({made} מסמכים): {m.subject[:120] or 'ללא נושא'}")
        audit(user, "mail_file", f"מייל {m.pk} נשמר בתיק {case.pk} ({made} מסמכים)")
        messages.success(request, f"נשמרו {made} מסמכים בתיק.")
    elif action == "assign":
        lead = get_object_or_404(leads_for(user), pk=request.POST.get("lead") or 0)
        m.lead, m.workspace = lead, lead.workspace
        m.case = mailbox.suggest_case(lead)
        m.save(update_fields=["lead", "workspace", "case"])
        messages.success(request, "המייל שויך לאיש הקשר.")
    elif action == "route":
        target = get_object_or_404(User.objects.filter(is_active=True).exclude(profile__role="sales"), pk=request.POST.get("user") or 0)
        m.owner, m.moved_by, m.workspace = target, user, prof(target).workspace
        m.save(update_fields=["owner", "moved_by", "workspace"])
        audit(user, "mail_route", f"מייל {m.pk} הועבר אל {target.username}")
        messages.success(request, f"המייל הועבר אל {target.username}.")
        return redirect("mail_home")
    elif action in ("ignore", "reopen"):
        m.status = EmailMessage.Status.IGNORED if action == "ignore" else EmailMessage.Status.NEW
        m.save(update_fields=["status"])
    elif action == "delete":
        for att in m.attachments.filter(document__isnull=True):
            att.file.delete(save=False)
        for att in m.attachments.filter(document__isnull=False):
            att.file.delete(save=False)  # the filed copy in the case stays
        audit(user, "mail_delete", f"נמחק מייל {m.pk}")
        m.delete()
        messages.success(request, "המייל נמחק.")
        return redirect("mail_home")
    else:
        return HttpResponseBadRequest("פעולה לא מוכרת")
    return redirect("mail_detail", pk=m.pk)
