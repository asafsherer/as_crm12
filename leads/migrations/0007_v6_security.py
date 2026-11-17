from django.db import migrations, models

import leads.fields


def encrypt_existing(apps, schema_editor):
    from leads.crypto import nid_hash

    Lead = apps.get_model("leads", "Lead")
    for lead in Lead.objects.all().iterator():
        # reading tolerates old plaintext; update() re-writes it encrypted
        Lead.objects.filter(pk=lead.pk).update(national_id=lead.national_id, national_id_hash=nid_hash(lead.national_id))
    Profile = apps.get_model("leads", "UserProfile")
    for prof in Profile.objects.exclude(totp_secret="").iterator():
        Profile.objects.filter(pk=prof.pk).update(totp_secret=prof.totp_secret)


class Migration(migrations.Migration):
    dependencies = [("leads", "0006_v5_goals")]

    operations = [
        migrations.RemoveConstraint(model_name="lead", name="uniq_lead_workspace_nid"),
        migrations.AlterField(model_name="lead", name="national_id",
                              field=leads.fields.EncryptedCharField(verbose_name="ת״ז")),
        migrations.AddField(model_name="lead", name="national_id_hash",
                            field=models.CharField(blank=True, default="", editable=False, max_length=64)),
        migrations.AlterField(model_name="userprofile", name="totp_secret",
                              field=leads.fields.EncryptedCharField(blank=True)),
        migrations.RunPython(encrypt_existing, migrations.RunPython.noop),
        migrations.AddConstraint(model_name="lead",
                                 constraint=models.UniqueConstraint(fields=("workspace", "national_id_hash"),
                                                                    name="uniq_lead_workspace_nid_hash")),
    ]
