"""
RAG (Retrieval-Augmented Generation) service for storing and querying textbook content
per module tree. Enables the learning agent to ground responses in uploaded textbook PDFs.

Uses Pinecone (hosted vector DB) to avoid memory issues on Railway/limited environments.
"""

import os
import re
import io
import logging
import uuid
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

# Chunking defaults
CHUNK_SIZE = 800
CHUNK_OVERLAP = 150
MAX_CHUNKS_QUERY = 5
INGEST_BATCH_SIZE = 50

# OpenAI embedding model (1536 dimensions)
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSION = 1536


def _extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extract text from PDF using PyPDF2."""
    try:
        import PyPDF2
        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        parts = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text()
            if text and text.strip():
                parts.append(f"--- Page {i + 1} ---\n{text.strip()}")
        return "\n\n".join(parts)
    except Exception as e:
        logger.error("Error extracting text from PDF: %s", e)
        return ""


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


def _get_pinecone_index():
    """Return Pinecone index if configured, else None."""
    api_key = os.getenv("PINECONE_API_KEY", "").strip()
    index_name = os.getenv("PINECONE_INDEX_NAME", "").strip()
    
    if not api_key or not index_name:
        return None
    
    try:
        from pinecone import Pinecone
        pc = Pinecone(api_key=api_key)
        return pc.Index(index_name)
    except ImportError:
        logger.warning("pinecone not installed; run: pip install pinecone")
        return None
    except Exception as e:
        logger.warning("Pinecone not available: %s", e)
        return None


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
    index = _get_pinecone_index()
    if not index:
        return {"success": False, "error": "Vector store not available (set PINECONE_API_KEY and PINECONE_INDEX_NAME)."}

    openai_client = _get_openai_client()
    if not openai_client:
        return {"success": False, "error": "Embeddings not available (set OPENAI_API_KEY)."}

    text = _extract_text_from_pdf(pdf_bytes)
    if not text or len(text.strip()) < 100:
        return {"success": False, "error": "Could not extract enough text from the PDF (may be image-only or corrupted)."}

    chunks = _chunk_text(text)
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

    index = _get_pinecone_index()
    if not index:
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
        logger.warning("Error querying textbook for module %s: %s", module_id, e)
        return {"success": False, "chunks": [], "error": str(e)}


def textbook_has_content(module_id: str) -> bool:
    """Return True if this module has a textbook ingested in the vector store."""
    index = _get_pinecone_index()
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
    index = _get_pinecone_index()
    if not index:
        return {"success": False, "error": "Vector store not available."}
    
    namespace = _namespace_name(module_id)
    try:
        index.delete(delete_all=True, namespace=namespace)
        return {"success": True}
    except Exception as e:
        logger.warning("Error deleting textbook for module %s: %s", module_id, e)
        return {"success": False, "error": str(e)}
