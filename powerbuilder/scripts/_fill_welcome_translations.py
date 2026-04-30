"""
Idempotent helper: fills Spanish and Vietnamese translations for the
welcome interstitial strings, and strips `#, fuzzy` markers from any
entries whose msgstr we set.

Run from the powerbuilder/ project root (or from anywhere -- it uses
absolute paths). Re-running is safe; lines that already have the right
translation are left as-is.
"""
from __future__ import annotations

import re
from pathlib import Path

PO_ROOT = Path(__file__).resolve().parent.parent / "locale"

# (msgid, es_msgstr, vi_msgstr)
WELCOME_TRANSLATIONS: list[tuple[str, str, str]] = [
    (
        "Welcome: Powerbuilder",
        "Bienvenido: Powerbuilder",
        "Chào mừng: Powerbuilder",
    ),
    (
        "Welcome",
        "Bienvenido",
        "Chào mừng",
    ),
    (
        "Ready to build power with you.",
        "Listos para construir poder contigo.",
        "Sẵn sàng xây dựng sức mạnh cùng bạn.",
    ),
    (
        # NOTE: matches the template exactly, including the inline span.
        # Translators get the full sentence as one unit so the W can
        # stay accent-styled.
        'Let\'s get this <span class="accent-W">W</span>.',
        'Vamos por esta <span class="accent-W">V</span>.',  # V de victoria
        'Hãy giành chiến <span class="accent-W">T</span>hắng này.',  # Thắng = win
    ),
    (
        "Enter now",
        "Entrar ahora",
        "Vào ngay",
    ),
]


def _po_block_re(msgid: str) -> re.Pattern[str]:
    """Match a PO entry: optional `#, fuzzy` line, the msgid line, then the msgstr line.

    PO files escape backslashes and double quotes with backslashes, so the
    Python-level string `Let's get this <span class="accent-W">W</span>.`
    is stored on disk as `Let's get this <span class=\"accent-W\">W</span>.`
    Apply the same transformation before regex-escaping so we can find it.
    """
    po_form = msgid.replace("\\", "\\\\").replace('"', '\\"')
    escaped = re.escape(po_form)
    # Allow fuzzy markers and other comment lines BEFORE msgid.
    pattern = (
        r"((?:^#.*\n)*)"            # group 1: existing comment block
        rf'msgid "{escaped}"\n'
        r'msgstr "([^"]*)"\n'        # group 2: existing msgstr (may be empty)
    )
    return re.compile(pattern, re.MULTILINE)


def _strip_fuzzy(comment_block: str) -> str:
    """Drop any line that contains `, fuzzy` so this entry will be used at runtime."""
    return "\n".join(
        line for line in comment_block.splitlines()
        if "fuzzy" not in line
    ) + ("\n" if comment_block.endswith("\n") else "")


def fill(po_path: Path, msgid: str, msgstr: str) -> tuple[bool, str]:
    text = po_path.read_text(encoding="utf-8")
    pattern = _po_block_re(msgid)
    match = pattern.search(text)
    if not match:
        return (False, f"NO_MATCH: '{msgid[:50]}'")

    existing_comments = match.group(1)
    existing_msgstr = match.group(2)
    if existing_msgstr == msgstr and "fuzzy" not in existing_comments:
        return (False, f"OK_ALREADY: '{msgid[:50]}'")

    new_comments = _strip_fuzzy(existing_comments)
    # Escape both msgid and msgstr the same way PO files do.
    safe_msgid = msgid.replace("\\", "\\\\").replace('"', '\\"')
    safe_msgstr = msgstr.replace("\\", "\\\\").replace('"', '\\"')
    replacement = (
        f'{new_comments}'
        f'msgid "{safe_msgid}"\n'
        f'msgstr "{safe_msgstr}"\n'
    )
    text = text[:match.start()] + replacement + text[match.end():]
    po_path.write_text(text, encoding="utf-8")
    return (True, f"UPDATED: '{msgid[:50]}'")


def main() -> int:
    es_po = PO_ROOT / "es" / "LC_MESSAGES" / "django.po"
    vi_po = PO_ROOT / "vi" / "LC_MESSAGES" / "django.po"
    if not es_po.exists() or not vi_po.exists():
        print(f"ERROR: missing PO files. Looked for:\n  {es_po}\n  {vi_po}")
        return 1

    changed_any = False
    for msgid, es, vi in WELCOME_TRANSLATIONS:
        for path, val in ((es_po, es), (vi_po, vi)):
            updated, msg = fill(path, msgid, val)
            print(f"  [{path.parent.parent.name}] {msg}")
            changed_any = changed_any or updated

    print()
    print("Changed at least one entry." if changed_any else "All entries already up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
