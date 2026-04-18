import os
import sys
from dotenv import load_dotenv

load_dotenv()

# Allow `from chat.agents.ingestor import ...` when run from the project root
sys.path.insert(0, os.path.dirname(__file__))

from llama_parse import LlamaParse
from pinecone import Pinecone
from langchain_core.documents import Document as LCDocument
from langchain_pinecone import PineconeVectorStore
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from chat.agents.ingestor import extract_doc_metadata

# Pinecone's default namespace is "" (empty string) in the SDK.
# langchain_pinecone uses the sentinel "__default__" in its API but maps it
# to "" when communicating with Pinecone. Raw SDK calls must use "" directly.
_PINECONE_DEFAULT_NS = ""


def bulk_upsert(directory_path: str, force_reindex: bool = False) -> None:
    """
    Parses every PDF in directory_path and upserts it to the __default__
    Pinecone namespace with rich metadata (date, document_type).

    Args:
        directory_path: Local folder containing PDF files.
        force_reindex:  When True, existing vectors for each file are deleted
                        before re-uploading so that the updated metadata is
                        applied. When False (default), files already present in
                        Pinecone are skipped.
    """
    api_key    = os.getenv("PINECONE_API_KEY")
    index_name = os.getenv("OPENAI_PINECONE_INDEX_NAME")

    pc    = Pinecone(api_key=api_key)
    index = pc.Index(index_name)

    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    llm        = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    parser     = LlamaParse(result_type="markdown")

    files = [f for f in os.listdir(directory_path) if f.endswith(".pdf")]
    if not files:
        print(f"No PDF files found in '{directory_path}'.")
        return

    print(f"Found {len(files)} PDF(s) in '{directory_path}'.")
    if force_reindex:
        print("force_reindex=True: existing vectors will be deleted and re-uploaded.\n")
    else:
        print("force_reindex=False: files already in Pinecone will be skipped.\n")

    for filename in files:
        # -- Deduplication / force-delete check -------------------------------
        existing = index.query(
            namespace=_PINECONE_DEFAULT_NS,
            filter={"filename": {"$eq": filename}},
            top_k=1,
            vector=[0.0] * 1536,   # text-embedding-3-small dimensions
            include_values=False,
        )
        already_indexed = len(existing.get("matches", [])) > 0

        if already_indexed and not force_reindex:
            print(f"  Skipping '{filename}': already indexed in Pinecone.")
            continue

        if already_indexed and force_reindex:
            print(f"  Deleting existing vectors for '{filename}'...")
            index.delete(
                namespace=_PINECONE_DEFAULT_NS,
                filter={"filename": {"$eq": filename}},
            )

        # -- Parse ------------------------------------------------------------
        print(f"  Parsing '{filename}'...")
        file_path  = os.path.join(directory_path, filename)
        llama_docs = parser.load_data(file_path)

        if not llama_docs:
            print(f"  Warning: LlamaParse returned no content for '{filename}'. Skipping.")
            continue

        # -- Metadata extraction (one LLM call per document, not per chunk) ---
        first_text   = llama_docs[0].text
        doc_metadata = extract_doc_metadata(first_text, llm, filename=filename)

        date_info = doc_metadata.get("date") or f"unknown (ingested {doc_metadata['date_ingested']})"
        print(f"  Date: {date_info} | Type: {doc_metadata['document_type']}")

        # -- Build LangChain Documents ----------------------------------------
        langchain_docs = []
        for doc in llama_docs:
            langchain_docs.append(LCDocument(
                page_content=doc.text,
                metadata={
                    "source":   filename,
                    "filename": filename,
                    "text":     doc.text,
                    **doc_metadata,
                },
            ))

        # -- Upsert -----------------------------------------------------------
        print(f"  Upserting {len(langchain_docs)} chunk(s)...")
        PineconeVectorStore.from_documents(
            langchain_docs,
            embeddings,
            index_name=index_name,
            namespace="__default__",   # langchain_pinecone maps this to "" internally
        )
        print(f"  Done: '{filename}' uploaded successfully.\n")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Bulk-upload PDFs to Pinecone with metadata extraction.")
    ap.add_argument(
        "directory",
        nargs="?",
        default="./research_memos",
        help="Path to folder containing PDFs (default: ./research_memos)",
    )
    ap.add_argument(
        "--force-reindex",
        action="store_true",
        help=(
            "Delete existing Pinecone vectors for each file before re-uploading. "
            "Use this to backfill date/document_type metadata on already-indexed documents."
        ),
    )
    args = ap.parse_args()

    bulk_upsert(args.directory, force_reindex=args.force_reindex)
