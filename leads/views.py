import io
import json
import os
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps

import segno
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, OuterRef, Q, Subquery, Sum
from django.db.models.functions import TruncMonth, TruncWeek
from django.http import FileResponse, Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from . import agents, ai, doc_rules, docgen, totp
from . import statuses as st
from .crypto import nid_hash
from .uploads import UploadRejected, validate_upload
from .importer import import_leads
from .models import (AuditLog, Case, CaseShare, LeadShare, DocTemplate, Document, DocumentAnalysis, Goal, Lead, Message, Note, Task,
                     UserProfile)
from .perms import (can_see_document, can_share_lead, case_access, cases_for, docs_for, leads_for, own_cases, own_leads,
                    prof, shared_leads)
from .utils import valid_israeli_id, normalize_phone, parse_date

User = get_user_model()
S = Lead.Status
MAX_ATTEMPTS = 3


# ---------- helpers ----------
def audit(user, action, detail=""):
    AuditLog.objects.create(user=user if getattr(user, "is_authenticated", False) else None, action=action, detail=detail[:500])


def manager_required(view):
    @wraps(view)
    @login_required
    def inner(request, *args, **kwargs):
        if not prof(request.user).is_manager:
            messages.error(request, "הפעולה הזאת מותרת רק למנהל.")
            return redirect("home")
        return view(request, *args, **kwargs)
    return inner


def _dec(value):
    try:
        v = str(value or "").replace(",", "").replace("₪", "").strip()
        return Decimal(v) if v else None
    except InvalidOperation:
        return None


def _parse_day(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _upload_ok(request, f):
    try:
        validate_upload(f)
    except UploadRejected as exc:
        audit(request.user, "upload_blocked", f"הועלה קובץ שנחסם: {f.name[:80]} ({exc})")
        messages.error(request, str(exc))
        return False
    return True


def _safe_next(request, default):
    target = request.POST.get("next", "")
    return target if url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}) else default


def _has_phone():
    return ~Q(phone="") | ~Q(phone2="")


def _apply_status(lead, status, user, callback_on=None):
    old = st.label(lead.workspace, lead.status)
    lead.status = status
    lead.last_contact = timezone.now()
    if status == S.NO_ANSWER:
        lead.call_attempts += 1
    lead.callback_on = callback_on if status in (S.CALLBACK, S.INTERESTED) else None
    lead.save()
    text = f"{old} ← {st.label(lead.workspace, lead.status)}"
    if lead.callback_on:
        text += f" (חזרה ב-{lead.callback_on:%d/%m/%Y})"
    Note.objects.create(lead=lead, author=user, kind=Note.Kind.STATUS, text=text)


# ---------- login / 2FA ----------
def client_ip(request):
    """Rightmost X-Forwarded-For entry = the one added by our own proxy; the left side can be forged by the client."""
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    parts = [x.strip() for x in xff.split(",") if x.strip()]
    return parts[-1] if parts else request.META.get("REMOTE_ADDR", "")


class ThrottledLoginView(auth_views.LoginView):
    template_name = "leads/login.html"
    LIMIT, WINDOW = 5, 15 * 60      # per account
    IP_LIMIT = 30                   # per address

    def _keys(self, request):
        user = request.POST.get("username", "").lower().strip()[:60]
        return f"login-fail:user:{user}", f"login-fail:ip:{client_ip(request)}"

    def post(self, request, *args, **kwargs):
        ukey, ikey = self._keys(request)
        if cache.get(ukey, 0) >= self.LIMIT or cache.get(ikey, 0) >= self.IP_LIMIT:
            audit(None, "login_blocked", f"נחסמו ניסיונות כניסה: {request.POST.get('username', '')[:60]}")
            form = self.get_form()
            form.add_error(None, "יותר מדי ניסיונות כושלים. נסה שוב בעוד 15 דקות.")
            return self.render_to_response(self.get_context_data(form=form))
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        for key in self._keys(self.request):
            cache.set(key, cache.get(key, 0) + 1, self.WINDOW)
        return super().form_invalid(form)

    def form_valid(self, form):
        cache.delete(self._keys(self.request)[0])
        return super().form_valid(form)


@login_required
def twofa_setup(request):
    p = prof(request.user)
    if p.totp_enabled:
        return redirect("home")
    if not p.totp_secret:
        p.totp_secret = totp.new_secret()
        p.save(update_fields=["totp_secret"])
    if request.method == "POST":
        if totp.verify(p.totp_secret, request.POST.get("code")):
            p.totp_enabled = True
            p.save(update_fields=["totp_enabled"])
            request.session["otp_ok"] = True
            audit(request.user, "2fa_enabled", "הופעלה התחברות דו-שלבית")
            messages.success(request, "ההתחברות הדו-שלבית הופעלה.")
            return redirect("home")
        messages.error(request, "הקוד שגוי. בדוק שהשעה בטלפון מדויקת ונסה שוב.")
    uri = totp.uri(p.totp_secret, request.user.get_username())
    qr = segno.make(uri, error="m").svg_inline(scale=5, dark="#16243B", border=2)
    return render(request, "leads/twofa_setup.html", {"qr": qr, "secret": p.totp_secret})


@login_required
def twofa_verify(request):
    p = prof(request.user)
    if not p.totp_enabled:
        return redirect("twofa_setup")
    if request.session.get("otp_ok"):
        return redirect("home")
    fail_key = f"otp-fail:{request.user.pk}"
    if request.method == "POST":
        if cache.get(fail_key, 0) >= 5:
            audit(request.user, "2fa_blocked", "נחסם אימות דו-שלבי אחרי ניסיונות כושלים")
            from django.contrib.auth import logout
            logout(request)
            messages.error(request, "יותר מדי קודים שגויים. נסה שוב בעוד 15 דקות.")
            return redirect("login")
        if totp.verify(p.totp_secret, request.POST.get("code")):
            request.session["otp_ok"] = True
            cache.delete(fail_key)
            return redirect("home")
        cache.set(fail_key, cache.get(fail_key, 0) + 1, 15 * 60)
        audit(request.user, "2fa_failed", "קוד דו-שלבי שגוי")
        messages.error(request, "הקוד שגוי.")
    return render(request, "leads/twofa_verify.html")


