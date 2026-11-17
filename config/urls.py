import os

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    # Online: set ADMIN_URL to a secret path (e.g. "panel-8f3k2x9/") so the admin login is not at the well-known /admin/.
    path(os.environ.get("ADMIN_URL", "admin/"), admin.site.urls),
    path("", include("leads.urls")),
]
