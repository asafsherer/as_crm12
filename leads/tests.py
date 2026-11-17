import io
import zipfile

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client, TestCase, override_settings

from . import uploads
from .crypto import decrypt_bytes, encrypt_bytes, nid_hash
from .models import AdReport, Case, Document, Lead, UserProfile


def make_user(name, role="manager", workspace="finance"):
    u = User.objects.create_user(name, password="Str0ng-pass-123")
    p = u.profile
    p.role, p.workspace = role, workspace
    p.save()
    return u


class EncryptionTests(TestCase):
    def test_national_id_is_encrypted_in_db_and_readable_in_app(self):
        lead = Lead.objects.create(national_id="123456782", first_name="דנה")
        with connection.cursor() as c:
            c.execute("select national_id from leads_lead where id=%s", [lead.pk])
            raw = c.fetchone()[0]
        self.assertNotIn("123456782", raw)
        self.assertTrue(raw.startswith("enc1:"))
        self.assertEqual(Lead.objects.get(pk=lead.pk).national_id, "123456782")

    def test_lookup_by_hash_and_unique_per_workspace(self):
        Lead.objects.create(national_id="123456782", workspace="finance")
        self.assertTrue(Lead.objects.filter(national_id_hash=nid_hash("123456782")).exists())
        self.assertEqual(nid_hash("12345678"), nid_hash("012345678"))
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError), transaction.atomic():
            Lead.objects.create(national_id="123456782", workspace="finance")
        Lead.objects.create(national_id="123456782", workspace="legal")  # other workspace is allowed

    def test_totp_secret_encrypted(self):
        u = make_user("a")
        u.profile.totp_secret = "JBSWY3DPEHPK3PXP"
        u.profile.save()
        with connection.cursor() as c:
            c.execute("select totp_secret from leads_userprofile where user_id=%s", [u.pk])
            self.assertTrue(c.fetchone()[0].startswith("enc1:"))
        self.assertEqual(UserProfile.objects.get(user=u).totp_secret, "JBSWY3DPEHPK3PXP")

    def test_file_roundtrip_and_disk_is_ciphertext(self):
        data = b"%PDF-1.4 secret payslip"
        self.assertNotIn(b"secret", encrypt_bytes(data))
        self.assertEqual(decrypt_bytes(encrypt_bytes(data)), data)
        lead = Lead.objects.create(national_id="123456782")
        case = Case.objects.create(lead=lead, case_type="other", owner=make_user("o"))
        d = Document.objects.create(case=case, file=ContentFile(data, name="x.pdf"), original_name="x.pdf", size=len(data))
        self.assertNotIn(b"secret", open(d.file.path, "rb").read())
        with d.file.open("rb") as fh:
            self.assertEqual(fh.read(), data)


