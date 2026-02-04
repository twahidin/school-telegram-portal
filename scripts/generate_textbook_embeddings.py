#!/usr/bin/env python3
"""
Generate a textbook embeddings file (JSONL) from a PDF for upload to Railway.

Run locally (where you have memory and OPENAI_API_KEY), then upload the .jsonl
file via the Textbook (RAG) → "Upload embeddings" option in the module view.

Usage:
  export OPENAI_API_KEY=sk-...
  python scripts/generate_textbook_embeddings.py --pdf path/to/textbook.pdf --output textbook_embeddings.jsonl

Options:
  --pdf       Path to PDF file (required)
  --output    Output file path (default: textbook_embeddings.jsonl)
  --title     Optional title for the upload (e.g. "Chapter 1")
  --batch     Batch size for OpenAI embeddings (default: 20)
"""

import argparse
import io
import json
import os
import sys

# Chunking: must match rag_service for consistent RAG behavior
CHUNK_SIZE = 600
CHUNK_OVERLAP = 100
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSION = 1536


def extract_text_from_pdf(pdf_path: str) -> str:
    """Extract text from PDF using PyPDF2."""
    import PyPDF2
    with open(pdf_path, "rb") as f:
        reader = PyPDF2.PdfReader(f)
        parts = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text()
            if text and text.strip():
                parts.append(f"--- Page {i + 1} ---\n{text.strip()}")
        return "\n\n".join(parts)


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list:
    """Split text into overlapping chunks."""
    if not text or not text.strip():
        return []
    text = text.replace("\r\n", "\n").strip()
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        if end >= len(text):
            chunks.append(text[start:].strip())
            break
        break_at = text.rfind("\n\n", start, end + 1)
        if break_at < start:
            break_at = text.rfind(". ", start, end + 1)
        if break_at >= start:
            end = break_at + 1
        chunks.append(text[start:end].strip())
        start = end - overlap
        if start >= len(text):
            break
    return [c for c in chunks if c]


def get_embeddings(texts: list, client) -> list:
    """Get embeddings for a list of texts using OpenAI."""
    if not texts:
        return []
    r = client.embeddings.create(model=EMBEDDING_MODEL, input=texts)
    return [item.embedding for item in r.data]


def main():
    parser = argparse.ArgumentParser(description="Generate textbook embeddings JSONL from a PDF")
    parser.add_argument("--pdf", required=True, help="Path to PDF file")
    parser.add_argument("--output", default="textbook_embeddings.jsonl", help="Output .jsonl file path")
    parser.add_argument("--title", default="", help="Optional title (e.g. Chapter 1)")
    parser.add_argument("--batch", type=int, default=20, help="OpenAI embedding batch size")
    args = parser.parse_args()

    if not os.path.isfile(args.pdf):
        print(f"Error: PDF not found: {args.pdf}", file=sys.stderr)
        sys.exit(1)

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("Error: Set OPENAI_API_KEY environment variable", file=sys.stderr)
        sys.exit(1)

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
    except ImportError:
        print("Error: Install openai: pip install openai", file=sys.stderr)
        sys.exit(1)

    try:
        import PyPDF2
    except ImportError:
        print("Error: Install PyPDF2: pip install PyPDF2", file=sys.stderr)
        sys.exit(1)

    print("Extracting text from PDF...")
    text = extract_text_from_pdf(args.pdf)
    if not text or len(text.strip()) < 100:
        print("Error: Could not extract enough text from PDF (may be image-only)", file=sys.stderr)
        sys.exit(1)
    print(f"  Extracted {len(text)} characters")

    print("Chunking text...")
    chunks = chunk_text(text)
    if not chunks:
        print("Error: No chunks produced", file=sys.stderr)
        sys.exit(1)
    print(f"  {len(chunks)} chunks")

    print("Getting embeddings from OpenAI (this may take a minute)...")
    batch_size = max(1, min(args.batch, 100))
    all_embeddings = []
    for start in range(0, len(chunks), batch_size):
        end = min(start + batch_size, len(chunks))
        batch = chunks[start:end]
        emb = get_embeddings(batch, client)
        if len(emb) != len(batch):
            print("Error: Embedding count mismatch", file=sys.stderr)
            sys.exit(1)
        all_embeddings.extend(emb)
        print(f"  {len(all_embeddings)}/{len(chunks)}")

    print(f"Writing {args.output}...")
    with open(args.output, "w", encoding="utf-8") as f:
        for i, (chunk, embedding) in enumerate(zip(chunks, all_embeddings)):
            obj = {"text": chunk, "embedding": embedding}
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    print(f"Done. Upload {args.output} via Module → Textbook → Upload embeddings (.json / .jsonl)")


if __name__ == "__main__":
    main()