# ---------- today / dashboard ----------
@login_required
def home(request):
    user, p, today = request.user, prof(request.user), date.today()
    leads = own_leads(user)
    tasks = Task.objects.filter(owner=user, done=False).select_related("lead", "case__lead")
    shared = []
    for sh in CaseShare.objects.filter(shared_with=user).select_related("case__lead", "shared_by"):
        since = sh.last_seen_at or sh.created_at
        new = sh.case.notes.filter(created_at__gt=since).exclude(author=user).count()
        shared.append({"share": sh, "case": sh.case, "new": new})
    counts = {r["status"]: r["n"] for r in leads.values("status").annotate(n=Count("id"))}
    cases = own_cases(user)
    now = timezone.localtime()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    closed_now = cases.filter(stage=Case.Stage.CLOSED, closed_at__gte=month_start)
    month_fees = closed_now.aggregate(t=Sum("fee"))["t"] or 0
    month_paid = closed_now.filter(fee_paid=True).aggregate(t=Sum("fee"))["t"] or 0
    expected = cases.exclude(stage=Case.Stage.CANCELLED).filter(fee__isnull=False, fee_paid=False).aggregate(t=Sum("fee"))["t"] or 0
    goal = Goal.objects.filter(workspace=p.workspace, month=month_start.date()).first()
    target = goal.fees_target if goal and goal.fees_target else 0
    hour = now.hour
    greeting = "בוקר טוב" if 5 <= hour < 12 else "צהריים טובים" if 12 <= hour < 17 else "ערב טוב" if 17 <= hour < 21 else "לילה טוב"
    defs_ = st.defs(p.workspace)
    return render(request, "leads/home.html", {
        "greeting": greeting, "person": user.first_name or user.get_username(), "month_fees": month_fees, "month_paid": month_paid,
        "month_target": target, "month_pct": min(100, round(float(month_paid) * 100 / float(target))) if target else None,
        "expected": expected, "money_label": "שכר טרחה" if p.workspace == "legal" else "עמלות",
        "month_cases": closed_now.count(),
        "chips": [{"def": d, "count": counts.get(d.key, 0)} for d in defs_],
        "new_web": leads.filter(intake_at__isnull=False, status=S.NEW).count(),
        "tasks_due": tasks.filter(due_on__lte=today),
        "tasks_later": tasks.exclude(due_on__lte=today)[:8],
        "today": today,
        "callbacks": leads.filter(callback_on__lte=today, status__in=[S.CALLBACK, S.INTERESTED]).count(),
        "shared": shared,
        "stats": [(d.label, counts.get(d.key, 0), d.key) for d in defs_],
        "total": leads.count(),
        "no_phone": leads.filter(phone="", phone2="").count(),
        "unassigned": Lead.objects.filter(workspace=p.workspace, assigned_to__isnull=True).count() if p.is_manager else 0,
        "open_cases": cases.exclude(stage__in=[Case.Stage.CLOSED, Case.Stage.CANCELLED]).count(),
        "closed_cases": cases.filter(stage=Case.Stage.CLOSED).count(),
        "fees_total": cases.filter(stage=Case.Stage.CLOSED).aggregate(t=Sum("fee"))["t"] or 0,
        "fees_unpaid": cases.filter(stage=Case.Stage.CLOSED, fee_paid=False).aggregate(t=Sum("fee"))["t"] or 0,
        "recent": Note.objects.filter(Q(lead__in=leads) | Q(case__in=cases_for(user))).select_related("lead", "author", "case")[:8],
    })


@login_required
def queue(request):
    today = date.today()
    base = own_leads(request.user).filter(_has_phone()).exclude(
        status__in=[S.CLOSED, S.NOT_RELEVANT, S.DO_NOT_CALL, S.IN_PROGRESS, *st.closed_keys(prof(request.user).workspace)])
    start_today = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    callbacks = base.filter(callback_on__lte=today, status__in=[S.CALLBACK, S.INTERESTED]).order_by("callback_on")[:40]
    retry = base.filter(status=S.NO_ANSWER, call_attempts__lt=MAX_ATTEMPTS).exclude(last_contact__gte=start_today).order_by("last_contact")[:40]
    fresh = base.filter(status=S.NEW).order_by("id")[:40]
    return render(request, "leads/queue.html", {
        "sections": [
            ("חזרות להיום", "לידים שביקשו שתחזור אליהם, או שעבר מועד החזרה", callbacks),
            ("לא ענו, ניסיון נוסף", f"עד {MAX_ATTEMPTS} ניסיונות לכל ליד", retry),
            ("לידים חדשים", "עוד לא התקשרת אליהם", fresh),
        ],
        "quick_buttons": [(S.NO_ANSWER.value, "לא ענה"), (S.INTERESTED.value, "מעוניין"),
                          (S.NOT_RELEVANT.value, "לא מעוניין"), (S.DO_NOT_CALL.value, "לא להתקשר")],
        "next": request.get_full_path(),
    })



def _run_call_summary(request, lead, case=None):
    """Summarize a pasted transcript with AI, save it as a note and (optionally) create follow-up tasks."""
    user = request.user
    transcript = request.POST.get("transcript", "").strip()
    if not ai.enabled():
        messages.error(request, "ה-AI לא מוגדר. חסר מפתח API בהגדרות השרת.")
        return
    if len(transcript) < 30:
        messages.error(request, "הדבק תמלול או הערות שיחה (לפחות כמה משפטים).")
        return
    if case is None and request.POST.get("client_ok") != "on":
        messages.error(request, "יש לאשר שהלקוח הסכים לשימוש בכלי AI.")
        return
    try:
        r = ai.summarize_call(transcript)
    except ai.AIError as exc:
        messages.error(request, str(exc))
        return
    lines = [r.get("summary", "")]
    interest = {"high": "גבוה", "medium": "בינוני", "low": "נמוך"}.get(r.get("customer_interest"), "לא ידוע")
    lines.append(f"רמת עניין: {interest}")
    if r.get("decisions"):
        lines.append("החלטות: " + "; ".join(map(str, r["decisions"])))
    if r.get("open_questions"):
        lines.append("שאלות פתוחות: " + "; ".join(map(str, r["open_questions"])))
    tasks = [t for t in r.get("follow_up_tasks", []) if isinstance(t, dict) and t.get("title")]
    if tasks:
        lines.append("משימות המשך: " + "; ".join(str(t["title"]) for t in tasks))
    Note.objects.create(lead=lead, case=case, author=user, kind=Note.Kind.AI, channel=Note.Channel.PHONE, text="\n".join(lines))
    created = 0
    if request.POST.get("make_tasks") == "on":
        for t in tasks[:6]:
            days = t.get("due_in_days")
            due = date.today() + timedelta(days=int(days)) if isinstance(days, int) and 0 <= days <= 90 else None
            Task.objects.create(owner=user, title=str(t["title"])[:200], due_on=due, lead=lead, case=case)
            created += 1
    audit(user, "ai_call_summary", f"סיכום שיחה ל{lead.full_name}")
    messages.success(request, "סיכום השיחה נשמר" + (f" ונוצרו {created} משימות המשך." if created else "."))


# ---------- leads ----------
BOARD_LIMIT = 25


@login_required
def lead_list(request):
    user, p = request.user, prof(request.user)
    base = own_leads(user)
    q = request.GET.get("q", request.POST.get("q", "")).strip()
    status = request.GET.get("status", request.POST.get("status", ""))
    phone = request.GET.get("phone", request.POST.get("phone", ""))
    owner = request.GET.get("owner", request.POST.get("owner", ""))
    web = request.GET.get("web", "")
    qs = base
    if q:
        cond = (Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(phone__icontains=q)
                | Q(phone2__icontains=q) | Q(city__icontains=q) | Q(email__icontains=q) | Q(campaign__icontains=q))
        if p.pii_ok and q.isdigit() and len(q) >= 5:
            cond |= Q(national_id_hash=nid_hash(q))  # ID numbers are encrypted: exact match only
        qs = qs.filter(cond)
    if phone == "yes":
        qs = qs.filter(_has_phone())
    elif phone == "no":
        qs = qs.filter(phone="", phone2="")
    if web == "1":
        qs = qs.filter(intake_at__isnull=False)
    if p.is_manager and owner == "none":
        qs = qs.filter(assigned_to__isnull=True)
    elif p.is_manager and owner.isdigit():
        qs = qs.filter(assigned_to_id=int(owner))

    team = User.objects.filter(is_active=True, profile__workspace=p.workspace).order_by("username") if p.is_manager else []
    if request.method == "POST" and p.is_manager and request.POST.get("action") == "assign":
        target = User.objects.filter(pk=request.POST.get("assignee"), is_active=True, profile__workspace=p.workspace).first()
        try:
            n = max(1, min(int(request.POST.get("count", "0")), 5000))
        except ValueError:
            n = 0
        if not target or not n:
            messages.error(request, "בחר משתמש וכמות.")
        else:
            ids = list(qs.filter(assigned_to__isnull=True).values_list("id", flat=True)[:n])
            Lead.objects.filter(id__in=ids).update(assigned_to=target)
            audit(user, "assign", f"שויכו {len(ids)} לידים אל {target.username}")
            messages.success(request, f"שויכו {len(ids)} לידים אל {target.username}.")
        params = {k: v for k, v in (("q", q), ("status", status), ("phone", phone), ("owner", owner), ("web", web)) if v}
        from urllib.parse import urlencode
        return redirect(f"{request.path}?{urlencode(params)}")

    defs = st.defs(p.workspace)
    valid = {d.key for d in defs}
    if status not in valid:
        status = ""
    counts = {r["status"]: r["n"] for r in qs.values("status").annotate(n=Count("id"))}
    last_note = Subquery(Note.objects.filter(lead=OuterRef("pk"), kind="note").order_by("-created_at").values("text")[:1])
    ordered = qs.select_related("assigned_to").annotate(last_note=last_note).order_by(F("callback_on").asc(nulls_last=True), "-id")
    sections, page = [], None
    if status:
        page = Paginator(ordered.filter(status=status), 50).get_page(request.GET.get("page"))
        d = next(x for x in defs if x.key == status)
        sections = [{"def": d, "count": counts.get(status, 0), "rows": page, "more": False}]
    else:
        for d in defs:
            n = counts.get(d.key, 0)
            if n:
                sections.append({"def": d, "count": n, "rows": list(ordered.filter(status=d.key)[:BOARD_LIMIT]), "more": n > BOARD_LIMIT})
    params = request.GET.copy()
    params.pop("page", None)
    shared_with_me = list(shared_leads(user).select_related("assigned_to").prefetch_related("shares")[:30]) if not (q or status or phone or owner or web) else []
    return render(request, "leads/lead_list.html", {"shared_with_me": shared_with_me, 
        "sections": sections, "page": page, "q": q, "status": status, "phone": phone, "owner": owner, "web": web,
        "chips": [{"def": d, "count": counts.get(d.key, 0)} for d in defs], "total": sum(counts.values()),
        "defs": defs, "palette": st.PALETTE, "today_date": date.today(), "qs": params.urlencode(), "team": team, "next": request.get_full_path(),
        "unassigned_matching": qs.filter(assigned_to__isnull=True).count() if p.is_manager else 0,
    })


