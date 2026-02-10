"""
RAG (Retrieval-Augmented Generation) service for storing and querying textbook content
per module tree. Enables the learning agent to ground responses in uploaded textbook PDFs.

Uses PGvector (PostgreSQL extension) for vector storage and similarity search.

PDF extraction: PyPDF2 (default) or Anthropic Vision when USE_ANTHROPIC_VISION_FOR_PDF=1
and ANTHROPIC_API_KEY is set (better for scanned PDFs, images, tables).
"""

import base64
import gc
import json
import os
import re
import io
import logging
import uuid
from typing import Optional, List, Dict, Any, Tuple

logger = logging.getLogger(__name__)

# Chunking defaults (smaller = less memory on Railway's limited RAM)
CHUNK_SIZE = 600
CHUNK_OVERLAP = 100
MAX_CHUNKS_QUERY = 5
INGEST_BATCH_SIZE = int(os.getenv("RAG_INGEST_BATCH_SIZE", "10"))  # Batch size for OpenAI embedding calls
RAG_MAX_PAGES = int(os.getenv("RAG_MAX_PAGES", "60"))  # Max pages per PDF upload

# OpenAI embedding model (1536 dimensions)
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSION = 1536

# Anthropic Vision: max pages per upload to limit cost/latency
MAX_PAGES_ANTHROPIC_VISION = int(os.getenv("RAG_VISION_MAX_PAGES", "40"))

# Table and schema
RAG_TABLE = "rag_embeddings"


def _log_memory_usage(label: str = ""):
    """Log current memory usage (helps debug OOM on Railway)."""
    try:
        import resource
        import platform
        ru_maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if platform.system() == 'Darwin':
            mem_mb = ru_maxrss / 1024 / 1024
        else:
            mem_mb = ru_maxrss / 1024
        logger.info(f"[MEMORY] {label}: ~{mem_mb:.0f} MB (peak RSS)")
    except Exception as e:
        logger.info(f"[MEMORY] {label}: (unable to measure: {e})")


def _get_pgvector_url() -> Optional[str]:
    """Get PostgreSQL connection URL for pgvector.
    Tries PGVECTOR_DATABASE_URL, then DATABASE_URL, then DATABASE_URL_PRIVATE."""
    url = (
        os.getenv("PGVECTOR_DATABASE_URL", "").strip()
        or os.getenv("DATABASE_URL", "").strip()
        or os.getenv("DATABASE_URL_PRIVATE", "").strip()
    )
    return url if url else None


def _get_pg_conn():
    """Return a psycopg2 connection with pgvector registered, or None."""
    url = _get_pgvector_url()
    if not url:
        return None
    try:
        import psycopg2
        from pgvector.psycopg2 import register_vector

        conn = psycopg2.connect(url)
        conn.autocommit = True  # CREATE EXTENSION requires autocommit
        cur = conn.cursor()
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.close()
        conn.autocommit = False
        register_vector(conn)
        return conn
    except ImportError as e:
        logger.warning("pgvector or psycopg2 not installed: %s", e)
        return None
    except Exception as e:
        logger.warning("Could not connect to pgvector: %s", e)
        return None


def _ensure_table(conn) -> bool:
    """Create rag_embeddings table and enable extension if needed."""
    try:
        cur = conn.cursor()
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {RAG_TABLE} (
                id UUID PRIMARY KEY,
                namespace VARCHAR(255) NOT NULL,
                embedding vector({EMBEDDING_DIMENSION}) NOT NULL,
                content TEXT NOT NULL,
                metadata JSONB
            )
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_rag_namespace ON {RAG_TABLE} (namespace)
        """)
        try:
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_rag_embedding ON {RAG_TABLE}
                USING hnsw (embedding vector_cosine_ops)
            """)
        except Exception as idx_err:
            logger.warning("Could not create HNSW index (queries will still work): %s", idx_err)
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        logger.exception("Error ensuring pgvector table: %s", e)
        conn.rollback()
        return False


