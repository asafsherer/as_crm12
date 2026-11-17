from django.conf import settings
from django.db import migrations


def create_profiles(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    Profile = apps.get_model("leads", "UserProfile")
    for u in User.objects.all():
        Profile.objects.get_or_create(user=u, defaults={"role": "manager" if u.is_superuser else "sales"})


class Migration(migrations.Migration):
    dependencies = [("leads", "0003_v3_roles_sharing_tasks")]
    operations = [migrations.RunPython(create_profiles, migrations.RunPython.noop)]