@login_required
def lead_drawer(request, pk):
    lead = get_object_or_404(leads_for(request.user), pk=pk)
    p = prof(request.user)
    return render(request, "leads/_lead_drawer.html", {
        "lead": lead, "defs": st.defs(lead.workspace), "notes": lead.notes.select_related("author", "case")[:30],
        "cases": lead.cases.all()[:6], "emails": lead.emails.all()[:5] if p.role != UserProfile.Role.SALES else [],
        "next": request.GET.get("next", "/leads/"),
    })


@login_required
@require_POST
def lead_note_quick(request, pk):
    lead = get_object_or_404(leads_for(request.user), pk=pk)
    text = request.POST.get("text", "").strip()[:4000]
    if text:
        Note.objects.create(lead=lead, author=request.user, text=text)
        lead.last_contact = timezone.now()
        lead.save(update_fields=["last_contact", "updated_at"])
    return redirect(_safe_next(request, "/leads/"))


@manager_required
@require_POST
def status_add(request):
    p = prof(request.user)
    d, err = st.create(p.workspace, request.POST.get("name"), request.POST.get("color", ""), request.POST.get("kind", "normal"))
    if err:
        messages.error(request, err)
    else:
        audit(request.user, "status_add", f"סטטוס חדש: {d.label}")
        messages.success(request, f"הסטטוס \"{d.label}\" נוסף.")
    return redirect(_safe_next(request, "/leads/"))


@manager_required
@require_POST
def status_delete(request, pk):
    from .models import StatusDef
    p = prof(request.user)
    d = get_object_or_404(StatusDef, pk=pk, workspace=p.workspace, builtin=False)
    moved = Lead.objects.filter(workspace=p.workspace, status=d.key).update(status=S.NEW)
    audit(request.user, "status_delete", f"נמחק סטטוס: {d.label} ({moved} לידים הוחזרו ל'חדש')")
    d.delete()
    messages.success(request, f"הסטטוס נמחק. {moved} לידים הוחזרו ל״חדש״.")
    return redirect(_safe_next(request, "/leads/"))


LEAD_FIELDS = ["first_name", "last_name", "national_id", "phone", "phone2", "email", "city", "street", "house_number", "zip_code", "gender", "source"]


def _lead_values(post):
    v = {k: post.get(k, "").strip() for k in LEAD_FIELDS}
    v["national_id"] = "".join(ch for ch in v["national_id"] if ch.isdigit()).zfill(9)[-9:] if v["national_id"] else ""
    v["phone"] = normalize_phone(v["phone"])
    v["phone2"] = normalize_phone(v["phone2"])
    v["source"] = v["source"][:60]
    return v


@login_required
def lead_create(request):
    user, p = request.user, prof(request.user)
    values = {}
    if request.method == "POST":
        values = _lead_values(request.POST)
        age = request.POST.get("age", "").strip()
        if not values["first_name"] or not values["national_id"] or not (values["phone"] or values["phone2"]):
            messages.error(request, "חובה למלא שם פרטי, תעודת זהות וטלפון.")
        elif Lead.objects.filter(workspace=p.workspace, national_id_hash=nid_hash(values["national_id"])).exists():
            messages.error(request, "ליד עם תעודת זהות זו כבר קיים.")
        else:
            lead = Lead.objects.create(
                workspace=p.workspace, assigned_to=user, id_valid=valid_israeli_id(values["national_id"]),
                birth_date=parse_date(request.POST.get("birth_date")), age=int(age) if age.isdigit() else None, **values)
            note = request.POST.get("note", "").strip()
            if note:
                Note.objects.create(lead=lead, author=user, text=note)
            audit(user, "lead_create", f"נוסף ליד: {lead.full_name}")
            messages.success(request, "הליד נוסף.")
            return redirect("lead_detail", pk=lead.pk)
    return render(request, "leads/lead_form.html", {"v": values, "mode": "create"})


@login_required
def lead_edit(request, pk):
    user = request.user
    lead = get_object_or_404(leads_for(user), pk=pk)
    if not prof(user).pii_ok:
        messages.error(request, "אין לך הרשאה לערוך פרטי לקוח.")
        return redirect("lead_detail", pk=pk)
    if request.method == "POST":
        values = _lead_values(request.POST)
        clash = Lead.objects.filter(workspace=lead.workspace, national_id_hash=nid_hash(values["national_id"])).exclude(pk=lead.pk).exists()
        if not values["first_name"] or not values["national_id"]:
            messages.error(request, "חובה למלא שם פרטי ותעודת זהות.")
        elif clash:
            messages.error(request, "תעודת זהות זו שייכת לליד אחר.")
        else:
            for k, v in values.items():
                setattr(lead, k, v)
            lead.id_valid = valid_israeli_id(lead.national_id)
            lead.birth_date = parse_date(request.POST.get("birth_date"))
            age = request.POST.get("age", "").strip()
            lead.age = int(age) if age.isdigit() else None
            lead.save()
            audit(user, "lead_edit", f"נערך ליד: {lead.full_name}")
            messages.success(request, "הפרטים נשמרו.")
            return redirect("lead_detail", pk=lead.pk)
    v = {k: getattr(lead, k) for k in LEAD_FIELDS}
    v.update(age=lead.age, birth_date=lead.birth_date.isoformat() if lead.birth_date else "")
    return render(request, "leads/lead_form.html", {"v": v, "mode": "edit", "lead": lead})


@manager_required
@require_POST
def lead_delete(request, pk):
    lead = get_object_or_404(leads_for(request.user), pk=pk)
    for case in lead.cases.all():
        for d in case.documents.all():
            d.file.delete(save=False)
    audit(request.user, "lead_delete", f"נמחק ליד: {lead.full_name} ({lead.cases.count()} תיקים)")
    lead.delete()
    messages.success(request, "הליד נמחק, יחד עם התיקים וההערות שלו.")
    return redirect("lead_list")


