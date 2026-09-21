from django.contrib import admin
from django.db.models import Count

from apps.customers.models import Customer


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "ticket_count", "created_at")
    search_fields = ("name", "email")
    readonly_fields = ("public_id", "created_at")

    def get_queryset(self, request):
        # Annotated, not prefetched: `.count()` on a prefetched manager issues a
        # fresh query per row, which is an N+1 hiding behind an optimisation.
        return super().get_queryset(request).annotate(ticket_count=Count("tickets"))

    @admin.display(description="Tickets", ordering="ticket_count")
    def ticket_count(self, obj: Customer) -> int:
        # Annotated by get_queryset above, so it is not a field on the model.
        return obj.ticket_count  # type: ignore[attr-defined]
