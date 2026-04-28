"""
Template context processors for the chat app.

Exposes app-wide flags (DEMO_MODE) to every Django template so templates can
conditionally render UI affordances. This keeps the demo gate logic in one
place instead of scattering `{% if request... %}` checks across templates.
"""
from django.conf import settings


def demo_flags(request):
    """
    Inject demo-related flags into the template context.

    Used by chat.html and base.html to hide the file upload button and any
    admin-style affordances when running in front of a live audience.
    """
    return {
        "DEMO_MODE": getattr(settings, "DEMO_MODE", False),
    }