@login_required
def lead_detail(request, pk):
    user, p = request.user, prof(request.user)
    lead = get_object_or_404(leads_for(user), pk=pk)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "note":
            text = request.POST.get("text", "").strip()
            channel = request.POST.get("channel", "")
            if text:
                Note.objects.create(lead=lead, author=user, text=text, channel=channel if channel in Note.Channel.values else "")
                lead.last_contact = timezone.now()
                lead.save(update_fields=["last_contact", "updated_at"])
                messages.success(request, "ההערה נשמרה.")
        elif action == "status":
            status = request.POST.get("status")
            if status not in st.keys(lead.workspace):
                return HttpResponseBadRequest("סטטוס לא תקין")
            cb = _parse_day(request.POST.get("callback_on"))
            if status == S.CALLBACK and not cb:
                cb = date.today() + timedelta(days=2)
            _apply_status(lead, status, user, cb)
            messages.success(request, "הסטטוס עודכן.")
        elif action == "open_case":
            if lead.workspace != p.workspace:
                messages.error(request, "ליד משותף מסביבת עבודה אחרת: אפשר לעדכן ולהוסיף הערות, לא לפתוח תיק.")
                return redirect("lead_detail", pk=pk)
            if p.role == UserProfile.Role.SALES:
                messages.error(request, "פתיחת תיק מותרת ליועץ או למנהל.")
                return redirect("lead_detail", pk=pk)
            ctype = request.POST.get("case_type")
            if ctype not in dict(Case.types_for(p.workspace)):
                return HttpResponseBadRequest("סוג תיק לא תקין")
            case = Case.objects.create(lead=lead, workspace=p.workspace, case_type=ctype, owner=user, amount=_dec(request.POST.get("amount")))
            Note.objects.create(lead=lead, case=case, author=user, kind=Note.Kind.STATUS, text=f"נפתח תיק: {case.get_case_type_display()}")
            if lead.status not in (S.IN_PROGRESS, S.CLOSED):
                _apply_status(lead, S.IN_PROGRESS, user)
            audit(user, "case_create", f"נפתח תיק {case.get_case_type_display()} ל{lead.full_name}")
            return redirect("case_detail", pk=case.pk)
        elif action == "call_summary":
            _run_call_summary(request, lead)
        elif action == "lead_share" and can_share_lead(user, lead):
            target = User.objects.filter(pk=request.POST.get("partner"), is_active=True).exclude(pk=user.pk).first()
            if not target:
                messages.error(request, "בחר משתמש לשיתוף.")
            else:
                LeadShare.objects.get_or_create(lead=lead, shared_with=target, defaults={"shared_by": user})
                text = request.POST.get("msg", "").strip() or "שיתפתי איתך ליד."
                Message.objects.create(sender=user, recipient=target, text=f"{text} ({lead.full_name})")
                Note.objects.create(lead=lead, author=user, kind=Note.Kind.STATUS, text=f"הליד שותף עם {target.username}")
                audit(user, "lead_share", f"ליד {lead.pk} ({lead.full_name}) שותף עם {target.username}")
                messages.success(request, f"הליד שותף עם {target.username}.")
        elif action == "lead_unshare" and can_share_lead(user, lead):
            sh = get_object_or_404(lead.shares, pk=request.POST.get("share"))
            audit(user, "lead_unshare", f"בוטל שיתוף ליד {lead.pk} עם {sh.shared_with.username}")
            Note.objects.create(lead=lead, author=user, kind=Note.Kind.STATUS, text=f"בוטל השיתוף עם {sh.shared_with.username}")
            sh.delete()
            messages.success(request, "השיתוף בוטל.")
        elif action == "assign" and p.is_manager:
            target = User.objects.filter(pk=request.POST.get("assignee"), is_active=True, profile__workspace=p.workspace).first()
            lead.assigned_to = target
            lead.save(update_fields=["assigned_to", "updated_at"])
            audit(user, "assign", f"{lead.full_name} שויך אל {target.username if target else 'ללא'}")
            messages.success(request, "השיוך עודכן.")
        return redirect("lead_detail", pk=lead.pk)
    return render(request, "leads/lead_detail.html", {
        "lead": lead, "statuses": [(d.key, d.label) for d in st.defs(lead.workspace)], "notes": lead.notes.select_related("author", "case"),
        "cases": lead.cases.all(), "case_types": Case.types_for(p.workspace), "channels": Note.Channel.choices,
        "team": User.objects.filter(is_active=True, profile__workspace=p.workspace) if p.is_manager else [],
        "lead_tasks": lead.tasks.filter(owner=user, done=False), "ai_enabled": ai.enabled(),
        "can_share": can_share_lead(user, lead), "lead_shares": lead.shares.select_related("shared_with"),
        "partners": User.objects.filter(is_active=True).exclude(pk=user.pk).select_related("profile").order_by("username"),
        "is_own_ws": lead.workspace == p.workspace,
    })


@login_required
@require_POST
def quick_status(request, pk):
    lead = get_object_or_404(leads_for(request.user), pk=pk)
    status = request.POST.get("status")
    if status not in st.keys(lead.workspace):
        return HttpResponseBadRequest("סטטוס לא תקין")
    cb = _parse_day(request.POST.get("callback_on"))
    if status == S.CALLBACK and not cb:
        cb = date.today() + timedelta(days=2)
    _apply_status(lead, status, request.user, cb)
    return redirect(_safe_next(request, "/queue/"))


@manager_required
def import_view(request):
    result = None
    if request.method == "POST" and request.FILES.get("file"):
        try:
            result = import_leads(request.FILES["file"], workspace=prof(request.user).workspace)
            audit(request.user, "import", f"ייבוא לידים: {result['created']} חדשים, {result['duplicates_removed']} כפילויות")
        except Exception as exc:
            messages.error(request, f"הייבוא נכשל: {exc}")
    return render(request, "leads/import.html", {"result": result})


# ---------- cases ----------
# categories that only make sense in the finance workspace (hidden from the legal upload menu)
_FINANCE_ONLY_CATS = {"payslip", "assessment", "bank", "credit", "balances", "amort", "loans", "property", "pension", "tax_exempt", "ni_employers"}
STAGE_ORDER = [Case.Stage.OPENED, Case.Stage.DOCS, Case.Stage.SUBMITTED, Case.Stage.APPROVED, Case.Stage.CLOSED]


@login_required
def case_board(request):
    user = request.user
    ctype = request.GET.get("type", "")
    qs = cases_for(user).select_related("lead", "owner")
    if ctype in Case.Type.values:
        qs = qs.filter(case_type=ctype)
    ws = prof(user).workspace
    columns = []
    for st in STAGE_ORDER:
        sub = qs.filter(stage=st)
        columns.append({"label": st.label, "value": st.value, "cases": list(sub[:60]), "count": sub.count(),
                        "fees": sub.filter(workspace=ws).aggregate(t=Sum("fee"))["t"] or 0})
    return render(request, "leads/case_board.html", {
        "columns": columns, "case_types": Case.types_for(ws), "ctype": ctype,
        "cancelled": qs.filter(stage=Case.Stage.CANCELLED).count(), "ws": ws,
    })


