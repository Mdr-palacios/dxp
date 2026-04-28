"""
Seed the best-practices corpus into Pinecone (__default__ namespace).

Reads every .md file under tool_templates/best_practices/, extracts metadata
from the YAML-style frontmatter (title, source, date, document_type, tags),
chunks the body, and upserts to the OPENAI_PINECONE_INDEX_NAME index with
deterministic vector IDs so re-running the script is idempotent.

Usage (from /powerbuilder):
    python scripts/seed_best_practices.py                  # seed all files
    python scripts/seed_best_practices.py --dry-run        # parse + chunk, no upload
    python scripts/seed_best_practices.py --force-reindex  # delete then re-upload

Required env (loaded from .env):
    OPENAI_API_KEY
    PINECONE_API_KEY
    OPENAI_PINECONE_INDEX_NAME

Falls back to writing a local index at scripts/.local_corpus_index.json if
PINECONE_API_KEY is missing, so the demo can run fully offline. The
researcher agent reads that same file when USE_LOCAL_CORPUS=true.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
CORPUS_DIR = PROJECT_DIR / "tool_templates" / "best_practices"
LOCAL_INDEX_PATH = SCRIPT_DIR / ".local_corpus_index.json"

# ---------------------------------------------------------------------------
# Frontmatter + chunking
# ---------------------------------------------------------------------------

# Two metadata header styles are supported.
#
# Style A (YAML frontmatter):
#   ---
#   title: Latinx GOTV Field Playbook
#   source: Powerbuilder curated corpus
#   date: 2026-04-15
#   document_type: best_practices
#   tags: [latinx, gotv, field, spanish]
#   ---
#
# Style B (markdown header, used by the curated best-practices files):
#   # Title Of The Document
#
#   **Source:** Powerbuilder curated corpus, distilled from ...
#   **Date:** 2025-09-15
#   **Document type:** field playbook
#   **Topics:** latinx, gotv, canvassing, spanish
#
# Anything after the metadata block is the body. If neither header is
# present we derive a title from the filename and treat the full file as body.
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
MD_BOLD_FIELD_RE = re.compile(r"^\*\*([^*]+):\*\*\s*(.+?)\s*$")


def _parse_md_header(text: str, fallback_title: str) -> tuple[dict, str]:
    """
    Parse the markdown-style metadata header used by the curated corpus.

    Title comes from the first # heading; field rows look like
    `**Field:** value`. The body starts at the first non-metadata,
    non-blank line (typically the first ## section heading).
    """
    lines = text.splitlines()
    meta: dict = {"document_type": "best_practices"}
    title = fallback_title
    body_start = 0
    seen_meta = False

    for i, line in enumerate(lines):
        stripped = line.strip()

        if i == 0 and stripped.startswith("# ") and not stripped.startswith("## "):
            title = stripped[2:].strip()
            body_start = i + 1
            continue

        if not stripped:
            body_start = i + 1
            continue

        bold_match = MD_BOLD_FIELD_RE.match(stripped)
        if bold_match:
            key = bold_match.group(1).strip().lower().replace(" ", "_")
            val = bold_match.group(2).strip()
            # Topics: comma-separated tag list -> list of strings.
            if key == "topics":
                val = [t.strip() for t in val.split(",") if t.strip()]
                meta["tags"] = val
            else:
                meta[key] = val
            body_start = i + 1
            seen_meta = True
            continue

        # First non-metadata content line: stop scanning, keep as body start.
        break

    meta["title"] = title
    body = "\n".join(lines[body_start:]).strip()
    if not seen_meta and not body:
        # No metadata block and no remaining content: use full text as body.
        body = text
    return meta, body


def parse_frontmatter(text: str, fallback_title: str) -> tuple[dict, str]:
    """
    Parse a markdown file's metadata header (YAML frontmatter or markdown
    bold-field style) and return (metadata_dict, body).
    """
    match = FRONTMATTER_RE.match(text)
    if match:
        raw_meta, body = match.group(1), match.group(2)
        meta: dict = {}
        for line in raw_meta.splitlines():
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if val.startswith("[") and val.endswith("]"):
                val = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
            else:
                val = val.strip("'\"")
            meta[key] = val
        meta.setdefault("title", fallback_title)
        meta.setdefault("document_type", "best_practices")
        return meta, body

    # Fall back to markdown-bold-field header style.
    return _parse_md_header(text, fallback_title)


def chunk_markdown(body: str, max_chars: int = 1500) -> list[str]:
    """
    Split markdown body into chunks at paragraph boundaries.

    We keep chunks reasonably large (default 1500 chars) so each chunk carries
    enough context to be useful on its own. Paragraph boundaries are preferred
    over hard character limits, but if a single paragraph exceeds the limit,
    we fall back to splitting on sentences.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    chunks: list[str] = []
    buf = ""

    for p in paragraphs:
        if len(p) > max_chars:
            # Long paragraph: flush whatever's buffered, then sentence-split.
            if buf:
                chunks.append(buf.strip())
                buf = ""
            sentences = re.split(r"(?<=[.!?])\s+", p)
            sub = ""
            for s in sentences:
                if len(sub) + len(s) + 1 > max_chars and sub:
                    chunks.append(sub.strip())
                    sub = s
                else:
                    sub = (sub + " " + s).strip()
            if sub:
                chunks.append(sub.strip())
            continue

        if len(buf) + len(p) + 2 > max_chars and buf:
            chunks.append(buf.strip())
            buf = p
        else:
            buf = (buf + "\n\n" + p).strip()

    if buf:
        chunks.append(buf.strip())

    return chunks


