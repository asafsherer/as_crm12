import os
import uuid

from django.conf import settings
from django.db import models
from .crypto import nid_hash
from .fields import EncryptedCharField
from .utils import whatsapp_link


class Lead(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "חדש"
        NO_ANSWER = "no_answer", "לא ענה"
        CALLBACK = "callback", "חזרה בעוד זמן"
        INTERESTED = "interested", "מעוניין"
        IN_PROGRESS = "in_progress", "בטיפול"
        CLOSED = "closed", "נסגר"
        NOT_RELEVANT = "not_relevant", "לא רלוונטי"
        DO_NOT_CALL = "do_not_call", "לא להתקשר"

    national_id = EncryptedCharField("ת״ז")  # encrypted at rest
    national_id_hash = models.CharField(max_length=64, blank=True, default="", editable=False)  # fingerprint for lookups
    first_name = models.CharField("שם פרטי", max_length=100, blank=True)
    last_name = models.CharField("שם משפחה", max_length=100, blank=True)
    birth_date = models.DateField("תאריך לידה", null=True, blank=True)
    age = models.PositiveSmallIntegerField("גיל", null=True, blank=True)
    gender = models.CharField("מין", max_length=20, blank=True)
    city = models.CharField("עיר", max_length=100, blank=True)
    street = models.CharField("רחוב", max_length=150, blank=True)
    house_number = models.CharField("מספר בית", max_length=20, blank=True)
    zip_code = models.CharField("מיקוד", max_length=10, blank=True)
    phone = models.CharField("טלפון", max_length=20, blank=True)
    phone2 = models.CharField("טלפון נוסף", max_length=20, blank=True)
    email = models.EmailField("אימייל", blank=True)

    id_valid = models.BooleanField("ת״ז תקינה", default=True)
    shared_phone = models.BooleanField("טלפון משותף", default=False)
    source = models.CharField("מקור הליד", max_length=60, blank=True, default="", db_index=True)
    # --- website / campaign intake (v10) ---
    campaign = models.CharField("קמפיין", max_length=100, blank=True, default="", db_index=True)
    utm_medium = models.CharField(max_length=60, blank=True, default="")
    utm_term = models.CharField("מילת חיפוש", max_length=120, blank=True, default="")
    landing_page = models.CharField("דף נחיתה", max_length=200, blank=True, default="")
    form_message = EncryptedCharField("הודעה מהטופס באתר", blank=True)  # free text typed by the visitor, encrypted
    interest = models.CharField("תחום עניין", max_length=20, blank=True, default="")
    lead_score = models.PositiveSmallIntegerField("ניקוד ליד", null=True, blank=True)
    intake_at = models.DateTimeField("נקלט מהאתר", null=True, blank=True, db_index=True)
    web_consent_at = models.DateTimeField("אישור יצירת קשר באתר", null=True, blank=True)
    call_time = models.CharField("זמן נוח לשיחה", max_length=20, blank=True)
    owns_property = models.CharField("בעלות על נכס", max_length=10, blank=True)
    enforcement_file = models.CharField("תיק בהוצל״פ", max_length=10, blank=True)

    workspace = models.CharField("מרחב עבודה", max_length=10, default="finance", db_index=True)
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="assigned_leads", verbose_name="מטפל")
    status = models.CharField("סטטוס", max_length=20, choices=Status.choices, default=Status.NEW, db_index=True)
    callback_on = models.DateField("לחזור בתאריך", null=True, blank=True, db_index=True)
    call_attempts = models.PositiveSmallIntegerField("ניסיונות חיוג", default=0)
    last_contact = models.DateTimeField("קשר אחרון", null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["last_name", "first_name"]
        # website leads arrive without an ID number, so a blank fingerprint must not collide
        constraints = [models.UniqueConstraint(fields=["workspace", "national_id_hash"], name="uniq_lead_workspace_nid_hash",
                                               condition=~models.Q(national_id_hash=""))]
        verbose_name = "ליד"
        verbose_name_plural = "לידים"

    def __str__(self):
        return self.full_name

    def save(self, *args, **kwargs):
        self.national_id_hash = nid_hash(self.national_id)
        uf = kwargs.get("update_fields")
        if uf is not None and "national_id" in uf:
            kwargs["update_fields"] = list(uf) + ["national_id_hash"]
        super().save(*args, **kwargs)

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip() or "ליד ללא שם"

    @property
    def best_phone(self):
        return self.phone or self.phone2

    @property
    def whatsapp_url(self):
        return whatsapp_link(self.best_phone)

    @property
    def address(self):
        parts = [f"{self.street} {self.house_number}".strip(), self.city]
        return ", ".join(p for p in parts if p)


class Case(models.Model):
    class Type(models.TextChoices):
        PENSION = "pension", "משיכת פנסיה"
        CONSOLIDATION = "consolidation", "איחוד הלוואות"
        PROPERTY_LOAN = "property_loan", "הלוואה על נכס"
        REFINANCE = "refinance", "מחזור משכנתא"
        ENFORCEMENT = "enforcement", "הוצאה לפועל"
        OTHER = "other", "אחר"

    class Stage(models.TextChoices):
        OPENED = "opened", "נפתח"
        DOCS = "docs", "איסוף מסמכים"
        SUBMITTED = "submitted", "הוגש"
        APPROVED = "approved", "אושר"
        CLOSED = "closed", "נסגר"
        CANCELLED = "cancelled", "בוטל"

    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="cases")
    workspace = models.CharField("מרחב עבודה", max_length=10, default="finance", db_index=True)
    case_type = models.CharField("סוג תיק", max_length=20, choices=Type.choices)
    stage = models.CharField("שלב", max_length=20, choices=Stage.choices, default=Stage.OPENED, db_index=True)
    amount = models.DecimalField("סכום העסקה", max_digits=12, decimal_places=0, null=True, blank=True)
    fee = models.DecimalField("עלות תיק / עמלה", max_digits=10, decimal_places=0, null=True, blank=True)
    fee_paid = models.BooleanField("העמלה שולמה", default=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="cases")
    monthly_income = models.DecimalField("הכנסה חודשית", max_digits=10, decimal_places=0, null=True, blank=True)
    file_number = models.CharField("מספר תיק הוצל״פ / אסמכתא", max_length=60, blank=True)
    # legal workspace (enforcement and other legal cases)
    class Procedure(models.TextChoices):
        EXECUTION = "execution", "הוצאה לפועל"
        CLAIM = "claim", "תביעה כספית"
        APPEAL = "appeal", "ערעור"
        MOTION = "motion", "בקשה או הליך ביניים"
        OTHER = "other", "אחר"

    debtor_name = models.CharField("שם החייב", max_length=150, blank=True)
    creditor_name = models.CharField("שם הזוכה", max_length=150, blank=True)
    procedure_type = models.CharField("סוג ההליך", max_length=12, choices=Procedure.choices, blank=True)
    principal_amount = models.DecimalField("סכום הקרן", max_digits=12, decimal_places=0, null=True, blank=True)
    registrar_judge = models.CharField("רשם / שופט", max_length=120, blank=True)
    ai_consent = models.BooleanField("הלקוח אישר ניתוח AI", default=False)
    ai_consent_at = models.DateTimeField(null=True, blank=True)
    summary = models.TextField("תקציר", blank=True)
    # per-case document requirements: {"states": {key: required|optional|off}, "confirmed_at": iso, "confirmed_missing": [keys]}
    doc_settings = models.JSONField(default=dict, blank=True)
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "תיק"
        verbose_name_plural = "תיקים"

    def __str__(self):
        return f"{self.get_case_type_display()} - {self.lead.full_name}"

    @classmethod
    def types_for(cls, workspace):
        """Case types a person in this workspace can open: finance types or enforcement, plus 'other'."""
        legal = {cls.Type.ENFORCEMENT.value, cls.Type.OTHER.value}
        finance = {t.value for t in cls.Type} - {cls.Type.ENFORCEMENT.value}
        allowed = legal if workspace == "legal" else finance
        return [(v, l) for v, l in cls.Type.choices if v in allowed]


