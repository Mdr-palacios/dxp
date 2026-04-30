"""
Tests the welcome interstitial flow:
  1. Anonymous GET /welcome/ -> redirect to /login/ (gated by demo_login_required)
  2. POST /login/ with correct password -> 302 to /welcome/ (not /chat/)
  3. GET /welcome/ when authenticated AND show_welcome flag set ->
     200 with welcome.html content
  4. GET /welcome/ a second time (flag now popped) -> 302 to /chat/
  5. Welcome template renders the headline, eyebrow, accent-W span,
     skip link, and the meta-refresh-style auto-advance JS
  6. Translations work: ES and VI render their localized strings
  7. Logout flushes the session, so a fresh login shows the welcome
     again
"""
from __future__ import annotations

import os
import sys
import html
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "powerbuilder_app.settings")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-validation-only")
os.environ["DEBUG"] = "True"
os.environ.setdefault("DEMO_PASSWORD", "fieldwork-mobilize-78")
os.environ["ALLOWED_HOSTS"] = "testserver,localhost,127.0.0.1"

import django
django.setup()

from django.test import Client
from django.urls import reverse


_assertions = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global _assertions
    _assertions += 1
    if cond:
        print(f"    PASS  {label}")
    else:
        print(f"    FAIL  {label}: {detail}")
        raise AssertionError(f"{label}: {detail}")


def section(title: str) -> None:
    print(f"\n  {title}")


def test_anonymous_redirect_to_login() -> None:
    section("Anonymous /welcome/ redirects to /login/")
    c = Client()
    resp = c.get("/welcome/")
    check("status is 302", resp.status_code == 302, f"got {resp.status_code}")
    check(
        "redirects to /login/",
        resp.url.startswith("/login/"),
        f"got {resp.url}",
    )


def test_login_redirects_to_welcome() -> None:
    section("Successful login redirects to /welcome/, not /chat/")
    c = Client()
    resp = c.post("/login/", {"password": "fieldwork-mobilize-78"})
    check("status is 302", resp.status_code == 302, f"got {resp.status_code}")
    check(
        "redirects to /welcome/",
        resp.url == "/welcome/",
        f"got {resp.url}",
    )
    check(
        "session has authenticated=True",
        c.session.get("authenticated") is True,
    )
    check(
        "session has show_welcome=True",
        c.session.get("show_welcome") is True,
    )


def test_welcome_view_renders_then_pops_flag() -> None:
    section("/welcome/ renders once then pops the flag")
    c = Client()
    c.post("/login/", {"password": "fieldwork-mobilize-78"})

    # First visit: should render
    resp = c.get("/welcome/")
    check("first visit status 200", resp.status_code == 200, f"got {resp.status_code}")
    body = html.unescape(resp.content.decode("utf-8"))

    check(
        "renders welcome-headline class",
        "welcome-headline" in body,
    )
    check(
        "renders English headline text",
        "Ready to build power with you." in body,
    )
    check(
        "renders the accent-W span",
        'class="accent-W">W</span>' in body,
    )
    check(
        "renders Enter now skip link",
        "Enter now" in body,
    )
    check(
        "auto-advance JS targets /chat/",
        '"/chat/"' in body or "'/chat/'" in body,
    )

    # session flag should be cleared after first visit
    check(
        "show_welcome flag was popped",
        c.session.get("show_welcome") is None,
    )

    # Second visit: should bounce to /chat/
    resp2 = c.get("/welcome/")
    check("second visit redirects", resp2.status_code == 302, f"got {resp2.status_code}")
    check(
        "second visit redirects to /chat/",
        resp2.url == "/chat/",
        f"got {resp2.url}",
    )


def test_translations_render() -> None:
    section("ES + VI translations render in welcome.html")

    expected = {
        "es": [
            "Listos para construir poder contigo.",
            "Vamos por esta",
            'class="accent-W">V</span>',
            "Entrar ahora",
        ],
        "vi": [
            "Sẵn sàng xây dựng sức mạnh cùng bạn.",
            "Hãy giành chiến",
            'class="accent-W">T</span>',
            "Vào ngay",
        ],
    }

    for lang, needles in expected.items():
        c = Client()
        c.post("/login/", {"password": "fieldwork-mobilize-78"})
        # Set Django's session-based language preference
        s = c.session
        s["_language"] = lang
        s.save()

        resp = c.get("/welcome/", HTTP_ACCEPT_LANGUAGE=lang)
        check(f"[{lang}] status 200", resp.status_code == 200, f"got {resp.status_code}")
        body = html.unescape(resp.content.decode("utf-8"))
        for needle in needles:
            check(
                f"[{lang}] contains '{needle[:40]}'",
                needle in body,
                f"missing in body (first 600): {body[:600]}",
            )


def test_logout_resets_flag() -> None:
    section("Logout flushes session, next login shows welcome again")
    c = Client()
    c.post("/login/", {"password": "fieldwork-mobilize-78"})
    c.get("/welcome/")  # consumes flag
    check(
        "flag consumed before logout",
        c.session.get("show_welcome") is None,
    )

    # Logout
    resp = c.get("/logout/")
    check("logout 302s", resp.status_code == 302)
    check(
        "session is cleared",
        c.session.get("authenticated") is None,
    )

    # Login again
    c.post("/login/", {"password": "fieldwork-mobilize-78"})
    check(
        "show_welcome flag is set again",
        c.session.get("show_welcome") is True,
    )


def test_url_pattern_registered() -> None:
    section("URL pattern is registered with name 'welcome'")
    url = reverse("welcome")
    check("reverse('welcome') = /welcome/", url == "/welcome/", f"got {url}")


def main() -> int:
    print("Welcome interstitial flow")
    test_url_pattern_registered()
    test_anonymous_redirect_to_login()
    test_login_redirects_to_welcome()
    test_welcome_view_renders_then_pops_flag()
    test_translations_render()
    test_logout_resets_flag()
    print(f"\nALL PASS: {_assertions} assertions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