class UploadTests(TestCase):
    def f(self, name, content):
        return SimpleUploadedFile(name, content)

    def test_good_files_pass(self):
        uploads.check_content(self.f("a.pdf", b"%PDF-1.7 hi"), "a.pdf")
        uploads.check_content(self.f("a.png", b"\x89PNG\r\n\x1a\nxx"), "a.png")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", "<x/>")
        uploads.check_content(self.f("a.docx", buf.getvalue()), "a.docx")

    def test_fake_extension_is_rejected(self):
        with self.assertRaises(uploads.UploadRejected):
            uploads.check_content(self.f("a.pdf", b"MZ\x90\x00 this is an exe"), "a.pdf")
        with self.assertRaises(uploads.UploadRejected):
            uploads.check_content(self.f("a.docx", b"not a zip at all"), "a.docx")

    def test_macro_docx_rejected(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", "<x/>")
            z.writestr("word/vbaProject.bin", "evil")
        with self.assertRaises(uploads.UploadRejected):
            uploads.check_content(self.f("a.docx", buf.getvalue()), "a.docx")

    @override_settings(CLAMAV_HOST="127.0.0.1", CLAMAV_PORT=1)
    def test_av_fails_closed_when_unreachable(self):
        with self.assertRaises(uploads.UploadRejected):
            uploads.av_scan(self.f("a.pdf", b"%PDF-1.4"))


class AccessTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = make_user("owner", "advisor")
        self.other = make_user("other", "advisor")
        self.sales = make_user("rep", "sales")
        lead = Lead.objects.create(national_id="123456782", first_name="x", workspace="finance")
        self.case = Case.objects.create(lead=lead, case_type="other", owner=self.owner, workspace="finance")
        self.doc = Document.objects.create(case=self.case, file=ContentFile(b"%PDF-1.4 hi", name="a.pdf"),
                                           original_name="a.pdf", size=10, uploaded_by=self.owner)

    def login(self, user):
        c = Client()
        c.force_login(user)
        s = c.session
        s["otp_ok"] = True
        s.save()
        return c

    def test_anonymous_redirected(self):
        r = Client().get(f"/documents/{self.doc.pk}/")
        self.assertIn(r.status_code, (302, 404))
        if r.status_code == 302:
            self.assertIn("login", r.url)

    def test_sales_cannot_download(self):
        r = self.login(self.sales).get(f"/documents/{self.doc.pk}/")
        self.assertEqual(r.status_code, 404)

    def test_other_advisor_cannot_download_unshared(self):
        r = self.login(self.other).get(f"/documents/{self.doc.pk}/")
        self.assertEqual(r.status_code, 404)

    def test_owner_downloads_decrypted_content(self):
        r = self.login(self.owner).get(f"/documents/{self.doc.pk}/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(b"".join(r.streaming_content), b"%PDF-1.4 hi")

    def test_security_headers_and_no_store(self):
        r = self.login(self.owner).get("/")
        self.assertIn("default-src 'self'", r["Content-Security-Policy"])
        self.assertIn("no-store", r["Cache-Control"])
        self.assertEqual(r["X-Frame-Options"], "DENY")

    def test_login_lockout_ignores_forged_forwarded_header(self):
        c = Client()
        for i in range(5):
            c.post("/login/", {"username": "owner", "password": "bad"}, HTTP_X_FORWARDED_FOR=f"9.9.9.{i}")
        r = c.post("/login/", {"username": "owner", "password": "Str0ng-pass-123"}, HTTP_X_FORWARDED_FOR="8.8.8.8")
        self.assertContains(r, "יותר מדי ניסיונות")

    def test_2fa_lockout_survives_new_login(self):
        self.owner.profile.totp_enabled = True
        self.owner.profile.totp_secret = "JBSWY3DPEHPK3PXP"
        self.owner.profile.save()
        for _ in range(6):
            c = Client()
            c.force_login(self.owner)
            c.post("/2fa/", {"code": "000000"})
        c = Client()
        c.force_login(self.owner)
        r = c.post("/2fa/", {"code": "000000"}, follow=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn("login", r.url)


class SettingsTests(TestCase):
    def test_debug_is_off_in_tests_and_default_is_safe(self):
        import re
        src = open("config/settings.py", encoding="utf-8").read()
        self.assertTrue(re.search(r'DEBUG = os.environ.get\("DEBUG", "0"\)', src))


# ---------------------------------------------------------------- v8: AI workers
import json as _json
from datetime import timedelta as _td
from unittest import mock as _mock

from django.urls import reverse as _reverse
from django.utils import timezone as _tz

from . import agents
from .models import AgentRun, Goal, MarketingCost


class AgentTests(TestCase):
    def setUp(self):
        self.mgr = make_user("boss", "manager")
        self.adv = make_user("adv", "advisor")
        self.other = make_user("adv2", "advisor")
        self.rep = make_user("rep", "sales")
        self.lead = Lead.objects.create(national_id="123456782", first_name="דנה", last_name="כהן", phone="0501234567",
                                        city="חיפה", workspace="finance", source="פייסבוק")
        self.case = Case.objects.create(lead=self.lead, case_type="consolidation", owner=self.adv, workspace="finance")

    def login(self, user):
        c = Client()
        c.force_login(user)
        s = c.session
        s["otp_ok"] = True
        s.save()
        return c

    # --- ops clerk
    def test_checklist_and_next_steps(self):
        f = agents.case_facts(self.case)
        self.assertEqual(f["required_total"], 7)  # ת״ז x2, ניהול חשבון, דוח מעסיקים, הכנסה, אשראי/יתרות, חוזה
        self.assertEqual(len(f["missing"]), 7)
        self.assertEqual(len(f["checklist"]), 8)  # + פטור ממס (אם יש)
        self.assertTrue(any(s.startswith("לבקש מהלקוח") for s in agents.next_steps(f)))

    def test_ops_code_only_without_ai(self):
        with _mock.patch("leads.agents.ai.enabled", return_value=False):
            run = agents.run_ops(self.case, self.adv)
        self.assertFalse(run.used_ai)
        msg = run.output["message"]
        self.assertIn(agents.NAME_TOKEN, msg)
        for label in run.facts["missing"]:
            self.assertIn(label, msg)

    def test_ai_never_receives_personal_data(self):
        self.case.ai_consent = True
        self.case.save()
        seen = {}

        def fake(system, payload, max_tokens=0):
            seen["payload"] = _json.dumps(payload, ensure_ascii=False)
            return {"summary_he": "תקציר", "attention_he": [], "client_message_he": ""}

        with _mock.patch("leads.agents.ai.enabled", return_value=True), _mock.patch("leads.agents.ai.run_json", side_effect=fake):
            agents.run_ops(self.case, self.adv)
        for secret in ("123456782", "0501234567", "דנה", "כהן", "חיפה"):
            self.assertNotIn(secret, seen["payload"])

    def test_ai_message_is_validated(self):
        self.case.ai_consent = True
        self.case.save()
        bad = {"summary_he": "ok", "attention_he": [], "client_message_he": f"שלום {agents.NAME_TOKEN}, שלח 5000 שקל"}
        with _mock.patch("leads.agents.ai.enabled", return_value=True), _mock.patch("leads.agents.ai.run_json", return_value=bad):
            run = agents.run_ops(self.case, self.adv)
        self.assertNotIn("5000", run.output["message"])  # digits / missing labels -> fallback to the code draft
        self.assertEqual(run.output["message"], agents.default_message(run.facts["missing"]))

    def test_ops_without_consent_does_not_call_ai(self):
        with _mock.patch("leads.agents.ai.enabled", return_value=True), _mock.patch("leads.agents.ai.run_json") as call:
            run = agents.run_ops(self.case, self.adv)
        call.assert_not_called()
        self.assertFalse(run.used_ai)

    # --- reviewer
    def test_reviewer_flags_problems(self):
        self.case.stage, self.case.closed_at = "closed", _tz.now() - _td(days=30)
        self.case.save()
        codes = {i["code"] for i in agents.review_issues("finance")}
        self.assertTrue({"missing_docs_advanced", "closed_no_fee", "no_amount"} <= codes)

    def test_reviewer_audits_ops_message(self):
        AgentRun.objects.create(agent="ops", workspace="finance", case=self.case, facts={"missing": ["תלושי שכר", "דפי בנק"]},
                                output={"message": "שלום, חסרים תלושי שכר"})
        codes = {i["code"] for i in agents.review_issues("finance")}
        self.assertIn("ops_message_incomplete", codes)
        self.assertIn("ops_outdated", codes)

    def test_reviewer_run_without_ai(self):
        with _mock.patch("leads.agents.ai.enabled", return_value=False):
            run = agents.run_reviewer("finance", self.mgr)
        self.assertEqual(run.agent, "reviewer")
        self.assertTrue(run.summary)

    # --- business manager: maths is done in code
    def test_business_math(self):
        now = _tz.now()
        for i in range(10):
            ld = Lead.objects.create(national_id=f"{i + 1:09d}", first_name="x", workspace="finance", source="פייסבוק", call_attempts=2, status="interested")
            if i < 2:
                Case.objects.create(lead=ld, case_type="other", workspace="finance", stage="closed", fee=5000, fee_paid=True, closed_at=now)
        MarketingCost.objects.create(workspace="finance", month=_tz.localdate().replace(day=1), source="פייסבוק", amount=1000)
        Goal.objects.create(workspace="finance", month=_tz.localdate().replace(day=1), fees_target=20000)
        f = agents.business_facts("finance")
        row = next(s for s in f["by_source"] if s["source"] == "פייסבוק")
        self.assertEqual((row["leads"], row["closed"], row["revenue"], row["cost"]), (11, 2, 10000.0, 1000.0))  # 10 + setUp lead
        self.assertEqual(row["cpl"], round(1000 / 11, 2))
        self.assertEqual(row["roi_pct"], 900.0)
        self.assertEqual(f["plan"]["avg_fee"], 5000.0)
        self.assertEqual(f["plan"]["needed_closed"], 2)  # (20000 - 10000) / 5000
        self.assertEqual(f["plan"]["needed_leads"], 11)  # 2 / (2 closed of 11 leads)

    def test_business_warns_on_small_sample(self):
        f = agents.business_facts("finance")
        self.assertTrue(any("מדגם קטן" in w for w in f["warnings"]))

    def test_unverified_numbers_flagged(self):
        self.assertEqual(agents.unverified_numbers("הכנסות 12,345 ₪, תשואה 7.5% ו-99 לידים", {"a": 12345.0, "b": 7.46}), ["99"])

    def test_business_ai_text_flags_invented_numbers(self):
        fake = {"explanation_he": "יש 4321 לידים", "improvements_he": [], "risks_he": []}
        with _mock.patch("leads.agents.ai.enabled", return_value=True), _mock.patch("leads.agents.ai.run_json", return_value=fake):
            run = agents.run_business("finance", self.mgr)
        self.assertIn("4321", run.output["unverified"])

    # --- permissions & screens
    def test_screens_and_permissions(self):
        with _mock.patch("leads.agents.ai.enabled", return_value=False), _mock.patch("leads.agent_views.ai.enabled", return_value=False):
            m = self.login(self.mgr)
            self.assertEqual(m.get(_reverse("agents")).status_code, 200)
            self.assertEqual(m.get(_reverse("business")).status_code, 200)
            self.assertEqual(m.post(_reverse("review_run")).status_code, 302)
            self.assertTrue(AgentRun.objects.filter(agent="reviewer").exists())
            cache.clear()
            self.assertEqual(m.post(_reverse("business_run"), {"days": "90"}).status_code, 302)
            self.assertEqual(self.login(self.mgr).get(_reverse("business")).status_code, 200)
            a = self.login(self.adv)
            self.assertEqual(a.post(_reverse("case_ops", args=[self.case.pk])).status_code, 302)
            self.assertEqual(a.get(_reverse("case_ops", args=[self.case.pk])).status_code, 200)
            self.assertEqual(a.get(_reverse("agents")).status_code, 200)

    def test_non_managers_cannot_use_manager_workers(self):
        for user in (self.adv, self.rep):
            c = self.login(user)
            c.post(_reverse("review_run"))
            c.post(_reverse("business_run"), {"days": "90"})
            c.get(_reverse("business"))
        self.assertFalse(AgentRun.objects.filter(agent__in=["reviewer", "business"]).exists())

    def test_sales_and_strangers_cannot_open_ops_for_a_case(self):
        self.assertEqual(self.login(self.rep).get(_reverse("case_ops", args=[self.case.pk])).status_code, 404)
        self.assertEqual(self.login(self.other).get(_reverse("case_ops", args=[self.case.pk])).status_code, 404)
        self.assertEqual(self.login(self.other).post(_reverse("case_ops", args=[self.case.pk])).status_code, 404)
        self.assertFalse(AgentRun.objects.filter(agent="ops").exists())
        self.assertEqual(self.login(self.rep).get(_reverse("agents")).status_code, 302)



# ---------------------------------------------------------------- v9: document requirements, clerk notes, Revital
from . import doc_rules


def add_doc(case, category, name="x.pdf", size=50_000):
    return Document.objects.create(case=case, category=category, file=ContentFile(b"%PDF-1.4 " + b"x" * 20, name=name),
                                   original_name=name, size=size)


class DocRulesTests(TestCase):
    def setUp(self):
        self.adv = make_user("adv", "advisor")
        self.lawyer = make_user("law", "advisor", "legal")
        self.stranger = make_user("adv2", "advisor")
        lead = Lead.objects.create(national_id="123456782", first_name="דנה", last_name="כהן", phone="0501234567", workspace="finance")
        self.case = Case.objects.create(lead=lead, case_type="refinance", owner=self.adv, workspace="finance")
        lead2 = Lead.objects.create(national_id="123456782", first_name="יוסי", phone="0507654321", workspace="legal")
        self.legal = Case.objects.create(lead=lead2, case_type="enforcement", owner=self.lawyer, workspace="legal")

    def login(self, user):
        c = Client()
        c.force_login(user)
        s = c.session
        s["otp_ok"] = True
        s.save()
        return c

    def labels(self, case):
        return {r["key"]: r for r in agents.checklist(case)}

    def test_defaults_follow_the_requested_documents(self):
        rows = self.labels(self.case)  # מחזור משכנתא
        for k in ("id_front", "id_back", "bank_cert", "ni_employers", "income", "credit_or_amort"):
            self.assertEqual(rows[k]["state"], "required", k)
        self.assertEqual(rows["tax_exempt"]["state"], "optional")  # "אם יש"
        self.assertNotIn("credit_or_balances", rows)
        loans = Case(case_type="consolidation", workspace="finance")
        self.assertEqual(doc_rules.states(loans)["credit_or_balances"], "required")  # הלוואות: דוח יתרות / נתוני אשראי

    def test_alternatives_and_legacy_id(self):
        add_doc(self.case, "assessment")      # שומה במקום תלושים
        add_doc(self.case, "amort")           # דוח סילוקין במקום נתוני אשראי
        add_doc(self.case, "id")              # תעודה בקובץ אחד מכסה שני צדדים
        rows = self.labels(self.case)
        for k in ("income", "credit_or_amort", "id_front", "id_back"):
            self.assertTrue(rows[k]["ok"], k)
        self.assertFalse(rows["bank_cert"]["ok"])

    def test_optional_is_never_missing_and_off_is_hidden(self):
        missing = {r["key"] for r in doc_rules.missing(agents.checklist(self.case))}
        self.assertNotIn("tax_exempt", missing)
        doc_rules.save_states(self.case, {"ni_employers": "off", "property": "optional"})
        self.case.save()
        case = Case.objects.get(pk=self.case.pk)
        rows = self.labels(case)
        self.assertNotIn("ni_employers", rows)
        self.assertEqual(rows["property"]["state"], "optional")
        self.assertEqual(set(case.doc_settings["states"]), {"ni_employers", "property"})  # only overrides are stored

    def test_finance_missing_is_acted_on_immediately(self):
        f = agents.case_facts(self.case)
        self.assertFalse(f["awaiting_confirmation"])
        self.assertTrue(f["missing"])

    def test_legal_missing_waits_for_confirmation(self):
        with _mock.patch("leads.agents.ai.enabled", return_value=False):
            run = agents.run_ops(self.legal, self.lawyer)
        self.assertTrue(run.facts["awaiting_confirmation"])
        self.assertEqual(run.facts["missing"], [])
        self.assertTrue(run.facts["proposed_missing"])
        self.assertEqual(run.output["message"], "")          # nothing is drafted for the client yet
        self.assertTrue(any("אשר מה באמת חסר" in s for s in run.output["next_steps"]))
        c = self.login(self.lawyer)
        c.post(_reverse("case_detail", args=[self.legal.pk]), {"action": "confirm_missing"})
        self.legal.refresh_from_db()
        with _mock.patch("leads.agents.ai.enabled", return_value=False):
            run = agents.run_ops(self.legal, self.lawyer)
        self.assertFalse(run.facts["awaiting_confirmation"])
        self.assertEqual(run.facts["missing"], run.facts["proposed_missing"])
        for label in run.facts["missing"]:
            self.assertIn(label, run.output["message"])
        # marking one as not needed makes the old approval stale: the person must confirm again
        c.post(_reverse("case_detail", args=[self.legal.pk]), {"action": "doc_state", "key": "debt_calc", "state": "off"})
        self.legal.refresh_from_db()
        self.assertTrue(agents.case_facts(self.legal)["awaiting_confirmation"])

    def test_clerk_notes_go_over_every_document(self):
        add_doc(self.case, "other", "a.pdf")
        add_doc(self.case, "payslip", "p.pdf")
        add_doc(self.case, "payslip", "p.pdf")            # same name and size -> duplicate
        add_doc(self.case, "bank_cert", "tiny.pdf", size=900)
        add_doc(self.case, "id", "id.jpg")
        add_doc(self.case, "pension", "pen.pdf")          # not needed in a refinance
        text = " | ".join(n["text"] for n in agents.doc_notes(self.case))
        for needle in ("סווג כ״אחר״", "כפילות", "קטן מאוד", "שני הצדדים", "לא נדרש בתיק", "חסר:"):
            self.assertIn(needle, text)

    def test_clerk_notes_never_reach_the_ai(self):
        self.case.ai_consent = True
        self.case.save()
        add_doc(self.case, "other", "secret-name.pdf")
        seen = {}

        def fake(system, payload, max_tokens=0):
            seen["p"] = _json.dumps(payload, ensure_ascii=False)
            return {"summary_he": "x", "attention_he": [], "client_message_he": ""}

        with _mock.patch("leads.agents.ai.enabled", return_value=True), _mock.patch("leads.agents.ai.run_json", side_effect=fake):
            agents.run_ops(self.case, self.adv)
        self.assertNotIn("secret-name", seen["p"])

    def test_only_the_handler_can_change_requirements(self):
        url = _reverse("case_detail", args=[self.case.pk])
        self.assertEqual(self.login(self.stranger).post(url, {"action": "doc_state", "key": "bank_cert", "state": "off"}).status_code, 404)
        self.assertEqual(self.login(self.adv).get(url).status_code, 200)
        self.login(self.adv).post(url, {"action": "doc_state", "key": "bank_cert", "state": "off"})
        self.case.refresh_from_db()
        self.assertNotIn("bank_cert", self.labels(self.case))
        self.login(self.adv).post(url, {"action": "doc_state", "key": "nonsense", "state": "off"})  # ignored, no crash
        self.login(self.adv).post(url, {"action": "doc_state", "key": "income", "state": "weird"})
        self.assertEqual(self.labels(self.case)["income"]["state"], "required")

    def test_case_types_follow_the_workspace(self):
        fin = {v for v, _ in Case.types_for("finance")}
        leg = {v for v, _ in Case.types_for("legal")}
        self.assertNotIn("enforcement", fin)
        self.assertEqual(leg, {"enforcement", "other"})
        c = self.login(self.adv)
        lead = Lead.objects.get(pk=self.case.lead_id)
        lead.assigned_to = self.adv
        lead.save()
        r = c.post(_reverse("lead_detail", args=[lead.pk]), {"action": "open_case", "case_type": "enforcement"})
        self.assertEqual(r.status_code, 400)

    def test_detail_and_ops_pages_render_for_both_workspaces(self):
        add_doc(self.legal, "debt_title", "pasak.pdf")
        for user, case in ((self.adv, self.case), (self.lawyer, self.legal)):
            c = self.login(user)
            self.assertEqual(c.get(_reverse("case_detail", args=[case.pk])).status_code, 200)
            with _mock.patch("leads.agents.ai.enabled", return_value=False), _mock.patch("leads.agent_views.ai.enabled", return_value=False):
                cache.clear()
                self.assertEqual(c.post(_reverse("case_ops", args=[case.pk])).status_code, 302)
                self.assertEqual(c.get(_reverse("case_ops", args=[case.pk])).status_code, 200)


class RevitalAccountTests(TestCase):
    def test_account_exists_in_legal_workspace_without_a_usable_password(self):
        u = User.objects.get(username="revital")
        self.assertFalse(u.has_usable_password())
        self.assertFalse(u.is_staff or u.is_superuser)
        self.assertEqual((u.profile.role, u.profile.workspace), ("advisor", "legal"))
        self.assertFalse(Client().login(username="revital", password=""))


# =====================================================================================
# v10: statuses board, website intake, mail, campaigner
# =====================================================================================
import json as _json
from email.message import EmailMessage as _Eml
from unittest import mock

from . import agents, mailbox, statuses as _st
from .models import AgentRun, EmailMessage, Note, Task


def _eml(frm="dana@example.com", to="office@as-finances.com", subject="מסמכים", body="שלום", att=None, mid="<a1@x>"):
    m = _Eml()
    m["From"], m["To"], m["Subject"], m["Message-ID"], m["Date"] = frm, to, subject, mid, "Mon, 09 Nov 2026 10:00:00 +0200"
    m.set_content(body)
    for name, data in (att or []):
        m.add_attachment(data, maintype="application", subtype="octet-stream", filename=name)
    return m.as_bytes()


PDF = b"%PDF-1.4 test"


class StatusBoardTests(TestCase):
    def setUp(self):
        self.mgr = make_user("mgr")
        self.adv = make_user("adv", role="advisor")
        self.lead = Lead.objects.create(national_id="123456782", first_name="דנה", workspace="finance", assigned_to=self.adv)
        self.c = Client()
        self.c.force_login(self.mgr)

    def test_manager_adds_custom_status_and_lead_can_use_it(self):
        self.c.post("/statuses/add/", {"name": "ממתין למסמכים", "color": "#0D9488"})
        d = _st.defs("finance")
        custom = [x for x in d if not x.builtin]
        self.assertEqual(len(custom), 1)
        self.c.post(f"/leads/{self.lead.pk}/quick/", {"status": custom[0].key})
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, custom[0].key)
        self.assertIn("ממתין למסמכים", Note.objects.filter(lead=self.lead).first().text)
        self.assertEqual(self.c.get("/leads/").status_code, 200)

    def test_advisor_cannot_add_status_and_unknown_status_rejected(self):
        a = Client(); a.force_login(self.adv)
        a.post("/statuses/add/", {"name": "x", "color": "#000000"})
        self.assertFalse(any(not x.builtin for x in _st.defs("finance")))
        r = self.c.post(f"/leads/{self.lead.pk}/quick/", {"status": "nope"})
        self.assertEqual(r.status_code, 400)

    def test_delete_custom_status_returns_leads_to_new(self):
        d, _ = _st.create("finance", "בדיקה", "#0D9488")
        Lead.objects.filter(pk=self.lead.pk).update(status=d.key)
        self.c.post(f"/statuses/{d.pk}/delete/")
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, "new")

    def test_builtin_status_cannot_be_deleted(self):
        d = _st.defs("finance")[0]
        self.c.post(f"/statuses/{d.pk}/delete/")
        self.assertEqual(len(_st.defs("finance")), 8)

    def test_drawer_respects_assignment(self):
        sales = make_user("s1", role="sales")
        s = Client(); s.force_login(sales)
        self.assertEqual(s.get(f"/leads/{self.lead.pk}/drawer/").status_code, 404)
        self.assertEqual(self.c.get(f"/leads/{self.lead.pk}/drawer/").status_code, 200)

    def test_board_and_home_render(self):
        for i in range(3):
            Lead.objects.create(national_id="", first_name=f"ליד{i}", phone=f"05200000{i:02d}", workspace="finance", intake_at=timezone_now())
        self.assertEqual(self.c.get("/leads/").status_code, 200)
        self.assertEqual(self.c.get("/leads/?status=new&web=1").status_code, 200)
        self.assertEqual(self.c.get("/").status_code, 200)


def timezone_now():
    from django.utils import timezone
    return timezone.now()


@override_settings(LEAD_INTAKE_TOKEN="secret-token-123")
class IntakeTests(TestCase):
    def setUp(self):
        cache.clear()
        self.mgr = make_user("mgr")
        self.mother = make_user("mom", role="advisor", workspace="legal")
        self.c = Client()

    def post(self, data, token="secret-token-123"):
        return self.c.post("/api/intake/", _json.dumps(data), content_type="application/json", HTTP_X_INTAKE_TOKEN=token)

    def good(self, **kw):
        d = {"name": "יוסי כהן", "phone": "052-1234567", "email": "Yossi@Gmail.com", "message": "רוצה לאחד הלוואות ולהקטין ריבית",
             "consent": True, "utm_source": "google", "utm_medium": "cpc", "utm_campaign": "איחוד-נובמבר", "page": "/consolidation"}
        d.update(kw)
        return d

    @override_settings(LEAD_INTAKE_TOKEN="")
    def test_switched_off_without_token(self):
        self.assertEqual(self.post(self.good()).status_code, 404)

    def test_wrong_token_and_missing_consent(self):
        self.assertEqual(self.post(self.good(), token="bad").status_code, 401)
        self.assertEqual(self.post(self.good(consent=False)).status_code, 400)
        self.assertEqual(Lead.objects.count(), 0)

    def test_creates_scored_lead_with_task_and_source(self):
        r = self.post(self.good())
        self.assertEqual(r.status_code, 200)
        l = Lead.objects.get()
        self.assertEqual((l.workspace, l.source, l.campaign, l.interest), ("finance", "Google Ads", "איחוד-נובמבר", "consolidation"))
        self.assertEqual(l.email, "yossi@gmail.com")
        self.assertEqual(l.phone, "0521234567")
        self.assertGreaterEqual(l.lead_score, agents.HOT)
        self.assertEqual(l.form_message, "רוצה לאחד הלוואות ולהקטין ריבית")
        self.assertTrue(Task.objects.filter(lead=l, owner=self.mgr).exists())
        self.assertIsNotNone(l.web_consent_at)

    def test_message_is_encrypted_at_rest(self):
        self.post(self.good())
        with connection.cursor() as c:
            c.execute("select form_message from leads_lead")
            self.assertTrue(c.fetchone()[0].startswith("enc1:"))

    def test_two_leads_without_id_do_not_collide(self):
        self.post(self.good())
        self.post(self.good(phone="0549999999", email="b@b.com", name="דנה לוי"))
        self.assertEqual(Lead.objects.count(), 2)

    def test_repeat_enquiry_adds_note_not_duplicate(self):
        self.post(self.good())
        self.post(self.good(message="שוב אני"))
        self.assertEqual(Lead.objects.count(), 1)
        self.assertTrue(Note.objects.filter(text__startswith="פנייה חוזרת").exists())

    def test_honeypot_stores_nothing(self):
        self.assertEqual(self.post(self.good(website="http://spam")).status_code, 200)
        self.assertEqual(Lead.objects.count(), 0)

    def test_enforcement_goes_to_legal_workspace(self):
        self.post(self.good(message="יש לי חייב ופסק דין, צריך הוצאה לפועל", utm_campaign="", utm_medium=""))
        l = Lead.objects.get()
        self.assertEqual((l.workspace, l.assigned_to.username), ("legal", "revital"))  # the migration-created legal user is the first legal assignee

    def test_rate_limit(self):
        codes = [self.post(self.good(phone=f"05200000{i:02d}")).status_code for i in range(70)]
        self.assertIn(429, codes)

    def test_get_not_allowed(self):
        self.assertEqual(self.c.get("/api/intake/").status_code, 405)


class MailTests(TestCase):
    def setUp(self):
        self.mgr = make_user("mgr")
        self.mother = make_user("mom", role="advisor", workspace="legal")
        self.lead = Lead.objects.create(national_id="123456782", first_name="דנה", last_name="לוי", email="dana@example.com", workspace="finance")
        self.case = Case.objects.create(lead=self.lead, case_type="consolidation", owner=self.mgr, workspace="finance")
        self.c = Client(); self.c.force_login(self.mgr)

    def test_matches_contact_case_and_suggests_category(self):
        m, created = mailbox.ingest_bytes(_eml(att=[("תלוש שכר אוקטובר.pdf", PDF)]))
        self.assertTrue(created)
        self.assertEqual((m.lead, m.case, m.category), (self.lead, self.case, "documents"))
        self.assertEqual(m.attachments.get().suggested_category, "payslip")

    def test_missing_document_hint(self):
        m, _ = mailbox.ingest_bytes(_eml(att=[("אישור ניהול חשבון.pdf", PDF)]))
        hint = [h for h in m.hints if h["type"] == "fills_missing"]
        self.assertTrue(hint and "אישור ניהול חשבון" in hint[0]["labels"])

    def test_file_name_wins_over_subject_and_short_words_need_boundaries(self):
        m, _ = mailbox.ingest_bytes(_eml(subject="תלושי שכר ואישור ניהול חשבון", mid="<s@x>",
                                         att=[("תלוש.pdf", PDF), ("אישור ניהול חשבון.pdf", PDF), ("תזכורת.pdf", PDF)]))
        cats = {a.original_name: a.suggested_category for a in m.attachments.all()}
        self.assertEqual(cats["תלוש.pdf"], "payslip")
        self.assertEqual(cats["אישור ניהול חשבון.pdf"], "bank_cert")
        self.assertEqual(cats["תזכורת.pdf"], "payslip")  # no filename match: falls back to the subject
        self.assertEqual(mailbox.category_for_name("תזכורת.pdf"), "other")
        self.assertEqual(mailbox.category_for_name("תז קדימה.jpg"), "id_front")
        self.assertEqual(mailbox.category_for_name("תז.jpg"), "id")

    def test_duplicate_message_id_ignored(self):
        mailbox.ingest_bytes(_eml())
        _, created = mailbox.ingest_bytes(_eml())
        self.assertFalse(created)
        self.assertEqual(EmailMessage.objects.count(), 1)

    def test_receipt_and_unknown_sender(self):
        m, _ = mailbox.ingest_bytes(_eml(frm="billing@vendor.com", subject="קבלה על תשלום", mid="<r@x>", att=[("receipt.pdf", PDF)]))
        self.assertEqual((m.category, m.lead), ("receipt", None))

    def test_fake_pdf_attachment_is_skipped(self):
        m, _ = mailbox.ingest_bytes(_eml(att=[("x.pdf", b"MZ not a pdf")], mid="<f@x>"))
        self.assertEqual(m.attachments.count(), 0)
        self.assertTrue(any(h["type"] == "skipped_attachments" for h in m.hints))

    def test_subject_body_and_attachments_encrypted_on_disk(self):
        m, _ = mailbox.ingest_bytes(_eml(subject="סודי מאוד", body="גוף סודי", att=[("a.pdf", PDF + b" secret")], mid="<e@x>"))
        with connection.cursor() as c:
            c.execute("select subject, body from leads_emailmessage where id=%s", [m.pk])
            subj, body = c.fetchone()
        self.assertTrue(subj.startswith("enc1:") and body.startswith("enc1:"))
        self.assertNotIn(b"secret", open(m.attachments.get().file.path, "rb").read())

    def test_file_into_case_creates_document(self):
        m, _ = mailbox.ingest_bytes(_eml(att=[("תלוש.pdf", PDF)]))
        att = m.attachments.get()
        self.c.post(f"/mail/{m.pk}/do/", {"action": "file", "case": self.case.pk, f"use_{att.pk}": "on", f"cat_{att.pk}": "payslip"})
        d = Document.objects.get(case=self.case)
        self.assertEqual((d.category, d.original_name), ("payslip", "תלוש.pdf"))
        m.refresh_from_db()
        self.assertEqual(m.status, "filed")
        with d.file.open("rb") as fh:
            self.assertEqual(fh.read(), PDF)

    def test_mother_sees_only_routed_mail_and_manager_keeps_it(self):
        m, _ = mailbox.ingest_bytes(_eml(att=[("a.pdf", PDF)]))
        mom = Client(); mom.force_login(self.mother)
        self.assertEqual(mom.get(f"/mail/{m.pk}/").status_code, 404)
        self.c.post(f"/mail/{m.pk}/do/", {"action": "route", "user": self.mother.pk})
        self.assertEqual(mom.get(f"/mail/{m.pk}/").status_code, 200)
        self.assertEqual(self.c.get(f"/mail/{m.pk}/").status_code, 200)

    def test_sales_has_no_mail(self):
        s = make_user("s2", role="sales")
        sc = Client(); sc.force_login(s)
        self.assertEqual(sc.get("/mail/").status_code, 302)

    def test_upload_eml_and_delete(self):
        up = SimpleUploadedFile("m.eml", _eml(mid="<u@x>"), content_type="message/rfc822")
        self.c.post("/mail/upload/", {"eml": up})
        m = EmailMessage.objects.get()
        self.assertEqual(self.c.get("/mail/").status_code, 200)
        self.c.post(f"/mail/{m.pk}/do/", {"action": "delete"})
        self.assertEqual(EmailMessage.objects.count(), 0)

    def test_clerk_run_is_code_only_and_proposes(self):
        mailbox.ingest_bytes(_eml(att=[("אישור ניהול חשבון.pdf", PDF)]))
        self.c.post("/mail/clerk/")
        run = AgentRun.objects.get(agent="mail")
        self.assertEqual(run.facts["new_total"], 1)
        self.assertEqual(run.output["items"][0]["action"], "file")
        self.assertEqual(Document.objects.count(), 0)  # proposes only
        self.assertEqual(self.c.get("/mail/").status_code, 200)

    def test_ai_gets_counts_only(self):
        mailbox.ingest_bytes(_eml(subject="שם סודי", att=[("a.pdf", PDF)]))
        seen = {}
        def fake(system, payload, max_tokens=0):
            seen["p"] = _json.dumps(payload, ensure_ascii=False)
            return {"summary_he": "יש 1 מיילים חדשים."}
        with mock.patch.object(agents.ai, "enabled", return_value=True), mock.patch.object(agents.ai, "run_json", fake):
            agents.run_mail("finance", self.mgr, EmailMessage.objects.all())
        for secret in ("שם סודי", "dana@example.com", "דנה", "a.pdf"):
            self.assertNotIn(secret, seen["p"])


class CampaignerTests(TestCase):
    def setUp(self):
        self.mgr = make_user("mgr")
        self.c = Client(); self.c.force_login(self.mgr)
        now = timezone_now()
        Lead.objects.create(national_id="", first_name="חם", last_name="מאוד", phone="0521111111", email="h@h.com", workspace="finance",
                            intake_at=now, form_message="רוצה משכנתא ומחזור לדירה שלי", utm_medium="cpc", campaign="משכנתא-נוב", source="Google Ads")
        Lead.objects.create(national_id="", first_name="קר", phone="0522222222", workspace="finance", intake_at=now, campaign="משכנתא-נוב")

    def test_scores_tiers_and_interest(self):
        s, _ = agents.score_lead("0521111111", "a@a.com", "חם מאוד", "רוצה משכנתא ומחזור לדירה", "mortgage", True)
        self.assertGreaterEqual(s, agents.HOT)
        self.assertEqual(agents.detect_interest("מחזור משכנתא"), "mortgage")
        self.assertEqual(agents.tier(10), "cold")

    def test_run_and_apply(self):
        self.c.post("/campaigner/run/", {"days": 30})
        run = AgentRun.objects.get(agent="campaign")
        self.assertEqual(run.facts["total"], 2)
        self.assertTrue(run.facts["warnings"])  # small sample is called out
        self.c.post("/campaigner/apply/", {"tasks": "on"})
        hot = Lead.objects.get(first_name="חם")
        self.assertGreaterEqual(hot.lead_score, agents.HOT)
        self.assertTrue(Task.objects.filter(lead=hot, title__startswith="להתקשר לליד חם").exists())
        self.assertEqual(Lead.objects.get(first_name="קר").status, "new")  # status never changed by the worker
        self.assertEqual(self.c.get("/campaigner/").status_code, 200)

    def test_ai_gets_no_personal_data(self):
        seen = {}
        def fake(system, payload, max_tokens=0):
            seen["p"] = _json.dumps(payload, ensure_ascii=False)
            return {"summary_he": "נקלטו 2 לידים.", "tips_he": []}
        with mock.patch.object(agents.ai, "enabled", return_value=True), mock.patch.object(agents.ai, "run_json", fake):
            agents.run_campaigner("finance", self.mgr, Lead.objects.all())
        for secret in ("0521111111", "h@h.com", "מאוד", "רוצה משכנתא", "חם"):
            self.assertNotIn(secret, seen["p"])

    def test_first_message_link_uses_name_without_digits_in_template(self):
        lead = Lead.objects.get(first_name="חם")
        r = self.c.get(f"/campaigner/message/{lead.pk}/")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].startswith("https://wa.me/972521111111"))

    def test_sales_blocked(self):
        s = make_user("s3", role="sales")
        sc = Client(); sc.force_login(s)
        self.assertEqual(sc.get("/campaigner/").status_code, 302)
        self.assertEqual(sc.post("/campaigner/run/").status_code, 302)