class Document(models.Model):
    class Category(models.TextChoices):
        ID_FRONT = "id_front", "ת״ז - צד קדמי"
        ID_BACK = "id_back", "ת״ז - צד אחורי"
        ID = "id", "תעודת זהות (שני הצדדים בקובץ אחד)"
        BANK_CERT = "bank_cert", "אישור ניהול חשבון"
        TAX_EXEMPT = "tax_exempt", "אישור פטור ממס"
        NI_EMPLOYERS = "ni_employers", "דוח סיום מעסיקים (ביטוח לאומי)"
        PAYSLIP = "payslip", "תלושי שכר"
        ASSESSMENT = "assessment", "שומת מס"
        BANK = "bank", "דפי בנק"
        CREDIT = "credit", "דוח נתוני אשראי"
        BALANCES = "balances", "דוח יתרות"
        AMORT = "amort", "דוח סילוקין משכנתא"
        LOANS = "loans", "פירוט הלוואות"
        PROPERTY = "property", "מסמכי נכס"
        PENSION = "pension", "דוח פנסיה / קופות"
        POA = "poa", "ייפוי כוח / הסכם שכר טרחה"
        DEBT_TITLE = "debt_title", "אסמכתא לחוב"
        DEBT_CALC = "debt_calc", "חישוב חוב מעודכן"
        SERVICE = "service", "אישור מסירה / התראה"
        COURT = "court", "החלטות ומסמכי הוצל״פ"
        CORRESPONDENCE = "correspondence", "תכתובת עם החייב"
        FEE_RECEIPT = "fee_receipt", "קבלת אגרה"
        CONTRACT = "contract", "חוזה חתום"
        RECEIPT = "receipt", "קבלה / חשבונית"
        OTHER = "other", "אחר"

    ALLOWED_EXT = {".pdf", ".jpg", ".jpeg", ".png", ".heic", ".doc", ".docx", ".xls", ".xlsx"}
    MAX_BYTES = 15 * 1024 * 1024

    def _path(instance, filename):
        ext = os.path.splitext(filename)[1].lower()
        return f"cases/{instance.case_id}/{uuid.uuid4().hex}{ext}"

    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name="documents")
    category = models.CharField("סוג מסמך", max_length=20, choices=Category.choices, default=Category.OTHER)
    file = models.FileField(upload_to=_path)
    is_shared = models.BooleanField("משותף עם השותף", default=False)
    custom_name = models.CharField("שם מסמך (חופשי)", max_length=120, blank=True)
    original_name = models.CharField(max_length=255)
    size = models.PositiveIntegerField(default=0)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    @property
    def display_name(self):
        return self.custom_name or self.get_category_display()

    class Meta:
        ordering = ["-uploaded_at"]



