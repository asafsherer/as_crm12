from django.db import models

from .crypto import decrypt_text, encrypt_text


class EncryptedCharField(models.TextField):
    """Stores text encrypted at rest; behaves like a normal string in Python.
    Cannot be searched or filtered by value (use a separate hash column for lookups)."""

    def from_db_value(self, value, expression, connection):
        return decrypt_text(value) if value else value

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        return encrypt_text(value) if value else value
