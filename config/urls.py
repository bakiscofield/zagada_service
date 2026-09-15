from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path

admin.site.site_header = "Zagada Service — administration"
admin.site.site_title = "Zagada Service"
admin.site.index_title = "Dépôts, retraits et clients"


def health(_request):
    return JsonResponse({"ok": True, "service": "zagada_service"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health/", health, name="health"),
    # Webhook du bot Telegram (dépôt/retrait en chat).
    path("api/telegram/", include("telegrambot.urls")),
]
