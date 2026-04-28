from django.conf import settings
from django.contrib import admin
from django.urls import include, path

# Admin is mounted at the path configured in settings.ADMIN_URL_PATH
# (default: admin/). Override per-environment via the ADMIN_URL_PATH env var
# so a public deployment doesn't expose /admin/ at the obvious URL.
urlpatterns = [
    path(settings.ADMIN_URL_PATH, admin.site.urls),
    path("", include("chat.urls")),
]