@login_required
def case_detail(request, pk):
    user, p = request.user, prof(request.user)
    case = get_object_or_404(Case.objects.select_related("lead", "owner"), pk=pk)
    access = case_access(user, case)
    if not access:
        raise Http404
    is_own = access == "own"
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "stage":
            stage = request.POST.get("stage")
            if stage not in Case.Stage.values:
                return HttpResponseBadRequest("שלב לא תקין")
            old = case.get_stage_display()
            case.stage = stage
            case.closed_at = timezone.now() if stage == Case.Stage.CLOSED else None
            case.save()
            Note.objects.create(lead=case.lead, case=case, author=user, kind=Note.Kind.STATUS, text=f"שלב התיק: {old} ← {case.get_stage_display()}")
            messages.success(request, "השלב עודכן." if not (stage == Case.Stage.CLOSED and case.fee is None) else "התיק נסגר. אל תשכח להזין את עלות התיק.")
        elif action == "finance":
            if request.POST.get("case_type") in dict(Case.types_for(case.workspace)):
                case.case_type = request.POST["case_type"]
            case.amount, case.fee = _dec(request.POST.get("amount")), _dec(request.POST.get("fee"))
            case.monthly_income = _dec(request.POST.get("monthly_income"))
            case.file_number = request.POST.get("file_number", "").strip()[:60]
            for k, val in _legal_fields(request.POST, case.workspace).items():
                setattr(case, k, val)
            case.fee_paid = request.POST.get("fee_paid") == "on"
            case.save()
            Note.objects.create(lead=case.lead, case=case, author=user, kind=Note.Kind.STATUS, text="עודכנו פרטי התיק והכספים")
            messages.success(request, "הפרטים נשמרו.")
        elif action == "summary":
            case.summary = request.POST.get("summary", "").strip()
            case.save()
            messages.success(request, "התקציר נשמר.")
        elif action == "note":
            text = request.POST.get("text", "").strip()
            channel = request.POST.get("channel", "")
            if text:
                Note.objects.create(lead=case.lead, case=case, author=user, text=text, channel=channel if channel in Note.Channel.values else "")
                messages.success(request, "ההערה נשמרה.")
        elif action == "upload":
            f = request.FILES.get("file")
            category = request.POST.get("category", "other")
            if category not in Document.Category.values:
                category = "other"
            if not f:
                messages.error(request, "לא נבחר קובץ.")
            elif os.path.splitext(f.name)[1].lower() not in Document.ALLOWED_EXT:
                messages.error(request, "סוג קובץ לא נתמך. מותר: PDF, תמונות, Word ו-Excel.")
            elif f.size > Document.MAX_BYTES:
                messages.error(request, "הקובץ גדול מ-15MB.")
            elif not _upload_ok(request, f):
                pass
            else:
                custom = re.sub(r"[\x00-\x1f]", "", request.POST.get("custom_name", "")).strip()[:120] if category == "other" else ""
                Document.objects.create(case=case, category=category, file=f, original_name=f.name[:255], size=f.size,
                                        uploaded_by=user, is_shared=not is_own, custom_name=custom)
                Note.objects.create(lead=case.lead, case=case, author=user, kind=Note.Kind.STATUS, text=f"הועלה מסמך: {f.name}")
                messages.success(request, "המסמך הועלה.")
        elif action == "doc_settings" and is_own:
            doc_rules.save_states(case, {k: request.POST.get("req_" + k) for k in doc_rules.REQ})
            case.save()
            Note.objects.create(lead=case.lead, case=case, author=user, kind=Note.Kind.STATUS, text="עודכנה רשימת המסמכים הנדרשים בתיק")
            messages.success(request, "רשימת המסמכים נשמרה.")
        elif action == "doc_state" and is_own:
            key, state = request.POST.get("key"), request.POST.get("state")
            if key in doc_rules.REQ and state in doc_rules.STATE_LABELS:
                posted = dict(doc_rules.states(case))
                posted[key] = state
                doc_rules.save_states(case, posted)
                case.save()
                messages.success(request, f"{doc_rules.REQ[key][0]}: {doc_rules.STATE_LABELS[state]}.")
        elif action == "confirm_missing" and is_own:
            rows = agents.checklist(case)
            proposed = doc_rules.missing(rows)
            doc_rules.confirm(case, [r["key"] for r in proposed])
            case.save()
            text = ("אושרה רשימת החוסרים: " + ", ".join(r["label"] for r in proposed)) if proposed else "אושר שכל המסמכים הנדרשים התקבלו"
            Note.objects.create(lead=case.lead, case=case, author=user, kind=Note.Kind.STATUS, text=text)
            audit(user, "docs_confirm", f"תיק {case.pk}: {text}")
            messages.success(request, "הרשימה אושרה. המתפעלת תנסח לפיה הודעה ללקוח.")
        elif action == "doc_category" and is_own:
            doc = get_object_or_404(case.documents, pk=request.POST.get("doc"))
            cat = request.POST.get("category")
            if cat in Document.Category.values:
                doc.category = cat
                doc.save(update_fields=["category"])
                messages.success(request, "סוג המסמך עודכן.")
        elif action == "toggle_doc" and is_own:
            doc = get_object_or_404(case.documents, pk=request.POST.get("doc"))
            doc.is_shared = not doc.is_shared
            doc.save(update_fields=["is_shared"])
            audit(user, "doc_share", f"{'שותף' if doc.is_shared else 'בוטל שיתוף'}: {doc.original_name} (תיק {case.pk})")
        elif action == "share" and is_own:
            target = User.objects.filter(pk=request.POST.get("partner"), is_active=True).exclude(profile__workspace=case.workspace).first()
            if not target:
                messages.error(request, "בחר משתמש לשיתוף.")
            else:
                CaseShare.objects.get_or_create(case=case, shared_with=target, defaults={"shared_by": user})
                text = request.POST.get("msg", "").strip() or "שיתפתי איתך תיק."
                Message.objects.create(sender=user, recipient=target, case=case, text=text)
                Note.objects.create(lead=case.lead, case=case, author=user, kind=Note.Kind.STATUS, text=f"התיק שותף עם {target.username}")
                audit(user, "case_share", f"תיק {case.pk} ({case.lead.full_name}) שותף עם {target.username}")
                messages.success(request, f"התיק שותף עם {target.username}. סמן למטה אילו מסמכים הוא יראה.")
        elif action == "unshare" and is_own:
            sh = get_object_or_404(case.shares, pk=request.POST.get("share"))
            audit(user, "case_unshare", f"בוטל שיתוף תיק {case.pk} עם {sh.shared_with.username}")
            Note.objects.create(lead=case.lead, case=case, author=user, kind=Note.Kind.STATUS, text=f"בוטל השיתוף עם {sh.shared_with.username}")
            sh.delete()
            messages.success(request, "השיתוף בוטל.")
            return redirect("case_detail", pk=case.pk)
        elif action in ("ai_consent", "ai_consent_off") and is_own and p.pii_ok:
            case.ai_consent = action == "ai_consent"
            case.ai_consent_at = timezone.now() if case.ai_consent else None
            case.save(update_fields=["ai_consent", "ai_consent_at"])
            audit(user, "ai_consent", f"תיק {case.pk}: אישור AI {'ניתן' if case.ai_consent else 'בוטל'}")
            messages.success(request, "אישור הלקוח לשימוש ב-AI נרשם." if case.ai_consent else "אישור ה-AI בוטל.")
        elif action == "ai_analyze":
            doc = get_object_or_404(docs_for(user, case), pk=request.POST.get("doc"))
            if not ai.enabled():
                messages.error(request, "ה-AI לא מוגדר. חסר מפתח API בהגדרות השרת.")
            elif not case.ai_consent:
                messages.error(request, "כדי לנתח מסמכים עם AI יש לסמן קודם שהלקוח אישר זאת.")
            else:
                try:
                    data, summary, warnings = ai.analyze_document(doc.file, doc.category)
                    DocumentAnalysis.objects.create(document=doc, data=data, summary=summary, warnings=warnings,
                                                    model_name=settings.CLAUDE_MODEL, created_by=user)
                    audit(user, "ai_analyze", f"נותח מסמך: {doc.original_name} (תיק {case.pk})")
                    messages.success(request, "הניתוח מוכן. בדוק את הנתונים ואשר אותם כדי שייכנסו לחישוב.")
                except (ai.AIError, OSError, NotImplementedError) as exc:
                    messages.error(request, str(exc) if isinstance(exc, ai.AIError) else "לא ניתן לקרוא את הקובץ.")
        elif action in ("analysis_confirm", "analysis_discard", "analysis_fix"):
            an = get_object_or_404(DocumentAnalysis.objects.filter(document__case=case), pk=request.POST.get("analysis"))
            if not docs_for(user, case).filter(pk=an.document_id).exists():
                raise Http404
            if action == "analysis_discard":
                an.delete()
                messages.success(request, "הניתוח נמחק.")
            else:
                if action == "analysis_fix":
                    try:
                        fixed = json.loads(request.POST.get("data_json", ""))
                        if not isinstance(fixed, dict):
                            raise ValueError
                        an.data = fixed
                    except ValueError:
                        messages.error(request, "התיקון אינו JSON תקין, לא נשמר.")
                        return redirect("case_detail", pk=case.pk)
                an.status = DocumentAnalysis.Status.CONFIRMED
                an.confirmed_by = user
                an.save()
                audit(user, "ai_confirm", f"אושר ניתוח למסמך {an.document.original_name} (תיק {case.pk})")
                messages.success(request, "הנתונים אושרו ונכנסו לסיכום הפיננסי.")
        elif action == "call_summary":
            if not case.ai_consent:
                messages.error(request, "כדי להשתמש ב-AI יש לסמן קודם שהלקוח אישר זאת.")
            else:
                _run_call_summary(request, case.lead, case)
        elif action == "generate" and is_own:
            tpl = get_object_or_404(DocTemplate.objects.filter(workspace=case.workspace), pk=request.POST.get("template"))
            try:
                with tpl.file.open("rb") as fh:
                    content = docgen.fill_template(fh, docgen.case_context(case, user))
            except Exception:
                messages.error(request, "לא ניתן היה לקרוא את התבנית. ודא שזה קובץ Word (docx) תקין.")
            else:
                from django.core.files.base import ContentFile
                name = f"{tpl.name} - {case.lead.full_name}.docx"
                doc = Document(case=case, category=Document.Category.CONTRACT, original_name=name[:255], size=len(content), uploaded_by=user)
                doc.file.save("generated.docx", ContentFile(content), save=False)
                doc.save()
                Note.objects.create(lead=case.lead, case=case, author=user, kind=Note.Kind.STATUS, text=f"נוצר מסמך מתבנית: {name}")
                audit(user, "doc_generate", f"נוצר {name} (תיק {case.pk})")
                messages.success(request, "המסמך נוצר ונשמר בתיק, בטבלת המסמכים.")
        elif action == "task":
            title = request.POST.get("title", "").strip()
            if title:
                Task.objects.create(owner=user, title=title[:200], due_on=_parse_day(request.POST.get("due_on")),
                                    priority=request.POST.get("priority") if request.POST.get("priority") in Task.Priority.values else "normal",
                                    case=case, lead=case.lead if is_own else None)
                messages.success(request, "המשימה נוספה.")
        return redirect("case_detail", pk=case.pk)

    if not is_own:
        CaseShare.objects.filter(case=case, shared_with=user).update(last_seen_at=timezone.now())
    docs = list(docs_for(user, case))
    rows = agents.checklist(case, docs)
    missing_rows = doc_rules.missing(rows)
    _, proposed, awaiting = agents.missing_state(case, rows)
    confirmed = bool(missing_rows) and doc_rules.is_confirmed(case, [r["key"] for r in missing_rows])
    upload_cats = [(v, l) for v, l in Document.Category.choices if case.workspace != "legal" or v not in _FINANCE_ONLY_CATS]
    partners = User.objects.filter(is_active=True).exclude(profile__workspace=case.workspace).exclude(pk=user.pk)
    return render(request, "leads/case_detail.html", {
        "case": case, "lead": case.lead, "docs": docs, "checklist": rows, "procedures": Case.Procedure.choices,
        "missing": len(missing_rows), "awaiting": awaiting, "confirmed": confirmed,
        "req_rows": doc_rules.settings_rows(case) if is_own else [], "state_labels": doc_rules.STATE_LABELS.items(),
        "clerk_notes": agents.doc_notes(case, docs, rows) if is_own else [],
        "notes": case.notes.select_related("author"), "stages": Case.Stage.choices,
        "categories": upload_cats, "channels": Note.Channel.choices, "case_types": Case.types_for(case.workspace),
        "is_own": is_own, "shares": case.shares.select_related("shared_with") if is_own else [],
        "partners": partners, "case_tasks": case.tasks.filter(owner=user, done=False),
        "can_delete": is_own and (p.is_manager or case.owner_id == user.id),
        "ai_enabled": ai.enabled(), "can_consent": is_own and p.pii_ok,
        "docs_info": _docs_with_analyses(docs),
        "fin": ai.case_financials(case) if is_own or case.ai_consent else None,
        "templates": DocTemplate.objects.filter(workspace=case.workspace).filter(Q(case_type="") | Q(case_type=case.case_type)),
    })


