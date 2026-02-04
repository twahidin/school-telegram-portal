"""
RAG (Retrieval-Augmented Generation) service for storing and querying textbook content
per module tree. Enables the learning agent to ground responses in uploaded textbook PDFs.

Uses Pinecone (hosted vector DB) to avoid memory issues on Railway/limited environments.

PDF extraction: PyPDF2 (default) or Anthropic Vision when USE_ANTHROPIC_VISION_FOR_PDF=1
and ANTHROPIC_API_KEY is set (better for scanned PDFs, images, tables).
"""

import base64
import gc
import os
import re
import io
import logging
import uuid
from typing import Optional, List, Dict, Any, Tuple

logger = logging.getLogger(__name__)

# Chunking defaults (smaller = less memory on Railway's limited RAM)
CHUNK_SIZE = 600  # Smaller chunks = less memory per batch
CHUNK_OVERLAP = 100
MAX_CHUNKS_QUERY = 5
INGEST_BATCH_SIZE = 5  # Very small batches to avoid OOM

# OpenAI embedding model (1536 dimensions)
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSION = 1536

# Anthropic Vision: max pages per upload to limit cost/latency (env override optional)
MAX_PAGES_ANTHROPIC_VISION = int(os.getenv("RAG_VISION_MAX_PAGES", "40"))


def _extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extract text from PDF using PyPDF2 (lightweight, low memory).
    
    Works well for text-based PDFs. For scanned/image PDFs, may return empty text.
    """
    try:
        import PyPDF2
        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        total_pages = len(reader.pages)
        logger.info(f"PyPDF2: Processing {total_pages} pages")
        
        parts = []
        pages_with_text = 0
        for i, page in enumerate(reader.pages):
            text = page.extract_text()
            if text and text.strip():
                parts.append(f"--- Page {i + 1} ---\n{text.strip()}")
                pages_with_text += 1
        
        logger.info(f"PyPDF2: Extracted text from {pages_with_text}/{total_pages} pages")
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
        # Prefer breaking at paragraph or sentence
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


def _get_pinecone_index() -> Tuple[Optional[Any], Optional[str]]:
    """Return (Pinecone index, None) if OK, else (None, error_code).
    error_code: None = no credentials, 'index_not_found' = 404, 'other' = other error."""
    api_key = os.getenv("PINECONE_API_KEY", "").strip()
    index_name = os.getenv("PINECONE_INDEX_NAME", "").strip()
    
    if not api_key or not index_name:
        return (None, None)
    
    try:
        from pinecone import Pinecone
        pc = Pinecone(api_key=api_key)
        return (pc.Index(index_name), None)
    except ImportError:
        logger.warning("pinecone not installed; run: pip install pinecone")
        return (None, "other")
    except Exception as e:
        err_str = str(e).lower()
        if "404" in err_str or "not found" in err_str or "resource" in err_str:
            logger.warning(
                "Pinecone index %r not found. Create it in the Pinecone console: dimension %s, metric cosine.",
                index_name, EMBEDDING_DIMENSION
            )
            return (None, "index_not_found")
        logger.warning("Pinecone not available: %s", e)
        return (None, "other")


def _pinecone_index_not_found_message() -> str:
    """User-facing message when Pinecone index does not exist (404)."""
    index_name = os.getenv("PINECONE_INDEX_NAME", "school-portal")
    return (
        f"Pinecone index '{index_name}' not found. Create it in the Pinecone console: "
        f"dimension {EMBEDDING_DIMENSION}, metric cosine, then redeploy."
    )


def _namespace_name(module_id: str) -> str:
    """Pinecone namespace for a module's textbook. Sanitize for Pinecone."""
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", module_id)
    return f"textbook_{safe}"[:63]