def vector_id(source_file: str, chunk_idx: int) -> str:
    """Deterministic ID so re-runs upsert (not duplicate)."""
    h = hashlib.sha1(f"{source_file}:{chunk_idx}".encode()).hexdigest()[:16]
    return f"bp-{h}"


# ---------------------------------------------------------------------------
# Document collection
# ---------------------------------------------------------------------------

def collect_documents() -> list[dict]:
    """
    Walk CORPUS_DIR and return one doc dict per chunk, ready to embed/upsert.
    """
    if not CORPUS_DIR.exists():
        raise FileNotFoundError(
            f"Corpus directory not found: {CORPUS_DIR}. "
            f"Did you run this from /powerbuilder?"
        )

    md_files = sorted(CORPUS_DIR.glob("*.md"))
    if not md_files:
        raise FileNotFoundError(f"No .md files found under {CORPUS_DIR}.")

    docs: list[dict] = []
    for md_path in md_files:
        text = md_path.read_text(encoding="utf-8")
        fallback_title = md_path.stem.replace("_", " ").title()
        meta, body = parse_frontmatter(text, fallback_title)
        chunks = chunk_markdown(body)

        for idx, chunk in enumerate(chunks):
            docs.append({
                "id": vector_id(md_path.name, idx),
                "text": chunk,
                "metadata": {
                    **meta,
                    "source_file": md_path.name,
                    "chunk_index": idx,
                    "total_chunks": len(chunks),
                    # langchain_pinecone reads "source" for citation display.
                    "source": meta.get("source", meta.get("title", md_path.name)),
                    # Map meta date into top-level for the researcher's recency sort.
                    "date": meta.get("date", ""),
                },
            })

    return docs


# ---------------------------------------------------------------------------
# Pinecone path
# ---------------------------------------------------------------------------

def upsert_to_pinecone(docs: list[dict], force_reindex: bool) -> None:
    """
    Embed every chunk and upsert to OPENAI_PINECONE_INDEX_NAME / __default__.
    """
    from langchain_openai import OpenAIEmbeddings
    from pinecone import Pinecone

    api_key = os.getenv("PINECONE_API_KEY")
    index_name = os.getenv("OPENAI_PINECONE_INDEX_NAME")
    if not (api_key and index_name):
        raise RuntimeError(
            "PINECONE_API_KEY and OPENAI_PINECONE_INDEX_NAME must be set."
        )

    pc = Pinecone(api_key=api_key)
    index = pc.Index(index_name)
    embeddings = OpenAIEmbeddings(model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"))

    # langchain_pinecone uses sentinel "__default__" but the raw SDK
    # uses "" for the default namespace.
    DEFAULT_NS = ""

    if force_reindex:
        ids_to_delete = [d["id"] for d in docs]
        # Pinecone enforces a 1000-id limit per delete call.
        for i in range(0, len(ids_to_delete), 1000):
            batch = ids_to_delete[i:i + 1000]
            try:
                index.delete(ids=batch, namespace=DEFAULT_NS)
            except Exception as exc:
                # If the IDs don't exist yet (first run), Pinecone may 404. Ignore.
                print(f"  delete batch skipped: {exc}")

    print(f"Embedding {len(docs)} chunks with text-embedding-3-small...")
    texts = [d["text"] for d in docs]
    vectors = embeddings.embed_documents(texts)

    print(f"Upserting to index '{index_name}', namespace '__default__'...")
    payload = [
        {"id": d["id"], "values": vec, "metadata": {**d["metadata"], "text": d["text"]}}
        for d, vec in zip(docs, vectors)
    ]
    # Pinecone allows up to 100 vectors per upsert call.
    for i in range(0, len(payload), 100):
        index.upsert(vectors=payload[i:i + 100], namespace=DEFAULT_NS)

    print(f"Done. Upserted {len(payload)} vectors.")


# ---------------------------------------------------------------------------
# Local fallback path
# ---------------------------------------------------------------------------

def write_local_index(docs: list[dict]) -> None:
    """
    Persist chunks to a local JSON file the researcher can read when Pinecone
    is unavailable or USE_LOCAL_CORPUS=true. No embeddings, just the text and
    metadata; the researcher does keyword scoring against this index.
    """
    payload = {
        "version": 1,
        "chunks": [
            {
                "id": d["id"],
                "text": d["text"],
                "metadata": d["metadata"],
            }
            for d in docs
        ],
    }
    LOCAL_INDEX_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote local fallback index: {LOCAL_INDEX_PATH} ({len(docs)} chunks)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Seed best-practices corpus.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and chunk but do not embed or upload.")
    parser.add_argument("--force-reindex", action="store_true",
                        help="Delete existing vectors with these IDs before upsert.")
    parser.add_argument("--local-only", action="store_true",
                        help="Skip Pinecone, write only the local fallback index.")
    args = parser.parse_args()

    docs = collect_documents()
    print(f"Collected {len(docs)} chunks from {CORPUS_DIR}.")
    by_file: dict[str, int] = {}
    for d in docs:
        f = d["metadata"]["source_file"]
        by_file[f] = by_file.get(f, 0) + 1
    for f, n in sorted(by_file.items()):
        print(f"  {f}: {n} chunks")

    # Always write the local fallback so the researcher has an offline option
    # the moment we run the seeder for the first time.
    write_local_index(docs)

    if args.dry_run:
        print("Dry run complete. No upload.")
        return 0

    if args.local_only:
        print("local-only flag set: skipping Pinecone upload.")
        return 0

    if not os.getenv("PINECONE_API_KEY"):
        print("PINECONE_API_KEY not set: falling back to local-only mode.")
        return 0

    try:
        upsert_to_pinecone(docs, force_reindex=args.force_reindex)
    except Exception as exc:
        print(f"Pinecone upload failed: {exc}")
        print("Local fallback index is still available.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