def _namespace_name(module_id: str) -> str:
    """Namespace for a module's textbook. Sanitize for safe use."""
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", module_id)
    return f"textbook_{safe}"[:255]


def _extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extract text from PDF using PyPDF2 (lightweight, low memory).
    
    Works well for text-based PDFs. For scanned/image PDFs, may return empty text.
    Limits to RAG_MAX_PAGES to avoid OOM on memory-constrained hosts.
    """
    try:
        import PyPDF2
        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        total_pages = len(reader.pages)
        max_pages = min(RAG_MAX_PAGES, total_pages)
        if total_pages > RAG_MAX_PAGES:
            logger.warning(f"PyPDF2: Limiting to first {max_pages} of {total_pages} pages (RAG_MAX_PAGES) to avoid OOM")
        logger.info(f"PyPDF2: Processing {max_pages} pages")
        
        parts = []
        pages_with_text = 0
        for i in range(max_pages):
            page = reader.pages[i]
            text = page.extract_text()
            if text and text.strip():
                parts.append(f"--- Page {i + 1} ---\n{text.strip()}")
                pages_with_text += 1
            del page  # Release page object promptly
        del reader
        gc.collect()
        
        logger.info(f"PyPDF2: Extracted text from {pages_with_text}/{max_pages} pages")
        if pages_with_text == 0:
            logger.warning("PyPDF2: No text extracted - PDF may be scanned/image-only")
        
        return "\n\n".join(parts)
    except Exception as e:
        logger.error("Error extracting text from PDF with PyPDF2: %s", e)
        return ""


def _get_anthropic_client():
    """Return Anthropic client if API key is available."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        from anthropic import Anthropic
        return Anthropic(api_key=api_key)
    except Exception as e:
        logger.warning("Anthropic client not available: %s", e)
        return None


def _extract_text_from_pdf_via_anthropic(pdf_bytes: bytes) -> str:
    """Extract text from PDF using Anthropic Vision (PDF → images → Claude).
    Better for scanned PDFs, images, tables. Processes one page at a time to avoid OOM."""
    client = _get_anthropic_client()
    if not client:
        return ""
    try:
        from pdf2image import convert_from_bytes
        from PIL import Image
    except ImportError:
        logger.warning("pdf2image or PIL not available for Anthropic Vision extraction")
        return ""
    max_pages = min(MAX_PAGES_ANTHROPIC_VISION, 100)
    parts = []
    for page_one_indexed in range(1, max_pages + 1):
        try:
            images = convert_from_bytes(
                pdf_bytes,
                first_page=page_one_indexed,
                last_page=page_one_indexed,
                dpi=100,
            )
        except Exception as e:
            if page_one_indexed == 1:
                logger.error("Error converting PDF to images for Vision: %s", e)
            break
        if not images:
            break
        img = images[0]
        del images
        w, h = img.size
        max_dim = 1400
        if w > max_dim or h > max_dim:
            ratio = min(max_dim / w, max_dim / h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        del img
        buf.seek(0)
        b64 = base64.b64encode(buf.read()).decode("utf-8")
        del buf
        try:
            resp = client.messages.create(
                model="claude-sonnet-4-5-20250929",
                max_tokens=4096,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": "Extract all text from this page of a textbook or document. Preserve paragraphs and structure. Output plain text only, no markdown or headings. If the page is mostly images or diagrams, describe them briefly in text.",
                        },
                    ],
                }],
            )
            text = (resp.content[0].text if resp.content else "").strip()
            if text:
                parts.append(f"--- Page {page_one_indexed} ---\n{text}")
        except Exception as e:
            logger.warning("Anthropic Vision extraction failed for page %s: %s", page_one_indexed, e)
    return "\n\n".join(parts) if parts else ""


