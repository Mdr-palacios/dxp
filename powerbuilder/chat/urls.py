from django.urls import path
from django.views.generic import RedirectView

from . import views

urlpatterns = [
    # Root → chat
    path("", RedirectView.as_view(url="/chat/", permanent=False), name="home"),

    # Auth
    path("login/",  views.login_view,  name="login"),
    path("logout/", views.logout_view, name="logout"),

    # Chat UI
    path("chat/",   views.chat_view,          name="chat"),

    # HTMX endpoint — processes a query and returns a message partial
    path("send/",   views.send_message_view,  name="send_message"),

    # File download
    path("download/<str:filename>/", views.download_view, name="download"),
]
