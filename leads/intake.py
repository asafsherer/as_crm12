"""Website lead intake. The website's server-side function (never the visitor's browser) posts here with a secret token.

POST /api/intake/   header  X-Intake-Token: <LEAD_INTAKE_TOKEN>
JSON or form fields: name (or first_name,last_name), phone, email, message, interest, consent (required),
                     call_time (any|morning|noon|afternoon|evening), owns_property (yes|no|joint),
                     enforcement_file (yes|no|unsure; yes -> legal workspace), utm_source, utm_medium, utm_campaign, utm_term, page, website (hidden honeypot: must stay empty)
"""
import hmac
import json
import re
from datetime import date

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.http import Http404, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import agents
from .models import Lead, Note, Task, UserProfile
from .utils import normalize_phone
from .views import audit, client_ip

User = get_user_model()
RATE_LIMIT, RATE_WINDOW = 60, 60  # per sending address; the website function is the sender, so keep this above peak campaign traffic
MAX_BODY = 20_000
CALL_TIME_LABEL = {"any": "כל שעה", "morning": "בוקר", "noon": "צהריים", "afternoon": "אחה״צ", "evening": "ערב"}
OWNS_LABEL = {"yes": "כן", "no": "לא", "joint": "בבעלות משותפת"}
ENF_LABEL = {"yes": "יש", "no": "אין", "unsure": "לא בטוח"}


def _clean(v, n):
    return re.sub(r"[\x00-\x08\x0b-\x1f]", "", str(v or "")).strip()[:n]


def _assignee(workspace):
    qs = User.objects.filter(is_active=True, profile__workspace=workspace)
    return (qs.filter(profile__role=UserProfile.Role.MANAGER).order_by("id").first()
            or qs.filter(profile__role=UserProfile.Role.ADVISOR).order_by("id").first())


@csrf_exempt
@require_POST
def api_intake(request):
    token = settings.LEAD_INTAKE_TOKEN
    if not token:
        raise Http404
    supplied = request.headers.get("X-Intake-Token", "")
    if not hmac.compare_digest(supplied.encode(), token.encode()):
        return JsonResponse({"ok": False, "error": "unauthorized"}, status=401)
    key = f"intake:{client_ip(request)}"
    cache.add(key, 0, RATE_WINDOW)
    try:
        if cache.incr(key) > RATE_LIMIT:
            return JsonResponse({"ok": False, "error": "rate_limited"}, status=429)
    except ValueError:
        pass
    if len(request.body or b"") > MAX_BODY:
        return JsonResponse({"ok": False, "error": "too_large"}, status=413)
    try:
        data = json.loads(request.body) if request.content_type == "application/json" else request.POST.dict()
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        return JsonResponse({"ok": False, "error": "bad_request"}, status=400)

    if data.get("website"):  # honeypot: bots fill hidden fields. Pretend success, store nothing.
        return JsonResponse({"ok": True})
    if str(data.get("consent", "")).lower() not in ("1", "true", "on", "yes"):
        return JsonResponse({"ok": False, "error": "consent_required"}, status=400)

    phone = normalize_phone(data.get("phone"))
    if not phone:
        return JsonResponse({"ok": False, "error": "phone_required"}, status=400)
    email = _clean(data.get("email"), 254).lower()
    if email:
        try:
            validate_email(email)
        except ValidationError:
            email = ""
    first = _clean(data.get("first_name"), 100)
    last = _clean(data.get("last_name"), 100)
    if not first and data.get("name"):
        parts = _clean(data.get("name"), 200).split(None, 1)
        first, last = parts[0], (parts[1] if len(parts) > 1 else "")
    if not first:
        return JsonResponse({"ok": False, "error": "name_required"}, status=400)
    message = _clean(data.get("message"), 2000)
    page = _clean(data.get("page"), 200)
    utm_source, utm_medium = _clean(data.get("utm_source"), 60), _clean(data.get("utm_medium"), 60)
    campaign, term = _clean(data.get("utm_campaign"), 100), _clean(data.get("utm_term"), 120)
    call_time = _clean(data.get("call_time"), 20)
    owns = _clean(data.get("owns_property"), 10).lower()
    enf = _clean(data.get("enforcement_file"), 10).lower()
    owns = owns if owns in ("yes", "no", "joint") else ""
    enf = enf if enf in ("yes", "no", "unsure") else ""
    interest = _clean(data.get("interest"), 20)
    if interest not in agents.INTEREST_LABEL:
        interest = ""
    interest = interest or ("enforcement" if enf == "yes" else agents.detect_interest(message, page, term, campaign))
    workspace = "legal" if interest == "enforcement" else "finance"  # enforcement enquiries go to the legal workspace
    if not _assignee(workspace):
        workspace = "finance"
    now = timezone.now()

    # same person again (same phone or e-mail): add a note to the existing lead instead of a duplicate
    existing = (Lead.objects.filter(workspace=workspace, phone=phone).first()
                or Lead.objects.filter(workspace=workspace, phone2=phone).first()
                or (Lead.objects.filter(workspace=workspace, email__iexact=email).first() if email else None))
    if existing:
        Note.objects.create(lead=existing, kind=Note.Kind.NOTE, channel="",
                            text="פנייה חוזרת מהאתר" + (f" ({campaign})" if campaign else "") + (f": {message[:300]}" if message else ""))
        existing.last_contact = now
        existing.save(update_fields=["last_contact", "updated_at"])
        owner = existing.assigned_to or _assignee(workspace)
        if owner:
            Task.objects.create(owner=owner, lead=existing, title=f"פנייה חוזרת מהאתר: {existing.full_name}", due_on=date.today(),
                                priority=Task.Priority.URGENT)
        return JsonResponse({"ok": True})

    paid = utm_medium.lower() in ("cpc", "ppc", "paid", "paidsearch", "paid_social", "display")
    score, _ = agents.score_lead(phone, email, f"{first} {last}".strip(), message, interest, paid)
    owner = _assignee(workspace)
    lead = Lead.objects.create(
        workspace=workspace, assigned_to=owner, first_name=first, last_name=last, phone=phone, email=email,
        source=agents.source_label(utm_source, utm_medium), campaign=campaign, utm_medium=utm_medium, utm_term=term,
        landing_page=page, form_message=message, call_time=call_time, owns_property=owns, enforcement_file=enf, interest=interest, lead_score=score, intake_at=now, web_consent_at=now,
        id_valid=True)
    extra = "".join(f" · {l}: {v}" for l, v in (
        ("זמן נוח לשיחה", CALL_TIME_LABEL.get(call_time, call_time)), ("בעל נכס", OWNS_LABEL.get(owns, "")),
        ("תיק בהוצל״פ", ENF_LABEL.get(enf, ""))) if v)
    Note.objects.create(lead=lead, kind=Note.Kind.NOTE, text="נקלט מהאתר" + extra + (f" · דף: {page}" if page else "")
                        + (f" · קמפיין: {campaign}" if campaign else ""))
    if owner:
        Task.objects.create(owner=owner, lead=lead, title=f"ליד חדש מהאתר: {lead.full_name}", due_on=date.today(),
                            priority=Task.Priority.URGENT if score >= agents.HOT else Task.Priority.NORMAL)
    audit(None, "intake", f"ליד חדש מהאתר ({lead.source})")
    return JsonResponse({"ok": True})