def _chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[str]:
    """Split text into overlapping chunks (by character count, roughly sentence-aware)."""
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


def _get_openai_client():
    """Return OpenAI client if API key is available."""
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        from openai import OpenAI
        return OpenAI(api_key=api_key)
    except Exception as e:
        logger.warning("OpenAI client not available: %s", e)
        return None


def _get_embeddings(texts: List[str], openai_client) -> List[List[float]]:
    """Get embeddings for a list of texts using OpenAI."""
    if not texts:
        return []
    try:
        response = openai_client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=texts,
        )
        return [item.embedding for item in response.data]
    except Exception as e:
        logger.error("Error getting embeddings: %s", e)
        return []


def _pgvector_not_available_message() -> str:
    """User-facing message when pgvector is not configured."""
    return (
        "Vector store not available. Set PGVECTOR_DATABASE_URL (or DATABASE_URL / DATABASE_URL_PRIVATE) "
        "to your PostgreSQL connection string with the pgvector extension."
    )


def ingest_textbook(
    module_id: str,
    pdf_bytes: bytes,
    title: Optional[str] = None,
    append: bool = True,
) -> Dict[str, Any]:
    """
    Ingest a textbook PDF for a module tree: extract text, chunk, embed, store in PGvector.
    By default appends to existing content so you can upload chapters one at a time.
    """
    logger.info(f"=== Starting textbook ingest (PDF): {len(pdf_bytes)} bytes ===")
    _log_memory_usage("Start of ingest")
    
    use_vision = os.getenv("USE_ANTHROPIC_VISION_FOR_PDF", "").strip().lower() in ("1", "true", "yes")
    
    if use_vision and _get_anthropic_client():
        logger.info("PDF extraction: Using Anthropic Vision (USE_ANTHROPIC_VISION_FOR_PDF=true)")
        text = _extract_text_from_pdf_via_anthropic(pdf_bytes)
        if not text or len(text.strip()) < 50:
            text = _extract_text_from_pdf(pdf_bytes)
    else:
        logger.info("PDF extraction: Using PyPDF2")
        text = _extract_text_from_pdf(pdf_bytes)
    
    del pdf_bytes
    gc.collect()
    _log_memory_usage("After PDF extraction")
    logger.info(f"Extracted {len(text)} characters")
    
    if not text or len(text.strip()) < 100:
        return {"success": False, "error": "Could not extract enough text from the PDF (may be image-only or corrupted)."}

    return ingest_text_content(module_id, text, title=title, append=append)


