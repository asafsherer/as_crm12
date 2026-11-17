"""Screens for the AI workers. Workers only propose; people decide and act."""
from datetime import date
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import agents, ai
from .models import AgentRun, Case, Lead, MarketingCost, Note, Task, UserProfile
from .perms import case_access, leads_for, own_cases, own_leads, prof
from .utils import whatsapp_link
from .views import audit, manager_required

A = AgentRun.Agent


def _throttled(key, seconds):
    """One run per key per `seconds`: protects the API bill from double clicks."""
    if cache.get(key):
        return True
    cache.set(key, 1, seconds)
    return False


def _no_sales(request):
    return prof(request.user).role == UserProfile.Role.SALES


@login_required
def agents_home(request):
    if _no_sales(request):
        messages.error(request, "המסך הזה זמין ליועצים ולמנהלים.")
        return redirect("home")
    p = prof(request.user)
    ctx = {"ai_enabled": ai.enabled()}
    if p.is_manager:
        ctx["review"] = AgentRun.objects.filter(agent=A.REVIEWER, workspace=p.workspace).first()
        ctx["biz"] = AgentRun.objects.filter(agent=A.BUSINESS, workspace=p.workspace).first()
        ctx["ops_runs"] = AgentRun.objects.filter(agent=A.OPS, workspace=p.workspace).select_related("case__lead")[:15]
    else:
        ctx["ops_runs"] = AgentRun.objects.filter(agent=A.OPS, case__in=own_cases(request.user)).select_related("case__lead")[:15]
    return render(request, "leads/agents.html", ctx)


@manager_required
@require_POST
def review_run(request):
    p = prof(request.user)
    if _throttled(f"agent:review:{p.workspace}", 30):
        messages.error(request, "הבדיקה הורצה הרגע. נסה שוב בעוד חצי דקה.")
    else:
        run = agents.run_reviewer(p.workspace, request.user)
        audit(request.user, "agent_review", f"הורץ מנהל תפעול ({'AI' if run.used_ai else 'קוד בלבד'})")
        messages.success(request, "הבדיקה הושלמה.")
        if run.error:
            messages.error(request, "ה-AI לא זמין כרגע, והסיכום נכתב בקוד בלבד: " + run.error)
    return redirect("agents")


@manager_required
def business(request):
    p = prof(request.user)
    today = date.today()
    run = AgentRun.objects.filter(agent=A.BUSINESS, workspace=p.workspace).first()
    trend = []
    if run:
        rows = run.facts.get("trend", [])
        top = max([r["revenue"] or 0 for r in rows] + [1])
        trend = [{**r, "h": round((r["revenue"] or 0) * 100 / top)} for r in rows]
    return render(request, "leads/business.html", {
        "run": run, "facts": run.facts if run else None, "trend": trend, "ai_enabled": ai.enabled(),
        "costs": MarketingCost.objects.filter(workspace=p.workspace)[:18], "this_month": today.strftime("%Y-%m"),
    })


@manager_required
@require_POST
def business_run(request):
    p = prof(request.user)
    try:
        days = int(request.POST.get("days", 90))
    except ValueError:
        days = 90
    days = days if days in (30, 90, 365) else 90
    target = None
    raw = request.POST.get("target", "").replace(",", "").replace("₪", "").strip()
    if raw:
        try:
            target = Decimal(raw)
            if target <= 0 or target > Decimal("100000000"):
                target = None
        except InvalidOperation:
            target = None
    if _throttled(f"agent:biz:{p.workspace}", 30):
        messages.error(request, "הניתוח הורץ הרגע. נסה שוב בעוד חצי דקה.")
    else:
        run = agents.run_business(p.workspace, request.user, window_days=days, target_override=target)
        audit(request.user, "agent_business", f"הורץ מנהל עסקי ({days} ימים, {'AI' if run.used_ai else 'קוד בלבד'})")
        messages.success(request, "הניתוח העסקי הושלם.")
        if run.error:
            messages.error(request, "ה-AI לא זמין כרגע, ומוצגים רק החישובים: " + run.error)
    return redirect("business")


@manager_required
@require_POST
def cost_save(request):
    p = prof(request.user)
    try:
        month = date.fromisoformat((request.POST.get("month", "") + "-01")[:10])
        amount = Decimal(request.POST.get("amount", "").replace(",", "").strip())
    except (ValueError, InvalidOperation):
        messages.error(request, "חודש או סכום לא תקינים.")
        return redirect("business")
    source = request.POST.get("source", "").strip()[:60] or agents.UNKNOWN_SOURCE
    if amount < 0 or amount > Decimal("100000000"):
        messages.error(request, "סכום לא סביר.")
        return redirect("business")
    MarketingCost.objects.update_or_create(workspace=p.workspace, month=month, source=source,
                                           defaults={"amount": amount, "created_by": request.user})
    audit(request.user, "cost_save", f"עלות שיווק: {source} {month:%m/%Y}")
    messages.success(request, "העלות נשמרה.")
    return redirect("business")


@manager_required
@require_POST
def cost_delete(request, pk):
    row = get_object_or_404(MarketingCost, pk=pk, workspace=prof(request.user).workspace)
    row.delete()
    audit(request.user, "cost_delete", "נמחקה עלות שיווק")
    return redirect("business")


