"""Google Ads reports typed/uploaded by hand: screens for the campaigner."""
import re
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import adsreport, ai, uploads
from .models import AdReport, AdReportImage, UserProfile
from .perms import own_leads, prof
from .views import audit


def _guard(request):
    return prof(request.user).role != UserProfile.Role.SALES


def _day(v, default):
    try:
        return date.fromisoformat(v)
    except (TypeError, ValueError):
        return default


def _clean_line(s, n):
    return re.sub(r"[\x00-\x1f]", " ", str(s or "")).strip()[:n]


@login_required
def ads_home(request):
    if not _guard(request):
        return redirect("home")
    p = prof(request.user)
    today = timezone.localdate()
    return render(request, "leads/ads_home.html", {
        "reports": AdReport.objects.filter(workspace=p.workspace)[:30], "ai_enabled": ai.enabled(),
        "default_start": today - timedelta(days=30), "default_end": today,
    })


@login_required
@require_POST
def ads_upload(request):
    if not _guard(request):
        return redirect("home")
    p = prof(request.user)
    today = timezone.localdate()
    start, end = _day(request.POST.get("start"), today - timedelta(days=30)), _day(request.POST.get("end"), today)
    if start > end:
        start, end = end, start
    table, shots = request.FILES.get("table"), request.FILES.getlist("shots")
    typed = [_clean_line(x, 300) for x in request.POST.get("notes", "").splitlines() if x.strip()][:20]
    rows, notes, warnings, source = [], list(typed), [], "manual"
    try:
        if table:
            if not table.name.lower().endswith((".csv", ".tsv", ".txt", ".xlsx", ".xlsm")):
                raise ValueError("סוג הקובץ לא נתמך. העלה CSV או Excel מ-Google Ads.")
            if table.size > 5 * 1024 * 1024:
                raise ValueError("הקובץ גדול מ-5MB.")
            rows, warnings = adsreport.rows_from_upload(table)
            source = "csv"
        if shots:
            if not ai.enabled():
                raise ValueError("קריאת צילומי מסך דורשת מפתח AI. אפשר להעלות CSV/Excel או להקליד את השורות.")
            if cache.get(f"ads_ai:{request.user.pk}"):
                raise ValueError("הקריאה הורצה הרגע. נסה שוב בעוד רגע.")
            cache.set(f"ads_ai:{request.user.pk}", 1, 20)
            for f in shots[:adsreport.MAX_IMAGES]:
                uploads.validate_upload(f)
            s_rows, s_notes, s_warn = adsreport.rows_from_images(shots)
            if not rows:
                rows, source = s_rows, "image"
            notes += s_notes
            warnings += s_warn
    except (ValueError, uploads.UploadRejected, ai.AIError) as exc:
        messages.error(request, str(exc) or "הקובץ נדחה.")
        return redirect("ads_home")
    report = AdReport.objects.create(workspace=p.workspace, title=_clean_line(request.POST.get("title"), 120), period_start=start,
                                     period_end=end, rows=rows, notes=notes, warnings=warnings, source=source, created_by=request.user)
    for f in shots[:adsreport.MAX_IMAGES]:
        f.seek(0)
        AdReportImage.objects.create(report=report, file=f)
    audit(request.user, "ads_report_create", f"דוח גוגל אדס {start}–{end} ({source})")
    return redirect("ads_report", pk=report.pk)


@login_required
def ads_report(request, pk):
    if not _guard(request):
        return redirect("home")
    report = get_object_or_404(AdReport, pk=pk, workspace=prof(request.user).workspace)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "save":
            try:
                rows = adsreport.rows_from_form(request.POST)
            except ValueError as exc:
                messages.error(request, str(exc))
                return redirect("ads_report", pk=pk)
            report.rows = rows
            report.notes = [_clean_line(x, 300) for x in request.POST.get("notes", "").splitlines() if x.strip()][:30]
            report.warnings = []
            report.status = AdReport.Status.CONFIRMED
            report.narrative = {}
            start, end = _day(request.POST.get("start"), report.period_start), _day(request.POST.get("end"), report.period_end)
            report.period_start, report.period_end = min(start, end), max(start, end)
            report.save()
            audit(request.user, "ads_report_confirm", f"אושר דוח גוגל אדס {report.pk}")
            messages.success(request, "הנתונים אושרו והחישוב עודכן.")
        elif action == "narrate" and report.status == AdReport.Status.CONFIRMED:
            if cache.get(f"ads_narr:{request.user.pk}"):
                messages.error(request, "הניתוח הורץ הרגע. נסה שוב בעוד רגע.")
            else:
                cache.set(f"ads_narr:{request.user.pk}", 1, 15)
                facts = adsreport.analyze(report, own_leads(request.user))
                report.narrative = {**adsreport.narrate(facts, report.notes), "at": timezone.now().isoformat()}
                report.save(update_fields=["narrative"])
                audit(request.user, "ads_report_narrate", f"ניתוח דוח {report.pk} ({'AI' if report.narrative['used_ai'] else 'קוד'})")
                if report.narrative.get("error"):
                    messages.error(request, "ה-AI לא זמין והסיכום נכתב בקוד: " + report.narrative["error"])
        elif action == "delete":
            for im in report.images.all():
                im.file.delete(save=False)
            audit(request.user, "ads_report_delete", f"נמחק דוח {report.pk}")
            report.delete()
            messages.success(request, "הדוח נמחק.")
            return redirect("ads_home")
        return redirect("ads_report", pk=pk)
    facts = adsreport.analyze(report, own_leads(request.user)) if report.rows else None
    return render(request, "leads/ads_report.html", {
        "report": report, "facts": facts, "ai_enabled": ai.enabled(), "images": report.images.all(),
        "blank_rows": range(3 if report.status == AdReport.Status.DRAFT else 1), "notes_text": "\n".join(report.notes),
    })


@login_required
def ads_image(request, pk, img):
    from django.http import FileResponse, Http404
    if not _guard(request):
        raise Http404
    report = get_object_or_404(AdReport, pk=pk, workspace=prof(request.user).workspace)
    im = get_object_or_404(report.images, pk=img)
    ext = im.file.name.lower().rsplit(".", 1)[-1]
    try:
        fh = im.file.open("rb")
    except FileNotFoundError:
        raise Http404
    return FileResponse(fh, content_type="image/png" if ext == "png" else "image/jpeg")
