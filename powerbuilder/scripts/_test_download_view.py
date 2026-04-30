"""
Tests for chat.views.download_view: streaming export delivery, allowlist
enforcement, and path-traversal defense.

The download endpoint is the last hop between the synthesizer and the
operator's disk; if it stalls on a 5 MB DOCX or trusts a symlink outside
the exports tree, the whole pipeline feels broken. These tests pin the
contract so future changes don't regress it.

Usage:
    python scripts/_test_download_view.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Bootstrap Django so views can import. Mirrors _test_render_helpers.py.
SCRIPT_DIR  = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "powerbuilder_app.settings")
# Provide a deterministic demo password so the auth decorator path is exercised
# the same way in test as in production. Set BEFORE django.setup() so settings
# pick it up if it ever ends up referenced at import time.
os.environ.setdefault("DEMO_PASSWORD", "test-password")

import django  # noqa: E402

django.setup()

from django.http import FileResponse, Http404  # noqa: E402
from django.test import RequestFactory  # noqa: E402

from chat import views  # noqa: E402


def _make_authed_request(rf: RequestFactory):
    """Build a GET request with the session pre-flagged as authenticated."""
    req = rf.get("/download/test.csv/")
    # SessionMiddleware isn't active in this lightweight harness, but the
    # demo_login_required decorator only calls request.session.get(), so a
    # plain dict is enough to satisfy it.
    req.session = {"authenticated": True}
    return req


def main() -> int:
    failures: list[str] = []

    rf = RequestFactory()

    # Use a temporary exports dir so the test never touches real artifacts.
    with tempfile.TemporaryDirectory() as tmp_root:
        exports_dir = os.path.join(tmp_root, "exports")
        os.makedirs(exports_dir, exist_ok=True)

        # Create one of each allowed file type with a known marker payload.
        csv_path  = os.path.join(exports_dir, "targets.csv")
        docx_path = os.path.join(exports_dir, "plan.docx")
        xlsx_path = os.path.join(exports_dir, "rollup.xlsx")
        marker = b"row1,row2\nA,B\n"
        for p in (csv_path, docx_path, xlsx_path):
            with open(p, "wb") as fh:
                fh.write(marker)

        # Patch EXPORTS_DIR for the duration of these tests so download_view
        # resolves into our temp tree rather than the real exports/ folder.
        original_exports = views.EXPORTS_DIR
        views.EXPORTS_DIR = exports_dir
        try:
            # 1. CSV downloads stream back with the right content type and
            #    Content-Disposition: attachment header.
            req = _make_authed_request(rf)
            resp = views.download_view(req, "targets.csv")
            if not isinstance(resp, FileResponse):
                failures.append(
                    f"CSV download should return FileResponse, got {type(resp).__name__}"
                )
            if resp.get("Content-Type", "").split(";")[0] != "text/csv":
                failures.append(
                    f"CSV content-type wrong: {resp.get('Content-Type')!r}"
                )
            disp = resp.get("Content-Disposition", "")
            if "attachment" not in disp or "targets.csv" not in disp:
                failures.append(f"CSV disposition wrong: {disp!r}")
            # Drain the streaming response to confirm the bytes match.
            body = b"".join(resp.streaming_content)
            if body != marker:
                failures.append(f"CSV body mismatch: {body!r}")
            resp.close()

            # 2. DOCX content type matches the OOXML mime.
            req = _make_authed_request(rf)
            resp = views.download_view(req, "plan.docx")
            ct = resp.get("Content-Type", "").split(";")[0]
            expected_docx_ct = (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            )
            if ct != expected_docx_ct:
                failures.append(f"DOCX content-type wrong: {ct!r}")
            resp.close()

            # 3. XLSX content type matches the OOXML mime.
            req = _make_authed_request(rf)
            resp = views.download_view(req, "rollup.xlsx")
            ct = resp.get("Content-Type", "").split(";")[0]
            expected_xlsx_ct = (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            if ct != expected_xlsx_ct:
                failures.append(f"XLSX content-type wrong: {ct!r}")
            resp.close()

            # 4. Disallowed extension -> 404 (Http404 raised).
            req = _make_authed_request(rf)
            try:
                views.download_view(req, "plan.exe")
                failures.append("Disallowed extension should raise Http404")
            except Http404:
                pass

            # 5. Path traversal attempt (..) -> 404 before any disk read.
            req = _make_authed_request(rf)
            try:
                views.download_view(req, "..%2Fetc%2Fpasswd.csv")
                failures.append("Encoded traversal should raise Http404")
            except Http404:
                pass

            # 6. Slash in name -> 404.
            req = _make_authed_request(rf)
            try:
                views.download_view(req, "subdir/foo.csv")
                failures.append("Slash in filename should raise Http404")
            except Http404:
                pass

            # 7. Backslash in name -> 404.
            req = _make_authed_request(rf)
            try:
                views.download_view(req, "subdir\\foo.csv")
                failures.append("Backslash in filename should raise Http404")
            except Http404:
                pass

            # 8. Missing file with allowed extension -> 404.
            req = _make_authed_request(rf)
            try:
                views.download_view(req, "nope.csv")
                failures.append("Missing file should raise Http404")
            except Http404:
                pass

            # 9. Symlink that resolves outside EXPORTS_DIR is rejected by the
            #    realpath confinement check, even though the basename is clean.
            outside = os.path.join(tmp_root, "secret.csv")
            with open(outside, "wb") as fh:
                fh.write(b"do not leak")
            link_path = os.path.join(exports_dir, "linked.csv")
            try:
                os.symlink(outside, link_path)
            except (OSError, NotImplementedError):
                # Skip on platforms / filesystems without symlink support.
                pass
            else:
                req = _make_authed_request(rf)
                try:
                    views.download_view(req, "linked.csv")
                    failures.append(
                        "Symlink to outside EXPORTS_DIR should raise Http404"
                    )
                except Http404:
                    pass
        finally:
            views.EXPORTS_DIR = original_exports

    print("chat.views.download_view test: 9 cases.")
    if failures:
        print(f"FAIL: {len(failures)} assertion(s) failed:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS: all assertion groups OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