@login_required
@require_POST
def case_delete(request, pk):
    case = get_object_or_404(Case, pk=pk)
    p = prof(request.user)
    if case_access(request.user, case) != "own" or not (p.is_manager or case.owner_id == request.user.id):
        messages.error(request, "אין הרשאה למחוק תיק זה.")
        return redirect("case_detail", pk=pk)
    for d in case.documents.all():
        d.file.delete(save=False)
    audit(request.user, "case_delete", f"נמחק תיק {case.get_case_type_display()} של {case.lead.full_name}")
    case.delete()
    messages.success(request, "התיק נמחק.")
    return redirect("case_board")


@login_required
def document_download(request, pk):
    doc = get_object_or_404(Document.objects.select_related("case__lead"), pk=pk)
    if not can_see_document(request.user, doc):
        raise Http404
    try:
        fh = doc.file.open("rb")
    except FileNotFoundError:
        raise Http404("הקובץ לא נמצא")
    audit(request.user, "doc_download", f"הורד: {doc.original_name} (תיק {doc.case_id})")
    return FileResponse(fh, as_attachment=True, filename=doc.original_name)


@login_required
@require_POST
def documents_bulk(request, case_pk):
    """Download several documents as one zip, or delete several at once. Same permission rules as one document."""
    import io
    import zipfile
    case = get_object_or_404(cases_for(request.user), pk=case_pk)
    ids = [int(i) for i in request.POST.getlist("doc") if i.isdigit()][:200]
    docs = [d for d in case.documents.filter(pk__in=ids) if can_see_document(request.user, d)]
    if not docs:
        messages.error(request, "לא נבחרו מסמכים.")
        return redirect("case_detail", pk=case.pk)
    op = request.POST.get("op")
    p = prof(request.user)
    if op == "download":
        buf, used = io.BytesIO(), set()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for d in docs:
                try:
                    data = d.file.open("rb").read()
                except FileNotFoundError:
                    continue
                name = (f"{d.custom_name or d.get_category_display()} - {d.original_name}").replace("/", "_").replace("\\", "_")
                n, i = name, 1
                while n in used:
                    i += 1
                    n = f"{i}_{name}"
                used.add(n)
                z.writestr(n, data)
        audit(request.user, "doc_download_bulk", f"הורדה מרוכזת: {len(docs)} מסמכים (תיק {case.pk})")
        resp = HttpResponse(buf.getvalue(), content_type="application/zip")
        resp["Content-Disposition"] = f"attachment; filename=case-{case.pk}-documents.zip"
        return resp
    if op == "delete":
        ws_manager = p.is_manager and case.workspace == p.workspace
        removed = 0
        for d in docs:
            if ws_manager or d.uploaded_by_id == request.user.id:
                Note.objects.create(lead=case.lead, case=case, author=request.user, kind=Note.Kind.STATUS, text=f"נמחק מסמך: {d.original_name}")
                d.file.delete(save=False)
                d.delete()
                removed += 1
        audit(request.user, "doc_delete_bulk", f"נמחקו {removed} מסמכים (תיק {case.pk})")
        messages.success(request, f"נמחקו {removed} מסמכים.") if removed else messages.error(request, "אין הרשאה למחוק את המסמכים שנבחרו.")
    return redirect("case_detail", pk=case.pk)


