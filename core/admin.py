"""Admin Django = poste de travail de l'équipe : la file « À traiter », les
actions « Réussie » / « Rejetée » (qui notifient le client dans Telegram), les
bookmakers et les réglages."""
from __future__ import annotations

from django.contrib import admin, messages
from django.utils import timezone
from django.utils.html import format_html

from . import services
from .models import AdminChat, Bookmaker, Client, PlayerProfile, SiteSettings, Transaction


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    fieldsets = (
        ("Marque", {"fields": ("brand_name", "welcome_text", "closed_message")}),
        ("Ouverture", {"fields": ("deposit_enabled", "payout_enabled")}),
        ("Réception des dépôts", {"fields": (
            ("flooz_number", "flooz_account_name"),
            ("tmoney_number", "tmoney_account_name"),
            "payment_instructions",
        )}),
        ("Support", {"fields": ("support_whatsapp", "support_telegram")}),
    )

    def has_add_permission(self, request):
        return not SiteSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        from django.shortcuts import redirect
        from django.urls import reverse
        return redirect(reverse("admin:core_sitesettings_change", args=[SiteSettings.load().pk]))


@admin.register(Bookmaker)
class BookmakerAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "is_active", "min_deposit", "min_payout", "order")
    list_editable = ("is_active", "min_deposit", "min_payout", "order")
    prepopulated_fields = {"code": ("name",)}


class PlayerProfileInline(admin.TabularInline):
    model = PlayerProfile
    extra = 0
    readonly_fields = ("last_used_at",)


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("display_name", "telegram_id", "phone", "is_blocked", "open_count", "created_at")
    list_filter = ("is_blocked",)
    search_fields = ("username", "first_name", "last_name", "phone", "telegram_id")
    readonly_fields = ("telegram_id", "username", "first_name", "last_name", "created_at", "updated_at")
    inlines = (PlayerProfileInline,)

    @admin.display(description="En cours")
    def open_count(self, obj):
        return obj.transactions.filter(status__in=Transaction.OPEN_STATUSES).count()


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = (
        "id", "kind_badge", "status_badge", "bookmaker", "player_id", "amount",
        "network", "phone", "payout_code", "payment_reference", "client", "created_at",
    )
    list_display_links = ("id", "kind_badge")
    list_filter = ("status", "kind", "network", "bookmaker")
    search_fields = ("id", "player_id", "player_name", "payout_code", "payment_reference",
                     "phone", "client__username", "client__first_name", "client__phone")
    date_hierarchy = "created_at"
    ordering = ("status", "-created_at")
    actions = ("action_success", "action_reject")
    readonly_fields = ("client", "kind", "created_at", "updated_at", "processed_by", "processed_at")
    fieldsets = (
        ("Demande", {"fields": ("client", "kind", "status", "bookmaker", ("player_id", "player_name"))}),
        ("Argent", {"fields": ("amount", ("network", "phone"), "payout_code", "payment_reference")}),
        ("Traitement", {"fields": ("admin_note", ("processed_by", "processed_at"), ("created_at", "updated_at"))}),
    )

    @admin.display(description="Type", ordering="kind")
    def kind_badge(self, obj):
        return ("💰 " if obj.kind == Transaction.Kind.DEPOSIT else "💸 ") + obj.get_kind_display()

    @admin.display(description="Statut", ordering="status")
    def status_badge(self, obj):
        colors = {
            "awaiting_payment": "#9a6700", "pending": "#0969da",
            "success": "#1a7f37", "rejected": "#cf222e", "cancelled": "#6e7781",
        }
        return format_html(
            '<b style="color:{}">{}</b>', colors.get(obj.status, "#000"), obj.get_status_display()
        )

    def save_model(self, request, obj, form, change):
        """Changer le statut depuis le formulaire = décision de l'admin (audit)."""
        if change and "status" in form.changed_data and obj.status in (
            Transaction.Status.SUCCESS, Transaction.Status.REJECTED, Transaction.Status.CANCELLED,
        ):
            obj.processed_by = request.user
            obj.processed_at = timezone.now()
        super().save_model(request, obj, form, change)

    def _apply(self, request, queryset, fn, label):
        done, skipped = 0, 0
        for tx in queryset:
            try:
                fn(tx, request.user)
                done += 1
            except services.ServiceError:
                skipped += 1
        if done:
            self.message_user(request, f"{done} demande(s) {label}. Le client est prévenu dans Telegram.")
        if skipped:
            self.message_user(request, f"{skipped} demande(s) déjà closes, ignorées.", level=messages.WARNING)

    @admin.action(description="✅ Marquer réussie (compte crédité / argent envoyé)")
    def action_success(self, request, queryset):
        self._apply(request, queryset, lambda tx, u: services.mark_success(tx, u), "marquée(s) réussie(s)")

    @admin.action(description="❌ Rejeter")
    def action_reject(self, request, queryset):
        self._apply(request, queryset, lambda tx, u: services.mark_rejected(tx, u), "rejetée(s)")


@admin.register(AdminChat)
class AdminChatAdmin(admin.ModelAdmin):
    list_display = ("label", "chat_id", "is_active", "created_at")
    list_editable = ("is_active",)
