#!/usr/bin/env python3
"""
Combine multiple embedding files (JSONL or JSON) into one JSONL for upload to School Portal.

Use this when you have 3 (or more) files from different PDFs/chapters and want
one file to upload via Module → Textbook → Upload embeddings.

Usage:
  python scripts/combine_embeddings.py file1.jsonl file2.jsonl file3.jsonl --output combined.jsonl

  # Or with a glob (shell expands it):
  python scripts/combine_embeddings.py chapter_*.jsonl --output combined.jsonl
"""

import argparse
import json
import sys

EMBEDDING_DIMENSION = 1536  # text-embedding-3-small


def load_items(path: str) -> list[dict]:
    """Load items from a .jsonl or .json file."""
    items = []
    path_lower = path.lower()
    with open(path, "r", encoding="utf-8") as f:
        if path_lower.endswith(".jsonl"):
            for line in f:
                line = line.strip()
                if not line:
                    continue
                items.append(json.loads(line))
        else:
            data = json.load(f)
            if isinstance(data, list):
                items = data
            else:
                items = [data]
    return items


def main():
    parser = argparse.ArgumentParser(
        description="Combine multiple embedding JSONL/JSON files into one JSONL file."
    )
    parser.add_argument(
        "files",
        nargs="+",
        help="Paths to .jsonl or .json files (e.g. file1.jsonl file2.jsonl file3.jsonl)",
    )
    parser.add_argument(
        "--output", "-o",
        default="combined_embeddings.jsonl",
        help="Output JSONL file path (default: combined_embeddings.jsonl)",
    )
    args = parser.parse_args()

    all_items = []
    for path in args.files:
        try:
            items = load_items(path)
        except FileNotFoundError:
            print(f"Error: File not found: {path}", file=sys.stderr)
            sys.exit(1)
        except json.JSONDecodeError as e:
            print(f"Error: Invalid JSON in {path}: {e}", file=sys.stderr)
            sys.exit(1)

        for i, item in enumerate(items):
            if not isinstance(item, dict) or "text" not in item or "embedding" not in item:
                print(f"Warning: Skipping invalid item {i} in {path} (need 'text' and 'embedding')", file=sys.stderr)
                continue
            emb = item["embedding"]
            if not isinstance(emb, (list, tuple)) or len(emb) != EMBEDDING_DIMENSION:
                print(f"Warning: Skipping item {i} in {path} (embedding must be {EMBEDDING_DIMENSION}-dim list)", file=sys.stderr)
                continue
            all_items.append({"text": item["text"], "embedding": list(emb)})

        print(f"  {path}: {len(items)} items")

    if not all_items:
        print("Error: No valid items to combine.", file=sys.stderr)
        sys.exit(1)

    with open(args.output, "w", encoding="utf-8") as f:
        for item in all_items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    print(f"Combined {len(all_items)} items → {args.output}")
    print("Upload this file in School Portal: Module → Textbook → Upload embeddings (.json / .jsonl)")


if __name__ == "__main__":
    main()