@login_required
@require_POST
def document_delete(request, pk):
    doc = get_object_or_404(Document, pk=pk)
    if not can_see_document(request.user, doc):
        raise Http404
    p = prof(request.user)
    owner_ws_manager = p.is_manager and doc.case.workspace == p.workspace
    if owner_ws_manager or doc.uploaded_by_id == request.user.id:
        Note.objects.create(lead=doc.case.lead, case=doc.case, author=request.user, kind=Note.Kind.STATUS, text=f"נמחק מסמך: {doc.original_name}")
        audit(request.user, "doc_delete", f"נמחק: {doc.original_name} (תיק {doc.case_id})")
        case_pk = doc.case_id
        doc.file.delete(save=False)
        doc.delete()
        messages.success(request, "המסמך נמחק.")
    else:
        case_pk = doc.case_id
        messages.error(request, "אין הרשאה למחוק מסמך זה.")
    return redirect("case_detail", pk=case_pk)


# ---------- tasks ----------
@login_required
def tasks(request):
    user = request.user
    if request.method == "POST":
        title = request.POST.get("title", "").strip()
        if title:
            lead = leads_for(user).filter(pk=request.POST.get("lead")).first() if request.POST.get("lead") else None
            Task.objects.create(owner=user, title=title[:200], due_on=_parse_day(request.POST.get("due_on")), lead=lead,
                                priority=request.POST.get("priority") if request.POST.get("priority") in Task.Priority.values else "normal")
            messages.success(request, "המשימה נוספה.")
        return redirect(_safe_next(request, "tasks"))
    mine = Task.objects.filter(owner=user).select_related("lead", "case__lead")
    return render(request, "leads/tasks.html", {
        "open": mine.filter(done=False), "done": mine.filter(done=True).order_by("-done_at")[:15], "today": date.today(),
    })


@login_required
@require_POST
def task_toggle(request, pk):
    t = get_object_or_404(Task, pk=pk, owner=request.user)
    t.done = not t.done
    t.done_at = timezone.now() if t.done else None
    t.save()
    return redirect(_safe_next(request, "tasks"))


@login_required
@require_POST
def task_delete(request, pk):
    get_object_or_404(Task, pk=pk, owner=request.user).delete()
    return redirect(_safe_next(request, "tasks"))


# ---------- messages ----------
@login_required
def inbox(request):
    user = request.user
    people = User.objects.filter(is_active=True).exclude(pk=user.pk).order_by("username")
    other = people.filter(pk=request.GET.get("with")).first() if request.GET.get("with") else None
    if request.method == "POST":
        to = people.filter(pk=request.POST.get("to")).first()
        text = request.POST.get("text", "").strip()
        if to and text:
            Message.objects.create(sender=user, recipient=to, text=text)
        return redirect(f"/messages/?with={to.pk}" if to else "/messages/")
    unread = {r["sender"]: r["n"] for r in Message.objects.filter(recipient=user, read_at__isnull=True).values("sender").annotate(n=Count("id"))}
    thread = []
    if other:
        thread = list(Message.objects.filter(Q(sender=user, recipient=other) | Q(sender=other, recipient=user)).select_related("case")[:200])
        Message.objects.filter(sender=other, recipient=user, read_at__isnull=True).update(read_at=timezone.now())
    return render(request, "leads/messages.html", {
        "people": [(u, unread.get(u.pk, 0)) for u in people], "other": other, "thread": thread,
    })


# ---------- audit ----------
@manager_required
def audit_log(request):
    action = request.GET.get("action", "")
    qs = AuditLog.objects.select_related("user")
    if action:
        qs = qs.filter(action=action)
    page = Paginator(qs, 100).get_page(request.GET.get("page"))
    actions = AuditLog.objects.values_list("action", flat=True).distinct().order_by("action")
    return render(request, "leads/audit.html", {"page": page, "action": action, "actions": actions})


def _docs_with_analyses(docs):
    latest = {}
    for a in DocumentAnalysis.objects.filter(document__in=docs).order_by("created_at"):
        latest[a.document_id] = a
    out = []
    for d in docs:
        a = latest.get(d.pk)
        if a:
            a.lines = ai.describe(a.data)
            a.data_json = json.dumps(a.data, ensure_ascii=False, indent=1)
        out.append((d, a))
    return out


# ---------- manual case ----------
def _legal_fields(post, workspace):
    """Debtor, creditor, procedure, principal and judge: only for the legal workspace."""
    if workspace != "legal":
        return {}
    proc = post.get("procedure_type", "")
    return {"debtor_name": post.get("debtor_name", "").strip()[:150], "creditor_name": post.get("creditor_name", "").strip()[:150],
            "procedure_type": proc if proc in Case.Procedure.values else "", "principal_amount": _dec(post.get("principal_amount")),
            "registrar_judge": post.get("registrar_judge", "").strip()[:120]}


@login_required
def case_create(request):
    user, p = request.user, prof(request.user)
    if p.role == UserProfile.Role.SALES:
        messages.error(request, "פתיחת תיק מותרת ליועץ או למנהל.")
        return redirect("home")
    v = {}
    if request.method == "POST":
        v = _lead_values(request.POST)
        ctype = request.POST.get("case_type")
        if ctype not in dict(Case.types_for(p.workspace)):
            messages.error(request, "בחר סוג תיק.")
        elif not v["national_id"] or not v["first_name"]:
            messages.error(request, "חובה למלא שם פרטי ותעודת זהות.")
        else:
            lead = Lead.objects.filter(workspace=p.workspace, national_id_hash=nid_hash(v["national_id"])).first()
            if lead is None:
                if not (v["phone"] or v["phone2"]):
                    messages.error(request, "לקוח חדש: חובה למלא טלפון.")
                    return render(request, "leads/case_form.html", {"v": v, "case_types": Case.types_for(p.workspace), "procedures": Case.Procedure.choices})
                lead = Lead.objects.create(workspace=p.workspace, assigned_to=user, id_valid=valid_israeli_id(v["national_id"]), **v)
                audit(user, "lead_create", f"נוסף ליד (דרך תיק): {lead.full_name}")
            case = Case.objects.create(
                lead=lead, workspace=p.workspace, case_type=ctype, owner=user, amount=_dec(request.POST.get("amount")),
                monthly_income=_dec(request.POST.get("monthly_income")), file_number=request.POST.get("file_number", "").strip()[:60],
                summary=request.POST.get("summary", "").strip(), **_legal_fields(request.POST, p.workspace))
            Note.objects.create(lead=lead, case=case, author=user, kind=Note.Kind.STATUS, text=f"נפתח תיק: {case.get_case_type_display()}")
            if lead.status not in (S.IN_PROGRESS, S.CLOSED):
                _apply_status(lead, S.IN_PROGRESS, user)
            audit(user, "case_create", f"נפתח תיק ידני {case.get_case_type_display()} ל{lead.full_name}")
            messages.success(request, "התיק נפתח.")
            return redirect("case_detail", pk=case.pk)
    return render(request, "leads/case_form.html", {"v": v, "case_types": Case.types_for(p.workspace), "procedures": Case.Procedure.choices})


# ---------- document templates ----------
@manager_required
def templates_view(request):
    p = prof(request.user)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "upload":
            f = request.FILES.get("file")
            name = request.POST.get("name", "").strip()
            ctype = request.POST.get("case_type", "")
            if not f or not name:
                messages.error(request, "חובה שם וקובץ.")
            elif not f.name.lower().endswith(".docx"):
                messages.error(request, "התבנית חייבת להיות קובץ Word (docx).")
            else:
                try:
                    found = docgen.placeholders_in(f)
                    f.seek(0)
                except Exception:
                    messages.error(request, "לא ניתן לקרוא את הקובץ. ודא שזה docx תקין.")
                    return redirect("templates")
                DocTemplate.objects.create(name=name[:120], workspace=p.workspace, file=f, uploaded_by=request.user,
                                           case_type=ctype if ctype in Case.Type.values else "")
                known = {k for k, _ in docgen.FIELDS}
                unknown = sorted(found - known)
                audit(request.user, "template_upload", f"הועלתה תבנית: {name}")
                messages.success(request, "התבנית נשמרה." + (f" שים לב: שדות לא מוכרים (יישארו כמו שהם): {', '.join(unknown)}" if unknown else ""))
        elif action == "delete":
            t = get_object_or_404(DocTemplate, pk=request.POST.get("template"), workspace=p.workspace)
            t.file.delete(save=False)
            t.delete()
            messages.success(request, "התבנית נמחקה.")
        return redirect("templates")
    return render(request, "leads/templates.html", {
        "templates": DocTemplate.objects.filter(workspace=p.workspace), "fields": docgen.FIELDS, "case_types": Case.types_for(p.workspace),
    })