@login_required
def case_ops(request, pk):
    case = get_object_or_404(Case.objects.select_related("lead"), pk=pk)
    if case_access(request.user, case) != "own":
        raise Http404
    if request.method == "POST":
        if _throttled(f"agent:ops:{case.pk}", 20):
            messages.error(request, "המתפעלת הורצה הרגע. נסה שוב בעוד רגע.")
        else:
            run = agents.run_ops(case, request.user)
            audit(request.user, "agent_ops", f"הורצה מתפעלת בתיק {case.pk} ({'AI' if run.used_ai else 'קוד בלבד'})")
            messages.success(request, "הדוח עודכן.")
            if run.error:
                messages.error(request, run.error)
        return redirect("case_ops", pk=case.pk)
    run = case.agent_runs.filter(agent=A.OPS).first()
    text = wa = ""
    if run and run.output.get("message"):
        name = (case.lead.first_name or "").strip()
        text = run.output["message"].replace(agents.NAME_TOKEN, name) if name else run.output["message"].replace(" " + agents.NAME_TOKEN, "")
        wa = whatsapp_link(case.lead.best_phone, text)
    return render(request, "leads/case_ops.html", {
        "case": case, "run": run, "message_text": text, "wa_url": wa, "ai_enabled": ai.enabled(),
        "clerk_notes": agents.doc_notes(case),
        "outdated": bool(run and case.updated_at > run.created_at),
    })


# ---------- campaigner ----------
@login_required
def campaigner(request):
    if _no_sales(request):
        messages.error(request, "המסך הזה זמין ליועצים ולמנהלים.")
        return redirect("home")
    p = prof(request.user)
    run = AgentRun.objects.filter(agent=A.CAMPAIGN, workspace=p.workspace).first()
    rows = []
    if run:
        ids = [r["lead_id"] for r in run.output.get("rows", [])]
        leads = {l.pk: l for l in own_leads(request.user).filter(pk__in=ids)}
        for r in run.output.get("rows", []):
            l = leads.get(r["lead_id"])
            if l:
                rows.append({**r, "lead": l, "interest_label": agents.INTEREST_LABEL.get(r["interest"], "לא ידוע"),
                             "tier_label": agents.TIER_LABEL[r["tier"]]})
    new_leads = own_leads(request.user).filter(intake_at__isnull=False, status=Lead.Status.NEW).count()
    return render(request, "leads/campaigner.html", {
        "run": run, "rows": rows, "ai_enabled": ai.enabled(), "new_leads": new_leads,
        "intake_on": bool(settings.LEAD_INTAKE_TOKEN), "tier_labels": agents.TIER_LABEL,
    })


@login_required
@require_POST
def campaigner_run(request):
    if _no_sales(request):
        return redirect("home")
    p = prof(request.user)
    try:
        days = int(request.POST.get("days", 30))
    except ValueError:
        days = 30
    days = days if days in (7, 30, 90) else 30
    if _throttled(f"agent:campaign:{request.user.pk}", 20):
        messages.error(request, "הקמפיינר הורץ הרגע. נסה שוב בעוד רגע.")
    else:
        run = agents.run_campaigner(p.workspace, request.user, own_leads(request.user), days=days)
        audit(request.user, "agent_campaign", f"הורץ קמפיינר ({days} ימים, {'AI' if run.used_ai else 'קוד בלבד'})")
        messages.success(request, "הקמפיינר סיים לעבור על הלידים.")
        if run.error:
            messages.error(request, "ה-AI לא זמין כרגע והסיכום נכתב בקוד בלבד: " + run.error)
    return redirect("campaigner")


@login_required
@require_POST
def campaigner_apply(request):
    """A person approves the proposals: store the scores on the leads and (optionally) open call tasks for the hot new ones."""
    if _no_sales(request):
        return redirect("home")
    p = prof(request.user)
    run = AgentRun.objects.filter(agent=A.CAMPAIGN, workspace=p.workspace).first()
    if not run:
        return redirect("campaigner")
    rows = {r["lead_id"]: r for r in run.output.get("rows", [])}
    leads = own_leads(request.user).filter(pk__in=rows.keys())
    scored = tasks = 0
    for l in leads:
        r = rows[l.pk]
        if l.lead_score != r["score"] or l.interest != r["interest"]:
            l.lead_score, l.interest = r["score"], r["interest"]
            l.save(update_fields=["lead_score", "interest", "updated_at"])
            scored += 1
        if request.POST.get("tasks") and r["tier"] == "hot" and l.status == Lead.Status.NEW:
            owner = l.assigned_to or request.user
            title = f"להתקשר לליד חם: {l.full_name}"
            if not Task.objects.filter(owner=owner, lead=l, title=title, done=False).exists():
                Task.objects.create(owner=owner, lead=l, title=title, due_on=date.today(), priority=Task.Priority.URGENT)
                tasks += 1
    audit(request.user, "campaign_apply", f"הוחלו ניקודים על {scored} לידים, נפתחו {tasks} משימות")
    messages.success(request, f"הניקוד נשמר ב-{scored} לידים" + (f", ונפתחו {tasks} משימות חזרה." if tasks else "."))
    return redirect("campaigner")


@login_required
def campaigner_message(request, pk):
    """WhatsApp link with a first-contact draft (code template, matched to what the lead asked about). A person sends it."""
    lead = get_object_or_404(leads_for(request.user), pk=pk)
    interest = lead.interest or agents.detect_interest(lead.form_message, lead.landing_page, lead.utm_term, lead.campaign)
    text = agents.FIRST_MESSAGE.get(interest, agents.FIRST_MESSAGE[""])
    first = (lead.first_name or "").strip()
    text = text.replace(agents.NAME_TOKEN, first) if first else text.replace(" " + agents.NAME_TOKEN, "")
    url = whatsapp_link(lead.best_phone, text)
    if not url:
        messages.error(request, "אין טלפון תקין לליד הזה.")
        return redirect("campaigner")
    Note.objects.create(lead=lead, author=request.user, text="נפתחה הודעת וואטסאפ ראשונה (טיוטת הקמפיינר)")
    return redirect(url)
