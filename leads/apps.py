from django.apps import AppConfig


class LeadsConfig(AppConfig):
    name = "leads"
    verbose_name = "לידים"

    def ready(self):
        from . import signals  # noqa: F401