@login_required
def template_sample(request):
    from django.http import HttpResponse
    resp = HttpResponse(docgen.sample_template(),
                        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    resp["Content-Disposition"] = 'attachment; filename="sample_template.docx"'
    return resp


# ---------- dashboard ----------
def _pct(value, top):
    return round(float(value) / float(top) * 100, 1) if top else 0


def _bars(rows, color_for=None):
    top = max([r[2] for r in rows] + [1])
    return [{"label": l, "value": v, "pct": _pct(v, top), "key": k} for k, l, v in rows]


def _month_floor(d):
    return d.replace(day=1)


def _add_months(d, n):
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    return date(y, m + 1, 1)


@login_required
def dashboard(request):
    user, p = request.user, prof(request.user)
    today = date.today()
    try:
        days = int(request.GET.get("days", 90))
    except ValueError:
        days = 90
    days = days if days in (30, 90, 365) else 90
    since = today - timedelta(days=days)
    this_month = _month_floor(today)

    leads, cases = own_leads(user), own_cases(user)
    show_money = p.role != UserProfile.Role.SALES

    if request.method == "POST" and request.POST.get("action") == "goal" and not p.is_manager:
        messages.error(request, "קביעת יעדים מותרת רק למנהל.")
        return redirect(f"{request.path}?days={days}")
    # goal save (manager)
    if request.method == "POST" and request.POST.get("action") == "goal" and p.is_manager:
        Goal.objects.update_or_create(
            workspace=p.workspace, month=this_month,
            defaults={"fees_target": _dec(request.POST.get("fees_target")) or 0,
                      "cases_target": int(request.POST.get("cases_target") or 0) if (request.POST.get("cases_target") or "0").isdigit() else 0})
        audit(user, "goal", f"עודכן יעד חודשי ({this_month:%m/%Y})")
        messages.success(request, "היעד נשמר.")
        return redirect(f"{request.path}?days={days}")

    # funnel
    by_status = {r["status"]: r["n"] for r in leads.values("status").annotate(n=Count("id"))}
    funnel = _bars([(v, l, by_status.get(v, 0)) for v, l in Lead.Status.choices])
    total_leads = leads.count()
    contacted = total_leads - by_status.get("new", 0)
    with_case = leads.filter(cases__isnull=False).distinct().count()

    # new leads per week (12 weeks)
    start_week = today - timedelta(weeks=11)
    wk = {r["w"].date() if hasattr(r["w"], "date") else r["w"]: r["n"]
          for r in leads.filter(created_at__date__gte=start_week - timedelta(days=7)).annotate(w=TruncWeek("created_at")).values("w").annotate(n=Count("id"))}
    weeks, cur = [], start_week - timedelta(days=start_week.weekday())
    for _ in range(12):
        weeks.append((cur.strftime("%d/%m"), wk.get(cur, 0)))
        cur += timedelta(weeks=1)
    top_w = max([n for _, n in weeks] + [1])
    new_leads = [{"label": l, "value": n, "pct": _pct(n, top_w)} for l, n in weeks]

    # cases
    stage_counts = {r["stage"]: r["n"] for r in cases.values("stage").annotate(n=Count("id"))}
    stages = _bars([(v, l, stage_counts.get(v, 0)) for v, l in Case.Stage.choices])
    type_counts = {r["case_type"]: r["n"] for r in cases.values("case_type").annotate(n=Count("id"))}
    types = _bars([(v, l, type_counts.get(v, 0)) for v, l in Case.types_for(p.workspace)])

    # fees per month (6 months), paid vs unpaid
    months = [_add_months(this_month, -i) for i in range(5, -1, -1)]
    closed = cases.filter(stage=Case.Stage.CLOSED, closed_at__date__gte=months[0])
    paid, unpaid = {}, {}
    for c in closed.values("closed_at", "fee", "fee_paid"):
        key = _month_floor(c["closed_at"].date())
        (paid if c["fee_paid"] else unpaid)[key] = (paid if c["fee_paid"] else unpaid).get(key, 0) + float(c["fee"] or 0)
    top_f = max([paid.get(m, 0) + unpaid.get(m, 0) for m in months] + [1])
    fees_cols = [{"label": m.strftime("%m/%y"), "paid": paid.get(m, 0), "unpaid": unpaid.get(m, 0),
                  "paid_pct": _pct(paid.get(m, 0), top_f), "unpaid_pct": _pct(unpaid.get(m, 0), top_f)} for m in months]

    open_qs = cases.exclude(stage__in=[Case.Stage.CLOSED, Case.Stage.CANCELLED])
    pipeline = open_qs.aggregate(t=Sum("fee"))["t"] or 0
    approved = cases.filter(stage=Case.Stage.APPROVED).aggregate(t=Sum("fee"))["t"] or 0
    closed_period = cases.filter(stage=Case.Stage.CLOSED, closed_at__date__gte=since)
    agg = closed_period.aggregate(n=Count("id"), fees=Sum("fee"), avg=Avg("fee"),
                                  dur=Avg(ExpressionWrapper(F("closed_at") - F("opened_at"), output_field=DurationField())))
    avg_days = round(agg["dur"].total_seconds() / 86400, 1) if agg["dur"] else None

    # goals (current month)
    goal = Goal.objects.filter(workspace=p.workspace, month=this_month).first()
    month_closed = cases.filter(stage=Case.Stage.CLOSED, closed_at__date__gte=this_month)
    m_agg = month_closed.aggregate(n=Count("id"), fees=Sum("fee"))
    goal_info = None
    if goal and show_money:
        goal_info = {
            "fees_target": goal.fees_target, "fees_actual": m_agg["fees"] or 0, "fees_pct": min(100, _pct(m_agg["fees"] or 0, goal.fees_target)),
            "cases_target": goal.cases_target, "cases_actual": m_agg["n"] or 0, "cases_pct": min(100, _pct(m_agg["n"] or 0, goal.cases_target)),
        }

    # team table (manager)
    team = []
    if p.is_manager:
        for u in User.objects.filter(is_active=True, profile__workspace=p.workspace).order_by("username"):
            ul = Lead.objects.filter(workspace=p.workspace, assigned_to=u)
            uc = Case.objects.filter(workspace=p.workspace, owner=u)
            team.append({
                "name": u.get_full_name() or u.username, "role": u.profile.get_role_display(),
                "leads": ul.count(), "contacted": ul.exclude(status="new").count(),
                "interested": ul.filter(status__in=["interested", "in_progress", "closed"]).count(),
                "open": uc.exclude(stage__in=["closed", "cancelled"]).count(),
                "closed": uc.filter(stage="closed", closed_at__date__gte=since).count(),
                "fees": uc.filter(stage="closed", closed_at__date__gte=since).aggregate(t=Sum("fee"))["t"] or 0,
            })

    return render(request, "leads/dashboard.html", {
        "days": days, "show_money": show_money, "funnel": funnel, "new_leads": new_leads, "stages": stages, "types": types,
        "fees_cols": fees_cols, "pipeline": pipeline, "approved": approved, "goal": goal_info, "goal_obj": goal,
        "team": team, "is_mgr": p.is_manager,
        "kpi": {
            "leads": total_leads, "contact_rate": _pct(contacted, total_leads), "conversion": _pct(with_case, total_leads),
            "open_cases": open_qs.count(), "closed_n": agg["n"] or 0, "closed_fees": agg["fees"] or 0,
            "avg_fee": agg["avg"], "avg_days": avg_days,
        },
    })
