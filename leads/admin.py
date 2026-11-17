from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User

from .models import AuditLog, Case, CaseShare, Document, Lead, Message, Note, Task, UserProfile


class ProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    fields = ("role", "workspace", "totp_enabled")
    readonly_fields = ("totp_enabled",)


class UserAdmin(BaseUserAdmin):
    inlines = [ProfileInline]
    list_display = ("username", "first_name", "last_name", "is_active", "is_staff")


admin.site.unregister(User)
admin.site.register(User, UserAdmin)


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = ("full_name", "phone", "city", "status", "workspace", "assigned_to")
    list_filter = ("status", "workspace", "shared_phone", "id_valid")
    search_fields = ("first_name", "last_name", "phone", "phone2", "city")  # ID numbers are encrypted, not searchable here


@admin.register(Case)
class CaseAdmin(admin.ModelAdmin):
    list_display = ("lead", "case_type", "stage", "workspace", "fee", "fee_paid", "owner")
    list_filter = ("case_type", "stage", "workspace", "fee_paid")


@admin.register(AuditLog)
class AuditAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "action", "detail")
    list_filter = ("action",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


admin.site.register([Note, Document, CaseShare, Task, Message])
