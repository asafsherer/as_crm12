from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.db import migrations

USERNAME = "revital"


def add_user(apps, schema_editor):
    """Creates the login for Revital (legal / enforcement workspace). No password is stored here:
    the account cannot sign in until a password is set (start.bat asks for it; online: admin screen)."""
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    Profile = apps.get_model("leads", "UserProfile")
    user, _ = User.objects.get_or_create(
        username=USERNAME,
        defaults={"first_name": "רויטל", "is_active": True, "is_staff": False, "is_superuser": False,
                  "password": make_password(None)},  # unusable password
    )
    Profile.objects.get_or_create(user=user, defaults={"role": "advisor", "workspace": "legal"})


def noop(apps, schema_editor):
    pass  # never delete a user automatically


class Migration(migrations.Migration):
    dependencies = [
        ("leads", "0009_v9_documents_checklist"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [migrations.RunPython(add_user, noop)]
