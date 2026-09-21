"""Admin: a window, not a control panel.

Editing a ticket here would bypass the service layer, and therefore the state
machine, the version counter and the audit trail. The one guarantee this system
makes is that no change happens without a trace, so the admin is registered
read-only rather than left as a quiet way around it.
"""

from django.contrib import admin
from django.http import HttpRequest

from apps.tickets.models import Comment, Ticket, TicketEvent


class ReadOnlyAdminMixin:
    def has_add_permission(self, request: HttpRequest, obj=None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj=None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj=None) -> bool:
        return False


class CommentInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = Comment
    extra = 0
    fields = ("created_at", "author", "is_internal", "body")
    readonly_fields = fields
    ordering = ("created_at",)


class TicketEventInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = TicketEvent
    extra = 0
    fields = ("created_at", "event_type", "actor", "field", "old_value", "new_value", "note")
    readonly_fields = fields
    ordering = ("created_at",)


@admin.register(Ticket)
class TicketAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = (
        "subject",
        "status",
        "priority",
        "reported_priority",
        "customer",
        "assignee",
        "version",
        "created_at",
    )
    list_filter = ("status", "priority", "reported_priority", "assignee")
    search_fields = ("subject", "description", "customer__name", "customer__email", "public_id")
    date_hierarchy = "created_at"
    inlines = [TicketEventInline, CommentInline]
    readonly_fields = [field.name for field in Ticket._meta.fields]

    def get_queryset(self, request: HttpRequest):
        return super().get_queryset(request).select_related("customer", "assignee", "created_by")


@admin.register(Comment)
class CommentAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("ticket", "author", "is_internal", "created_at")
    list_filter = ("is_internal",)
    search_fields = ("body", "ticket__subject")
    readonly_fields = [field.name for field in Comment._meta.fields]


@admin.register(TicketEvent)
class TicketEventAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = (
        "ticket",
        "event_type",
        "actor",
        "field",
        "old_value",
        "new_value",
        "created_at",
    )
    list_filter = ("event_type",)
    search_fields = ("ticket__subject", "note")
    readonly_fields = [field.name for field in TicketEvent._meta.fields]