def ingest_text_content(
    module_id: str,
    text: str,
    title: Optional[str] = None,
    append: bool = True,
) -> Dict[str, Any]:
    """
    Ingest raw text into RAG: chunk, embed, store in PGvector.
    Used for TXT files or after PDF extraction.
    """
    logger.info(f"=== Ingesting text content: {len(text)} characters ===")
    if not text or len(text.strip()) < 100:
        return {"success": False, "error": "Text is too short (need at least 100 characters)."}

    conn = _get_pg_conn()
    if not conn:
        return {"success": False, "error": _pgvector_not_available_message()}

    openai_client = _get_openai_client()
    if not openai_client:
        conn.close()
        return {"success": False, "error": "Embeddings not available (set OPENAI_API_KEY)."}

    if not _ensure_table(conn):
        conn.close()
        return {"success": False, "error": "Could not create RAG table."}

    chunks = _chunk_text(text)
    del text
    gc.collect()
    
    if not chunks:
        conn.close()
        return {"success": False, "error": "No text chunks produced."}

    namespace = _namespace_name(module_id)
    upload_title = (title or "Textbook").strip()[:200]

    total_chunks = len(chunks)
    try:
        cur = conn.cursor()
        if not append:
            cur.execute(f"DELETE FROM {RAG_TABLE} WHERE namespace = %s", (namespace,))

        batch_size = INGEST_BATCH_SIZE
        total_upserted = 0

        for start in range(0, total_chunks, batch_size):
            end = min(start + batch_size, total_chunks)
            batch_chunks = chunks[start:end]
            
            embeddings = _get_embeddings(batch_chunks, openai_client)
            if not embeddings or len(embeddings) != len(batch_chunks):
                cur.close()
                conn.close()
                return {"success": False, "error": "Failed to generate embeddings for chunks."}
            
            for i, (chunk, embedding) in enumerate(zip(batch_chunks, embeddings)):
                meta = {
                    "page_chunk": start + i + 1,
                    "total_chunks": total_chunks,
                    "upload_title": upload_title,
                }
                # Pass embedding as pgvector string literal — avoids importing numpy (~30-40 MB)
                emb_str = "[" + ",".join(str(v) for v in embedding) + "]"
                cur.execute(
                    f"""
                    INSERT INTO {RAG_TABLE} (id, namespace, embedding, content, metadata)
                    VALUES (%s, %s, %s::vector, %s, %s)
                    """,
                    (
                        str(uuid.uuid4()),
                        namespace,
                        emb_str,
                        chunk,
                        json.dumps(meta),
                    ),
                )
                total_upserted += 1
            
            conn.commit()  # Commit each batch to release memory
            logger.info(f"Ingested batch {start}-{end} of {total_chunks} chunks ({total_upserted} total)")
            del batch_chunks, embeddings
            gc.collect()

        # Free chunks list before final query
        del chunks
        gc.collect()

        cur.execute(f"SELECT COUNT(*) FROM {RAG_TABLE} WHERE namespace = %s", (namespace,))
        total_count = cur.fetchone()[0]
        cur.close()
        conn.commit()
        conn.close()

        _log_memory_usage("After ingest complete")
        return {
            "success": True,
            "chunk_count": total_chunks,
            "total_chunk_count": total_count,
            "title": title or "Textbook",
        }
    except Exception as e:
        logger.exception("Error ingesting textbook for module %s: %s", module_id, e)
        conn.rollback()
        try:
            conn.close()
        except Exception:
            pass
        return {"success": False, "error": str(e), "chunk_count": 0, "total_chunk_count": 0}


def ingest_precomputed_embeddings(
    module_id: str,
    items: List[Dict[str, Any]],
    title: Optional[str] = None,
    append: bool = True,
) -> Dict[str, Any]:
    """
    Ingest pre-computed embeddings (from generate_textbook_embeddings.py).
    Each item: {"text": "...", "embedding": [float, ...]}.
    Skips OpenAI calls — ideal for memory-constrained hosts.
    """
    logger.info(f"=== Ingesting {len(items)} pre-computed embeddings ===")

    conn = _get_pg_conn()
    if not conn:
        return {"success": False, "error": _pgvector_not_available_message()}

    if not _ensure_table(conn):
        conn.close()
        return {"success": False, "error": "Could not create RAG table."}

    namespace = _namespace_name(module_id)
    upload_title = (title or "Textbook").strip()[:200]
    total_items = len(items)

    try:
        cur = conn.cursor()
        if not append:
            cur.execute(f"DELETE FROM {RAG_TABLE} WHERE namespace = %s", (namespace,))

        total_upserted = 0
        batch_size = INGEST_BATCH_SIZE

        for start in range(0, total_items, batch_size):
            end = min(start + batch_size, total_items)
            for i in range(start, end):
                item = items[i]
                text = item.get("text", "").strip()
                embedding = item.get("embedding")
                if not text or not embedding:
                    continue
                meta = {
                    "page_chunk": i + 1,
                    "total_chunks": total_items,
                    "upload_title": upload_title,
                }
                emb_str = "[" + ",".join(str(v) for v in embedding) + "]"
                cur.execute(
                    f"""
                    INSERT INTO {RAG_TABLE} (id, namespace, embedding, content, metadata)
                    VALUES (%s, %s, %s::vector, %s, %s)
                    """,
                    (
                        str(uuid.uuid4()),
                        namespace,
                        emb_str,
                        text,
                        json.dumps(meta),
                    ),
                )
                total_upserted += 1

            conn.commit()
            logger.info(f"Ingested pre-computed batch {start}-{end} of {total_items}")
            gc.collect()

        cur.execute(f"SELECT COUNT(*) FROM {RAG_TABLE} WHERE namespace = %s", (namespace,))
        total_count = cur.fetchone()[0]
        cur.close()
        conn.commit()
        conn.close()

        return {
            "success": True,
            "chunk_count": total_upserted,
            "total_chunk_count": total_count,
            "title": title or "Textbook",
        }
    except Exception as e:
        logger.exception("Error ingesting precomputed embeddings for module %s: %s", module_id, e)
        conn.rollback()
        try:
            conn.close()
        except Exception:
            pass
        return {"success": False, "error": str(e), "chunk_count": 0, "total_chunk_count": 0}