class PortedFromV4Tests(TestCase):
    """Features taken from the FastAPI/Next.js line: status kinds, legal case fields, quick note."""
    def setUp(self):
        self.mgr = make_user("mgr")
        self.mom = make_user("mom2", role="advisor", workspace="legal")
        self.c = Client(); self.c.force_login(self.mgr)

    def test_custom_status_kind_removes_lead_from_call_queue(self):
        lead = Lead.objects.create(national_id="123456782", first_name="א", phone="0521234567", workspace="finance", assigned_to=self.mgr)
        d, _ = _st.create("finance", "אושר בבנק", "#0D9488", "won")
        self.c.post(f"/leads/{lead.pk}/quick/", {"status": d.key})
        self.assertNotIn("א", "".join(str(x) for x in self.c.get("/queue/").context["sections"][2][2]))
        self.assertEqual(_st.closed_keys("finance") & {d.key}, {d.key})
        d2, _ = _st.create("finance", "בבדיקה", "#0D9488")
        self.assertEqual(d2.kind, "normal")
        self.assertEqual(_st.create("finance", "x", "#0D9488", "bogus")[0].kind, "normal")

    def test_legal_case_fields_saved_only_in_legal_workspace(self):
        mom = Client(); mom.force_login(self.mom)
        r = mom.post("/cases/new/", {"first_name": "דוד", "national_id": "123456782", "phone": "0521234567", "case_type": "enforcement",
                                     "debtor_name": "חייב בע״מ", "creditor_name": "זוכה", "procedure_type": "execution",
                                     "principal_amount": "50000", "registrar_judge": "השופט כהן", "file_number": "12-345678-9"})
        case = Case.objects.get()
        self.assertEqual((case.debtor_name, case.creditor_name, case.procedure_type, case.registrar_judge), ("חייב בע״מ", "זוכה", "execution", "השופט כהן"))
        self.assertEqual(case.principal_amount, 50000)
        self.assertEqual(mom.get(f"/cases/{case.pk}/").status_code, 200)
        self.c.post("/cases/new/", {"first_name": "מירב", "national_id": "000000018", "phone": "0521234568", "case_type": "pension",
                                    "debtor_name": "לא אמור להישמר"})
        self.assertEqual(Case.objects.get(lead__first_name="מירב").debtor_name, "")

    def test_quick_note_from_board_row(self):
        lead = Lead.objects.create(national_id="123456782", first_name="ב", workspace="finance", assigned_to=self.mgr)
        self.c.post(f"/leads/{lead.pk}/note/", {"text": "ביקש שנחזור מחר", "next": "/leads/"})
        self.assertTrue(Note.objects.filter(lead=lead, text="ביקש שנחזור מחר").exists())
        self.assertContains(self.c.get("/leads/"), "ביקש שנחזור מחר")

    def test_home_shows_received_and_expected(self):
        lead = Lead.objects.create(national_id="123456782", first_name="ג", workspace="finance", assigned_to=self.mgr)
        Case.objects.create(lead=lead, case_type="pension", owner=self.mgr, workspace="finance", fee=3000, fee_paid=False, stage="approved")
        r = self.c.get("/")
        self.assertEqual(r.context["expected"], 3000)
        self.assertContains(r, "צפוי להתקבל: ₪3,000")