class Note(models.Model):
    class Kind(models.TextChoices):
        NOTE = "note", "הערה"
        STATUS = "status", "שינוי סטטוס"
        AI = "ai", "סיכום AI"

    class Channel(models.TextChoices):
        PHONE = "phone", "שיחת טלפון"
        WHATSAPP = "whatsapp", "וואטסאפ"
        SMS = "sms", "SMS"
        MEETING = "meeting", "פגישה"
        EMAIL = "email", "אימייל"

    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="notes")
    case = models.ForeignKey(Case, null=True, blank=True, on_delete=models.CASCADE, related_name="notes")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.NOTE)
    channel = models.CharField(max_length=10, choices=Channel.choices, blank=True)
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class UserProfile(models.Model):
    class Role(models.TextChoices):
        MANAGER = "manager", "מנהל"
        ADVISOR = "advisor", "יועץ"
        SALES = "sales", "נציג מכירות"

    class Workspace(models.TextChoices):
        FINANCE = "finance", "פיננסים"
        LEGAL = "legal", "הוצל״פ"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField("תפקיד", max_length=10, choices=Role.choices, default=Role.SALES)
    workspace = models.CharField("מרחב עבודה", max_length=10, choices=Workspace.choices, default=Workspace.FINANCE)
    totp_secret = EncryptedCharField(blank=True)  # encrypted at rest
    totp_enabled = models.BooleanField("התחברות דו-שלבית פעילה", default=False)

    class Meta:
        verbose_name = "פרופיל משתמש"
        verbose_name_plural = "פרופילי משתמשים"

    def __str__(self):
        return f"{self.user.get_username()} ({self.get_role_display()}, {self.get_workspace_display()})"

    @property
    def is_manager(self):
        return self.role == self.Role.MANAGER

    @property
    def pii_ok(self):
        return self.role in (self.Role.MANAGER, self.Role.ADVISOR)