def query_textbook(
    module_id: str,
    query: str,
    k: int = MAX_CHUNKS_QUERY,
) -> Dict[str, Any]:
    """
    Query the textbook RAG store for a module. Returns relevant chunks for context.

    Args:
        module_id: Root module_id of the module tree.
        query: Natural language or keyword query (or current module title/objectives).
        k: Max number of chunks to return.

    Returns:
        Dict with success, chunks (list of { content, metadata }), error.
    """
    if not query or not query.strip():
        return {"success": True, "chunks": []}

    conn = _get_pg_conn()
    if not conn:
        return {"success": False, "chunks": [], "error": _pgvector_not_available_message()}

    openai_client = _get_openai_client()
    if not openai_client:
        return {"success": False, "chunks": [], "error": "Embeddings not available."}

    namespace = _namespace_name(module_id)

    try:
        embeddings = _get_embeddings([query.strip()], openai_client)
        if not embeddings:
            return {"success": False, "chunks": [], "error": "Failed to generate query embedding."}
        
        # Pass as pgvector string literal — avoids importing numpy
        query_emb_str = "[" + ",".join(str(v) for v in embeddings[0]) + "]"

        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT content, metadata FROM {RAG_TABLE}
            WHERE namespace = %s
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (namespace, query_emb_str, min(k, 10)),
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()

        chunks = []
        for content, meta in rows:
            if meta and isinstance(meta, dict):
                m = dict(meta)
            else:
                m = json.loads(meta) if isinstance(meta, str) else {}
            chunks.append({"content": content or "", "metadata": m})

        return {"success": True, "chunks": chunks}
    except Exception as e:
        logger.warning("Error querying textbook for module %s: %s", module_id, e)
        try:
            conn.close()
        except Exception:
            pass
        return {"success": False, "chunks": [], "error": str(e)}


def textbook_has_content(module_id: str) -> bool:
    """Return True if this module has a textbook ingested in the vector store."""
    conn = _get_pg_conn()
    if not conn:
        return False
    
    namespace = _namespace_name(module_id)
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {RAG_TABLE} WHERE namespace = %s", (namespace,))
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        return count > 0
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return False


def delete_textbook(module_id: str) -> Dict[str, Any]:
    """Remove textbook content for this module."""
    conn = _get_pg_conn()
    if not conn:
        return {"success": False, "error": _pgvector_not_available_message()}
    
    namespace = _namespace_name(module_id)
    try:
        cur = conn.cursor()
        cur.execute(f"DELETE FROM {RAG_TABLE} WHERE namespace = %s", (namespace,))
        cur.close()
        conn.commit()
        conn.close()
        return {"success": True}
    except Exception as e:
        logger.warning("Error deleting textbook for module %s: %s", module_id, e)
        conn.rollback()
        try:
            conn.close()
        except Exception:
            pass
        return {"success": False, "error": str(e)}