class V11Tests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        U = get_user_model()
        self.a = U.objects.create_user("aa", password="x"); self.b = U.objects.create_user("bb", password="x")
        self.a.profile.workspace = self.b.profile.workspace = "finance"; self.a.profile.role = "manager"; self.b.profile.role = "advisor"
        self.a.profile.save(); self.b.profile.save()
        self.lead = Lead.objects.create(workspace="finance", first_name="דנה", last_name="כהן", phone="0501234567", assigned_to=self.a, id_valid=True)

    def test_lead_share_gives_access_and_unshare_removes(self):
        from .perms import leads_for
        self.assertFalse(leads_for(self.b).filter(pk=self.lead.pk).exists())
        self.client.force_login(self.a)
        r = self.client.post(f"/leads/{self.lead.pk}/", {"action": "lead_share", "partner": self.b.pk})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(leads_for(self.b).filter(pk=self.lead.pk).exists())
        sh = self.lead.shares.first()
        self.client.post(f"/leads/{self.lead.pk}/", {"action": "lead_unshare", "share": sh.pk})
        self.assertFalse(leads_for(self.b).filter(pk=self.lead.pk).exists())

    def test_non_owner_cannot_share(self):
        self.client.force_login(self.b)
        r = self.client.post(f"/leads/{self.lead.pk}/", {"action": "lead_share", "partner": self.a.pk})
        self.assertEqual(r.status_code, 404)

    def _case_with_docs(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        case = Case.objects.create(lead=self.lead, workspace="finance", case_type="mortgage", owner=self.a)
        docs = []
        for i in range(3):
            docs.append(Document.objects.create(case=case, category="other", custom_name=f"מסמך {i}", original_name=f"f{i}.pdf", size=3,
                                                file=SimpleUploadedFile(f"f{i}.pdf", b"%PDF-1.4 x"), uploaded_by=self.a))
        return case, docs

    def test_bulk_download_zip_and_delete(self):
        import io, zipfile
        case, docs = self._case_with_docs()
        self.client.force_login(self.a)
        r = self.client.post(f"/cases/{case.pk}/documents/bulk/", {"op": "download", "doc": [d.pk for d in docs[:2]]})
        self.assertEqual(r["Content-Type"], "application/zip")
        self.assertEqual(len(zipfile.ZipFile(io.BytesIO(r.content)).namelist()), 2)
        self.client.post(f"/cases/{case.pk}/documents/bulk/", {"op": "delete", "doc": [d.pk for d in docs[:2]]})
        self.assertEqual(case.documents.count(), 1)

    def test_bulk_requires_access(self):
        case, docs = self._case_with_docs()
        self.client.force_login(self.b)  # advisor, not owner
        r = self.client.post(f"/cases/{case.pk}/documents/bulk/", {"op": "delete", "doc": [docs[0].pk]})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(case.documents.count(), 3)

    def test_custom_name_only_for_other(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        case, _ = self._case_with_docs()
        self.client.force_login(self.a)
        self.client.post(f"/cases/{case.pk}/", {"action": "upload", "category": "other", "custom_name": "חוזה <b>שכירות</b>",
                        "file": SimpleUploadedFile("x.pdf", b"%PDF-1.4 hello")})
        d = case.documents.order_by("-id").first()
        self.assertEqual(d.display_name, "חוזה <b>שכירות</b>")  # stored as text; templates escape it
        page = self.client.get(f"/cases/{case.pk}/").content.decode()
        self.assertNotIn("<b>שכירות</b>", page)

    def test_intake_site_form_fields(self):
        from django.test import override_settings
        with override_settings(LEAD_INTAKE_TOKEN="t"):
            r = self.client.post("/api/intake/", data=_json.dumps({"name": "רוני לוי", "phone": "0527654321", "consent": "true", "call_time": "evening",
                                 "owns_property": "joint", "enforcement_file": "yes"}), content_type="application/json", headers={"X-Intake-Token": "t"})
        self.assertEqual(r.status_code, 200)
        l = Lead.objects.get(phone="0527654321")
        self.assertEqual((l.call_time, l.owns_property, l.enforcement_file), ("evening", "joint", "yes"))


class AdsReportTests(TestCase):
    CSV = ("דוח קמפיינים\n1 בנובמבר 2026 - 15 בנובמבר 2026\n"
           "קמפיין,עלות,קליקים,חשיפות,המרות\n"
           "Refi Search,\"₪1,200.50\",150,\"4,000\",12\n"
           "Pension_Search,300,10,900,0\n"
           "סה\"כ: חשבון,\"1,500.50\",160,\"4,900\",12\n").encode("utf-8")

    def setUp(self):
        from django.contrib.auth import get_user_model
        self.u = get_user_model().objects.create_user("mgr", password="x")
        self.u.profile.role, self.u.profile.workspace = "manager", "finance"
        self.u.profile.save()
        self.client.force_login(self.u)

    def test_parse_number(self):
        from .adsreport import parse_number as n
        self.assertEqual(n("₪1,234.50"), 1234.5)
        self.assertEqual(n("1.234,50"), 1234.5)
        self.assertEqual(n("4,000"), 4000)
        self.assertEqual(n("--"), 0)
        self.assertIsNone(n("12abc.3.4"))

    def test_csv_preamble_totals_hebrew(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from .adsreport import rows_from_upload
        rows, warns = rows_from_upload(SimpleUploadedFile("r.csv", self.CSV))
        self.assertEqual([r["campaign"] for r in rows], ["Refi Search", "Pension_Search"])
        self.assertEqual((rows[0]["cost"], rows[0]["impressions"]), (1200.5, 4000))

    def _confirmed(self):
        from datetime import date
        from django.utils import timezone
        r = AdReport.objects.create(workspace="finance", period_start=date(2026, 11, 1), period_end=date(2026, 11, 30), status="confirmed",
                                    rows=[{"campaign": "Refi Search", "cost": 1000, "clicks": 100, "impressions": 5000, "conversions": 10},
                                          {"campaign": "Pension Search", "cost": 500, "clicks": 50, "impressions": 1000, "conversions": 0},
                                          {"campaign": "Ghost", "cost": 400, "clicks": 40, "impressions": 800, "conversions": 0}])
        now = timezone.now().replace(day=10)
        for i in range(4):
            l = Lead.objects.create(workspace="finance", first_name=f"a{i}", phone=f"050000000{i}", campaign="refi_search", intake_at=now,
                                    lead_score=80 if i < 2 else 30, id_valid=True)
        c = Case.objects.create(lead=l, workspace="finance", case_type="mortgage", owner=self.u, stage="closed", fee=3000)
        Lead.objects.create(workspace="finance", first_name="z", phone="0500000099", campaign="Other Camp", intake_at=now, id_valid=True)
        return r

    def test_analysis_numbers_and_flags(self):
        from .adsreport import analyze
        from .perms import own_leads
        f = analyze(self._confirmed(), own_leads(self.u))
        refi = next(c for c in f["campaigns"] if c["campaign"] == "Refi Search")
        self.assertEqual((refi["leads"], refi["hot"], refi["closed"], refi["revenue"]), (4, 2, 1, 3000))
        self.assertEqual((refi["cost_per_lead"], refi["cost_per_hot"], refi["cost_per_closed"], refi["return_x"]), (250, 500, 1000, 3.0))
        self.assertEqual(refi["ctr_pct"], 2.0)
        kinds = {(x["kind"], x["campaign"]) for x in f["flags"]}
        self.assertIn(("spend_no_leads", "Pension Search"), kinds)
        self.assertIn(("spend_no_leads", "Ghost"), kinds)
        self.assertIn(("crm_only", "Other Camp"), kinds)
        self.assertIn(("conv_mismatch", "Refi Search"), kinds)
        self.assertEqual(f["totals"]["cost"], 1900)

    def test_views_flow_and_validation(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        r = self.client.post("/campaigner/ads/upload/", {"start": "2026-11-01", "end": "2026-11-15", "table": SimpleUploadedFile("r.csv", self.CSV)})
        rep = AdReport.objects.get()
        self.assertEqual(r.status_code, 302)
        self.assertEqual(rep.status, "draft")
        page = self.client.get(f"/campaigner/ads/{rep.pk}/").content.decode()
        self.assertIn("טרם אושרו", page)
        bad = self.client.post(f"/campaigner/ads/{rep.pk}/", {"action": "save", "campaign": ["X"], "cost": ["abc"], "clicks": ["1"], "impressions": ["1"], "conversions": ["1"]})
        rep.refresh_from_db(); self.assertEqual(rep.status, "draft")
        ok = self.client.post(f"/campaigner/ads/{rep.pk}/", {"action": "save", "campaign": ["X"], "cost": ["10.5"], "clicks": ["1"], "impressions": ["10"], "conversions": ["0"]})
        rep.refresh_from_db(); self.assertEqual((rep.status, rep.rows[0]["cost"]), ("confirmed", 10.5))
        self.client.post(f"/campaigner/ads/{rep.pk}/", {"action": "narrate"})
        rep.refresh_from_db(); self.assertIn("summary", rep.narrative)
        self.assertEqual(self.client.get(f"/campaigner/ads/{rep.pk}/").status_code, 200)

    def test_sales_and_other_workspace_blocked(self):
        from django.contrib.auth import get_user_model
        U = get_user_model()
        rep = AdReport.objects.create(workspace="finance", period_start="2026-11-01", period_end="2026-11-02")
        s = U.objects.create_user("sl", password="x"); s.profile.role = "sales"; s.profile.workspace = "finance"; s.profile.save()
        self.client.force_login(s)
        self.assertEqual(self.client.get(f"/campaigner/ads/{rep.pk}/").status_code, 302)
        m = U.objects.create_user("lg", password="x"); m.profile.role = "manager"; m.profile.workspace = "legal"; m.profile.save()
        self.client.force_login(m)
        self.assertEqual(self.client.get(f"/campaigner/ads/{rep.pk}/").status_code, 404)

    def test_screenshot_requires_ai_and_reads_draft(self):
        from unittest import mock
        from django.core.files.uploadedfile import SimpleUploadedFile
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
        r = self.client.post("/campaigner/ads/upload/", {"shots": SimpleUploadedFile("a.png", png)})
        self.assertEqual(AdReport.objects.count(), 0)  # no AI key: rejected with a message
        fake = {"rows": [{"campaign": "Refi", "cost": "₪300", "clicks": 20, "impressions": None, "conversions": 1}], "notes_he": ["מילת מפתח: מיחזור"], "warnings_he": []}
        with mock.patch("leads.ai.enabled", return_value=True), mock.patch("leads.ai._call", return_value=__import__("json").dumps(fake)), \
                mock.patch("leads.uploads.validate_upload"):
            self.client.post("/campaigner/ads/upload/", {"shots": SimpleUploadedFile("a.png", png)})
        rep = AdReport.objects.get()
        self.assertEqual((rep.source, rep.status, rep.rows[0]["cost"], rep.rows[0]["impressions"]), ("image", "draft", 300, 0))
        self.assertTrue(any("impressions" in w for w in rep.warnings))
