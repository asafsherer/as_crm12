from django.conf import settings
from django.contrib.auth.signals import user_logged_in, user_login_failed
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import AuditLog, UserProfile


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.get_or_create(
            user=instance,
            defaults={"role": UserProfile.Role.MANAGER if instance.is_superuser else UserProfile.Role.SALES},
        )


@receiver(user_logged_in)
def log_login(sender, request, user, **kwargs):
    AuditLog.objects.create(user=user, action="login", detail="כניסה למערכת")


@receiver(user_login_failed)
def log_failed(sender, credentials, request=None, **kwargs):
    AuditLog.objects.create(user=None, action="login_failed", detail=f"ניסיון כניסה כושל: {credentials.get('username', '')[:60]}")