class CaseShare(models.Model):
    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name="shares")
    shared_with = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="shared_cases")
    shared_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("case", "shared_with")


class LeadShare(models.Model):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="shares")
    shared_with = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="shared_leads")
    shared_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("lead", "shared_with")


class Task(models.Model):
    class Priority(models.TextChoices):
        NORMAL = "normal", "רגיל"
        URGENT = "urgent", "דחוף"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tasks")
    title = models.CharField(max_length=200)
    due_on = models.DateField(null=True, blank=True, db_index=True)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.NORMAL)
    done = models.BooleanField(default=False)
    done_at = models.DateTimeField(null=True, blank=True)
    lead = models.ForeignKey(Lead, null=True, blank=True, on_delete=models.CASCADE, related_name="tasks")
    case = models.ForeignKey(Case, null=True, blank=True, on_delete=models.CASCADE, related_name="tasks")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["done", "due_on", "-created_at"]


class Message(models.Model):
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sent_messages")
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="received_messages")
    text = models.TextField()
    case = models.ForeignKey(Case, null=True, blank=True, on_delete=models.SET_NULL, related_name="messages")
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]


class AuditLog(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=30, db_index=True)
    detail = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]


class DocumentAnalysis(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "ממתין לאישור"
        CONFIRMED = "confirmed", "אושר"

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="analyses")
    data = models.JSONField(default=dict)
    summary = models.TextField(blank=True)
    warnings = models.JSONField(default=list)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    model_name = models.CharField(max_length=60, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class DocTemplate(models.Model):
    name = models.CharField("שם התבנית", max_length=120)
    workspace = models.CharField(max_length=10, default="finance")
    case_type = models.CharField("מתאים לסוג תיק", max_length=20, blank=True)
    file = models.FileField(upload_to="templates/")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]


class Goal(models.Model):
    workspace = models.CharField(max_length=10, default="finance")
    month = models.DateField(help_text="היום הראשון של החודש")
    fees_target = models.DecimalField("יעד הכנסות (עלות תיקים)", max_digits=12, decimal_places=0, default=0)
    cases_target = models.PositiveIntegerField("יעד תיקים שנסגרו", default=0)

    class Meta:
        unique_together = ("workspace", "month")


class MarketingCost(models.Model):
    """Monthly marketing spend per lead source. Entered by a manager; used by the business manager worker."""
    workspace = models.CharField(max_length=10, default="finance")
    month = models.DateField(help_text="היום הראשון של החודש")
    source = models.CharField("מקור", max_length=60)
    amount = models.DecimalField("עלות (₪)", max_digits=10, decimal_places=0)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-month", "source"]
        unique_together = ("workspace", "month", "source")


class AdReport(models.Model):
    """A Google Ads report entered by hand: a CSV/Excel export, screenshots (read by AI into a DRAFT a person corrects), or typed rows.
    Only confirmed rows are used in the numbers."""

    class Status(models.TextChoices):
        DRAFT = "draft", "ממתין לבדיקה"
        CONFIRMED = "confirmed", "אושר"

    workspace = models.CharField(max_length=10, default="finance", db_index=True)
    title = models.CharField("שם הדוח", max_length=120, blank=True)
    period_start = models.DateField()
    period_end = models.DateField()
    rows = models.JSONField(default=list)  # [{campaign, cost, clicks, impressions, conversions}]
    notes = models.JSONField(default=list)  # other things seen/typed (keywords, ads, budgets...) for the reviewer
    warnings = models.JSONField(default=list)
    source = models.CharField(max_length=10, default="manual")  # csv | image | manual
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    narrative = models.JSONField(default=dict, blank=True)  # {summary, tips, unverified, used_ai, at}
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class AdReportImage(models.Model):
    report = models.ForeignKey(AdReport, on_delete=models.CASCADE, related_name="images")
    file = models.FileField(upload_to="ads/")


