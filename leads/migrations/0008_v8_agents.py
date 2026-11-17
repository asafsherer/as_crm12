from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("leads", "0007_v6_security"),
    ]

    operations = [
        migrations.AddField(
            model_name="lead",
            name="source",
            field=models.CharField(blank=True, db_index=True, default="", max_length=60, verbose_name="מקור הליד"),
        ),
        migrations.CreateModel(
            name="MarketingCost",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("workspace", models.CharField(default="finance", max_length=10)),
                ("month", models.DateField(help_text="היום הראשון של החודש")),
                ("source", models.CharField(max_length=60, verbose_name="מקור")),
                ("amount", models.DecimalField(decimal_places=0, max_digits=10, verbose_name="עלות (₪)")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-month", "source"], "unique_together": {("workspace", "month", "source")}},
        ),
        migrations.CreateModel(
            name="AgentRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("agent", models.CharField(choices=[("ops", "מתפעלת"), ("reviewer", "מנהל תפעול"), ("business", "מנהל עסקי")], db_index=True, max_length=10)),
                ("workspace", models.CharField(db_index=True, default="finance", max_length=10)),
                ("facts", models.JSONField(default=dict)),
                ("output", models.JSONField(default=dict)),
                ("summary", models.TextField(blank=True)),
                ("used_ai", models.BooleanField(default=False)),
                ("model_name", models.CharField(blank=True, max_length=60)),
                ("error", models.CharField(blank=True, max_length=300)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("case", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="agent_runs", to="leads.case")),
                ("requested_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]
