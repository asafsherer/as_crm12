from django.db.models import Q

from .models import Case, Document, EmailMessage, Lead, UserProfile


def prof(user):
    p, _ = UserProfile.objects.get_or_create(
        user=user,
        defaults={"role": UserProfile.Role.MANAGER if user.is_superuser else UserProfile.Role.SALES},
    )
    return p


def own_leads(user):
    p = prof(user)
    qs = Lead.objects.filter(workspace=p.workspace)
    return qs if p.is_manager else qs.filter(assigned_to=user)


def shared_leads(user):
    return Lead.objects.filter(shares__shared_with=user)


def leads_for(user):
    """Own leads plus leads someone shared with this user (shared ones can be opened and worked on)."""
    return (own_leads(user) | shared_leads(user)).distinct()


def can_share_lead(user, lead):
    p = prof(user)
    return own_leads(user).filter(pk=lead.pk).exists() and (p.is_manager or lead.assigned_to_id == user.id)


def own_cases(user):
    p = prof(user)
    if p.role == UserProfile.Role.SALES:
        return Case.objects.none()
    qs = Case.objects.filter(workspace=p.workspace)
    return qs if p.is_manager else qs.filter(owner=user)


def cases_for(user):
    own = own_cases(user)
    shared = Case.objects.filter(shares__shared_with=user)
    return (own | shared).distinct()


def case_access(user, case):
    """'own' (same workspace), 'shared' (shared with this user) or None."""
    if own_cases(user).filter(pk=case.pk).exists():
        return "own"
    if case.shares.filter(shared_with=user).exists():
        return "shared"
    return None


def docs_for(user, case):
    docs = case.documents.select_related("uploaded_by")
    if case.workspace == prof(user).workspace:
        return docs
    return docs.filter(is_shared=True)


def can_see_document(user, doc):
    access = case_access(user, doc.case)
    if access == "own":
        return True
    return access == "shared" and doc.is_shared


def mail_for(user):
    """Emails a person may see: managers see their whole workspace; advisors see mail routed to them or from their own contacts.
    Mail a manager passed on to someone else stays visible to that manager."""
    p = prof(user)
    if p.role == UserProfile.Role.SALES:
        return EmailMessage.objects.none()
    own = Q(owner=user) | Q(moved_by=user)
    if p.is_manager:
        return EmailMessage.objects.filter(Q(workspace=p.workspace) | own)
    return EmailMessage.objects.filter(Q(workspace=p.workspace, lead__in=leads_for(user)) | own)