class AgentRun(models.Model):
    """One run of an AI worker. Workers only PROPOSE: nothing here changes a case, stage or message by itself."""

    class Agent(models.TextChoices):
        OPS = "ops", "מתפעלת"
        REVIEWER = "reviewer", "מנהל תפעול"
        BUSINESS = "business", "מנהל עסקי"
        MAIL = "mail", "מתפעל מיילים"
        CAMPAIGN = "campaign", "קמפיינר"

    agent = models.CharField(max_length=10, choices=Agent.choices, db_index=True)
    workspace = models.CharField(max_length=10, default="finance", db_index=True)
    case = models.ForeignKey(Case, null=True, blank=True, on_delete=models.CASCADE, related_name="agent_runs")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    facts = models.JSONField(default=dict)    # numbers computed by code (no ID numbers, phones or addresses)
    output = models.JSONField(default=dict)
    summary = models.TextField(blank=True)
    used_ai = models.BooleanField(default=False)
    model_name = models.CharField(max_length=60, blank=True)
    error = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]


class EmailMessage(models.Model):
    """An email kept in the CRM. Subject and body are encrypted at rest. Nothing is filed into a case without a person's approval."""

    class Direction(models.TextChoices):
        IN = "in", "נכנס"
        OUT = "out", "יוצא"

    class Category(models.TextChoices):
        RECEIPT = "receipt", "קבלה / חשבונית"
        DOCUMENTS = "documents", "מסמכים מלקוח"
        CLIENT = "client", "הודעה מלקוח"
        OTHER = "other", "אחר"

    class Status(models.TextChoices):
        NEW = "new", "חדש"
        FILED = "filed", "טופל"
        IGNORED = "ignored", "התעלמות"

    message_id = models.CharField(max_length=255, unique=True)
    workspace = models.CharField(max_length=10, default="finance", db_index=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="mail_owned")
    moved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    lead = models.ForeignKey(Lead, null=True, blank=True, on_delete=models.SET_NULL, related_name="emails")
    case = models.ForeignKey(Case, null=True, blank=True, on_delete=models.SET_NULL, related_name="emails")
    direction = models.CharField(max_length=3, choices=Direction.choices, default=Direction.IN)
    from_addr = models.CharField(max_length=254, blank=True, db_index=True)
    to_addrs = models.TextField(blank=True)
    subject = EncryptedCharField(blank=True)
    body = EncryptedCharField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True, db_index=True)
    received_at = models.DateTimeField(auto_now_add=True)
    category = models.CharField(max_length=10, choices=Category.choices, default=Category.OTHER, db_index=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.NEW, db_index=True)
    hints = models.JSONField(default=list, blank=True)  # code-made suggestions, e.g. which missing documents the attachments could fill

    class Meta:
        ordering = ["-sent_at", "-received_at"]
        verbose_name = "מייל"
        verbose_name_plural = "מיילים"

    def __str__(self):
        return f"{self.from_addr} ({self.sent_at:%d/%m/%Y})" if self.sent_at else self.from_addr


class EmailAttachment(models.Model):
    def _path(instance, filename):
        ext = os.path.splitext(filename)[1].lower()
        return f"mail/{instance.email_id}/{uuid.uuid4().hex}{ext}"

    email = models.ForeignKey(EmailMessage, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to=_path)  # same encrypted storage as case documents
    original_name = models.CharField(max_length=255)
    size = models.PositiveIntegerField(default=0)
    suggested_category = models.CharField(max_length=20, choices=Document.Category.choices, default=Document.Category.OTHER)
    document = models.ForeignKey(Document, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")  # set once filed


class StatusDef(models.Model):
    """A lead status shown on the board: its label and color. The 8 built-in statuses keep the logic the queue relies on;
    managers can add more (custom ones are plain labels)."""
    workspace = models.CharField(max_length=10, default="finance", db_index=True)
    key = models.CharField(max_length=30)
    label = models.CharField("שם הסטטוס", max_length=40)
    color = models.CharField(max_length=7, default="#3B82F6")
    sort = models.PositiveSmallIntegerField(default=100)
    builtin = models.BooleanField(default=False)

    class Kind(models.TextChoices):
        NORMAL = "normal", "בתהליך"
        WON = "won", "נסגר"
        LOST = "lost", "לא רלוונטי"

    kind = models.CharField(max_length=6, choices=Kind.choices, default=Kind.NORMAL)

    class Meta:
        ordering = ["sort", "id"]
        unique_together = ("workspace", "key")
