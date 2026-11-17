from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError

from leads.models import AuditLog, UserProfile


class Command(BaseCommand):
    help = ("Lost the login code? Turns off two-step login for a user and clears login/code lockouts. "
            "On the next sign-in the user sets up two-step login again (new QR). Password: use  manage.py changepassword <user>.")

    def add_arguments(self, parser):
        parser.add_argument("username")

    def handle(self, *args, **opts):
        User = get_user_model()
        name = opts["username"].strip()
        user = User.objects.filter(username__iexact=name).first()
        if not user:
            raise CommandError(f"אין משתמש בשם {name}")
        p = UserProfile.objects.get(user=user)
        p.totp_enabled, p.totp_secret = False, ""
        p.save(update_fields=["totp_enabled", "totp_secret"])
        for key in (f"login-fail:user:{user.username.lower()}", f"otp-fail:{user.pk}"):
            try:
                cache.delete(key)
            except Exception:  # cache table not created yet: nothing to clear
                pass
        AuditLog.objects.create(user=None, action="reset_access", detail=f"אופס אימות דו-שלבי למשתמש {user.username} (משורת הפקודה)")
        self.stdout.write(f"בוצע. {user.username} יכול להיכנס עם הסיסמה ולהגדיר אימות דו-שלבי מחדש.")