def ingest_textbook(
    module_id: str,
    pdf_bytes: bytes,
    title: Optional[str] = None,
    append: bool = True,
) -> Dict[str, Any]:
    """
    Ingest a textbook PDF for a module tree: extract text, chunk, embed, store in Pinecone.
    By default appends to existing content so you can upload chapters one at a time.

    Args:
        module_id: Root module_id of the module tree.
        pdf_bytes: Raw PDF file bytes.
        title: Optional display name for this upload (e.g. chapter name).
        append: If True (default), add to existing RAG content. If False, replace all content.

    Returns:
        Dict with success, chunk_count (this upload), total_chunk_count (total in RAG), error.
    """
    index, pinecone_err = _get_pinecone_index()
    if not index:
        if pinecone_err == "index_not_found":
            return {"success": False, "error": _pinecone_index_not_found_message()}
        return {"success": False, "error": "Vector store not available (set PINECONE_API_KEY and PINECONE_INDEX_NAME)."}

    openai_client = _get_openai_client()
    if not openai_client:
        return {"success": False, "error": "Embeddings not available (set OPENAI_API_KEY)."}

    # PDF text extraction method selection
    # PyPDF2 (default): Low memory, works for text-based PDFs
    # Anthropic Vision (optional): Memory-intensive, better for scanned PDFs
    # WARNING: Anthropic Vision uses pdf2image which can cause OOM on Railway
    use_vision = os.getenv("USE_ANTHROPIC_VISION_FOR_PDF", "").strip().lower() in ("1", "true", "yes")
    
    if use_vision and _get_anthropic_client():
        logger.info("PDF extraction: Using Anthropic Vision (USE_ANTHROPIC_VISION_FOR_PDF=true)")
        logger.warning("Anthropic Vision is memory-intensive. If you get OOM errors, disable it in Railway env vars.")
        text = _extract_text_from_pdf_via_anthropic(pdf_bytes)
        if not text or len(text.strip()) < 50:
            logger.info("Vision extraction returned little text, falling back to PyPDF2")
            text = _extract_text_from_pdf(pdf_bytes)
    else:
        logger.info("PDF extraction: Using PyPDF2 (lightweight, recommended for Railway)")
        text = _extract_text_from_pdf(pdf_bytes)
    
    # Free PDF bytes from memory immediately after extraction
    del pdf_bytes
    gc.collect()
    logger.info(f"Extracted {len(text)} characters, freed PDF from memory")
    
    if not text or len(text.strip()) < 100:
        return {"success": False, "error": "Could not extract enough text from the PDF (may be image-only or corrupted)."}

    chunks = _chunk_text(text)
    # Free full text after chunking
    del text
    gc.collect()
    
    if not chunks:
        return {"success": False, "error": "No text chunks produced from PDF."}

    namespace = _namespace_name(module_id)
    upload_title = (title or "Textbook").strip()[:200]

    try:
        # Delete existing content if not appending
        if not append:
            try:
                index.delete(delete_all=True, namespace=namespace)
            except Exception:
                pass

        # Process in batches
        batch_size = INGEST_BATCH_SIZE
        total_upserted = 0
        
        for start in range(0, len(chunks), batch_size):
            end = min(start + batch_size, len(chunks))
            batch_chunks = chunks[start:end]
            
            # Get embeddings for this batch
            embeddings = _get_embeddings(batch_chunks, openai_client)
            if not embeddings or len(embeddings) != len(batch_chunks):
                return {"success": False, "error": "Failed to generate embeddings for chunks."}
            
            # Prepare vectors for Pinecone
            vectors = []
            for i, (chunk, embedding) in enumerate(zip(batch_chunks, embeddings)):
                vector_id = str(uuid.uuid4())
                vectors.append({
                    "id": vector_id,
                    "values": embedding,
                    "metadata": {
                        "text": chunk,
                        "page_chunk": start + i + 1,
                        "total_chunks": len(chunks),
                        "upload_title": upload_title,
                    }
                })
            
            # Upsert to Pinecone
            index.upsert(vectors=vectors, namespace=namespace)
            total_upserted += len(vectors)
            
            # Free batch memory immediately
            del batch_chunks, embeddings, vectors
            gc.collect()
            logger.info(f"Batch complete: {total_upserted}/{len(chunks)} chunks uploaded")

        # Get total count in namespace
        try:
            stats = index.describe_index_stats()
            ns_stats = stats.namespaces.get(namespace, {})
            total_count = getattr(ns_stats, 'vector_count', total_upserted)
        except Exception:
            total_count = total_upserted

        return {
            "success": True,
            "chunk_count": len(chunks),
            "total_chunk_count": total_count,
            "title": title or "Textbook",
        }
    except Exception as e:
        err_str = str(e).lower()
        if "404" in err_str or "not found" in err_str:
            return {"success": False, "error": _pinecone_index_not_found_message(), "chunk_count": 0, "total_chunk_count": 0}
        logger.exception("Error ingesting textbook for module %s: %s", module_id, e)
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

    index, pinecone_err = _get_pinecone_index()
    if not index:
        if pinecone_err == "index_not_found":
            return {"success": False, "chunks": [], "error": _pinecone_index_not_found_message()}
        return {"success": False, "chunks": [], "error": "Vector store not available."}

    openai_client = _get_openai_client()
    if not openai_client:
        return {"success": False, "chunks": [], "error": "Embeddings not available."}

    namespace = _namespace_name(module_id)

    try:
        # Get query embedding
        embeddings = _get_embeddings([query.strip()], openai_client)
        if not embeddings:
            return {"success": False, "chunks": [], "error": "Failed to generate query embedding."}
        
        query_embedding = embeddings[0]

        # Query Pinecone
        results = index.query(
            vector=query_embedding,
            top_k=min(k, 10),
            namespace=namespace,
            include_metadata=True,
        )

        if not results.matches:
            return {"success": True, "chunks": []}

        chunks = []
        for match in results.matches:
            metadata = match.metadata or {}
            content = metadata.pop("text", "")
            chunks.append({"content": content, "metadata": metadata})

        return {"success": True, "chunks": chunks}
    except Exception as e:
        err_str = str(e).lower()
        if "404" in err_str or "not found" in err_str:
            return {"success": False, "chunks": [], "error": _pinecone_index_not_found_message()}
        logger.warning("Error querying textbook for module %s: %s", module_id, e)
        return {"success": False, "chunks": [], "error": str(e)}


def textbook_has_content(module_id: str) -> bool:
    """Return True if this module has a textbook ingested in the vector store."""
    index, _ = _get_pinecone_index()
    if not index:
        return False
    
    namespace = _namespace_name(module_id)
    try:
        stats = index.describe_index_stats()
        ns_stats = stats.namespaces.get(namespace, {})
        count = getattr(ns_stats, 'vector_count', 0)
        return count > 0
    except Exception:
        return False


def delete_textbook(module_id: str) -> Dict[str, Any]:
    """Remove textbook content for this module."""
    index, pinecone_err = _get_pinecone_index()
    if not index:
        if pinecone_err == "index_not_found":
            return {"success": False, "error": _pinecone_index_not_found_message()}
        return {"success": False, "error": "Vector store not available."}
    
    namespace = _namespace_name(module_id)
    try:
        index.delete(delete_all=True, namespace=namespace)
        return {"success": True}
    except Exception as e:
        err_str = str(e).lower()
        if "404" in err_str or "not found" in err_str:
            return {"success": False, "error": _pinecone_index_not_found_message()}
        logger.warning("Error deleting textbook for module %s: %s", module_id, e)
        return {"success": False, "error": str(e)}
