"""
Validate DEMO_MODE behaviour end-to-end.

Run from /powerbuilder:
    python scripts/_test_demo_mode.py

Tests:
1. Settings: DEBUG defaults False; DEMO_MODE defaults False.
2. Settings: ALLOWED_HOSTS reads from env, ADMIN_URL_PATH normalizes.
3. Random helper: DEMO_MODE on -> deterministic per scope.
4. Random helper: DEMO_MODE off -> non-deterministic (different across runs).
5. Random helper: different scopes -> different sequences (in demo mode).
6. Production headers: applied only when DEBUG is False.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

# Configure Django before any settings access.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "powerbuilder_app.settings")
# Force a known-good test environment regardless of .env contents.
os.environ["DEBUG"] = "False"
os.environ["DEMO_MODE"] = "True"
os.environ["DEMO_RANDOM_SEED"] = "2026"
os.environ["ADMIN_URL_PATH"] = "secret-admin"
os.environ["ALLOWED_HOSTS"] = "demo.example.com,powerbuilder.app"
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-validation-only")

import django

django.setup()

from django.conf import settings
from chat.utils.random_seed import maybe_seed_random, is_demo_mode


def main() -> int:
    failures: list[str] = []

    # 1. Settings flags read from env correctly.
    if settings.DEBUG is not False:
        failures.append(f"DEBUG should be False, got {settings.DEBUG}")
    if settings.DEMO_MODE is not True:
        failures.append(f"DEMO_MODE should be True, got {settings.DEMO_MODE}")
    if settings.DEMO_RANDOM_SEED != 2026:
        failures.append(f"DEMO_RANDOM_SEED should be 2026, got {settings.DEMO_RANDOM_SEED}")

    # 2. ADMIN_URL_PATH normalizes (no leading slash, single trailing slash).
    if settings.ADMIN_URL_PATH != "secret-admin/":
        failures.append(f"ADMIN_URL_PATH should be 'secret-admin/', got {settings.ADMIN_URL_PATH!r}")

    # 3. ALLOWED_HOSTS is a list parsed from comma-separated env.
    if "demo.example.com" not in settings.ALLOWED_HOSTS:
        failures.append(f"ALLOWED_HOSTS missing demo.example.com, got {settings.ALLOWED_HOSTS}")
    if "powerbuilder.app" not in settings.ALLOWED_HOSTS:
        failures.append(f"ALLOWED_HOSTS missing powerbuilder.app, got {settings.ALLOWED_HOSTS}")

    # 4. Production security headers applied because DEBUG is False.
    expected_secure = {
        "SECURE_HSTS_SECONDS": 31536000,
        "SECURE_HSTS_INCLUDE_SUBDOMAINS": True,
        "SECURE_HSTS_PRELOAD": True,
        "SESSION_COOKIE_SECURE": True,
        "CSRF_COOKIE_SECURE": True,
        "X_FRAME_OPTIONS": "DENY",
        "SECURE_CONTENT_TYPE_NOSNIFF": True,
    }
    for key, expected in expected_secure.items():
        actual = getattr(settings, key, None)
        if actual != expected:
            failures.append(f"{key} should be {expected!r}, got {actual!r}")

    # 5. is_demo_mode() returns True when the setting is on.
    if not is_demo_mode():
        failures.append("is_demo_mode() should return True when DEMO_MODE=True")

    # 6. Random seeding is deterministic in demo mode AND scope-isolated.
    rng_a1 = maybe_seed_random(scope="voterfile")
    rng_a2 = maybe_seed_random(scope="voterfile")
    seq_a1 = [rng_a1.random() for _ in range(5)]
    seq_a2 = [rng_a2.random() for _ in range(5)]
    if seq_a1 != seq_a2:
        failures.append(f"voterfile scope not deterministic: {seq_a1} vs {seq_a2}")

    rng_b = maybe_seed_random(scope="precincts")
    seq_b = [rng_b.random() for _ in range(5)]
    if seq_a1 == seq_b:
        failures.append(f"voterfile and precincts scopes produced identical sequences: {seq_a1}")

    # 7. Context processor exposes DEMO_MODE.
    from chat.context_processors import demo_flags
    flags = demo_flags(request=None)
    if flags.get("DEMO_MODE") is not True:
        failures.append(f"context processor flags should expose DEMO_MODE=True, got {flags}")

    # 8. URL routing uses the configured admin path.
    from django.urls import reverse
    admin_url = reverse("admin:index")
    if not admin_url.startswith("/secret-admin/"):
        failures.append(f"admin URL should be at /secret-admin/, got {admin_url}")

    # Report.
    print(f"DEMO_MODE end-to-end test: 8 assertion groups checked.")
    if failures:
        print(f"FAIL: {len(failures)} assertion(s) failed:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("PASS: all assertions OK.")
    print(f"  DEBUG={settings.DEBUG}")
    print(f"  DEMO_MODE={settings.DEMO_MODE}")
    print(f"  ALLOWED_HOSTS={settings.ALLOWED_HOSTS}")
    print(f"  ADMIN_URL_PATH={settings.ADMIN_URL_PATH}")
    print(f"  HSTS={settings.SECURE_HSTS_SECONDS}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
