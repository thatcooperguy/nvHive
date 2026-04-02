"""NVHive Knowledge Base — RAG (Retrieval Augmented Generation).

Upload documents → chunk them → search relevant chunks → inject into prompt.

Storage: ~/.hive/knowledge/ (plain text chunks with metadata)
No vector database needed — uses simple keyword/TF-IDF search for MVP.

Usage:
  nvh learn path/to/document.pdf       # ingest a document
  nvh learn path/to/folder/             # ingest all files in a folder
  nvh ask "What does the doc say about X?" --knowledge  # query with RAG
  nvh knowledge list                    # list ingested documents
  nvh knowledge search "keyword"        # search the knowledge base
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass
class Document:
    id: str
    filename: str
    path: str
    doc_type: str           # pdf, txt, md, py, etc.
    num_chunks: int
    ingested_at: str
    size_bytes: int

@dataclass
class Chunk:
    doc_id: str
    chunk_index: int
    content: str
    metadata: dict          # page number, section, etc.

class KnowledgeBase:
    def __init__(self, kb_dir: Path | None = None):
        self.kb_dir = kb_dir or (Path.home() / ".hive" / "knowledge")
        self.kb_dir.mkdir(parents=True, exist_ok=True)
        self.docs_file = self.kb_dir / "documents.json"
        self.chunks_dir = self.kb_dir / "chunks"
        self.chunks_dir.mkdir(exist_ok=True)
        self._docs: list[Document] = []
        self._load_docs()

    def _load_docs(self):
        if self.docs_file.exists():
            try:
                data = json.loads(self.docs_file.read_text())
                self._docs = [Document(**d) for d in data]
            except Exception:
                self._docs = []

    def _save_docs(self):
        data = [
            {"id": d.id, "filename": d.filename, "path": d.path,
             "doc_type": d.doc_type, "num_chunks": d.num_chunks,
             "ingested_at": d.ingested_at, "size_bytes": d.size_bytes}
            for d in self._docs
        ]
        self.docs_file.write_text(json.dumps(data, indent=2))

    def ingest(self, file_path: str) -> Document:
        """Ingest a document into the knowledge base."""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        # Generate doc ID from content hash
        content_bytes = path.read_bytes()
        doc_id = hashlib.sha256(content_bytes).hexdigest()[:12]

        # Check if already ingested
        for d in self._docs:
            if d.id == doc_id:
                return d  # already ingested

        # Extract text based on file type
        ext = path.suffix.lower()
        if ext == ".pdf":
            text = self._extract_pdf(path)
        elif ext in (".txt", ".md", ".rst", ".csv", ".log"):
            text = path.read_text(errors="replace")
        elif ext in (".py", ".js", ".ts", ".java", ".c", ".cpp", ".go", ".rs"):
            text = path.read_text(errors="replace")
        elif ext in (".json", ".yaml", ".yml", ".toml"):
            text = path.read_text(errors="replace")
        else:
            text = path.read_text(errors="replace")

        # Chunk the text
        chunks = self._chunk_text(text, chunk_size=1000, overlap=200)

        # Save chunks
        for i, chunk_text in enumerate(chunks):
            chunk = Chunk(
                doc_id=doc_id,
                chunk_index=i,
                content=chunk_text,
                metadata={"filename": path.name, "chunk": i, "total": len(chunks)},
            )
            chunk_file = self.chunks_dir / f"{doc_id}_{i:04d}.json"
            chunk_file.write_text(json.dumps({
                "doc_id": chunk.doc_id,
                "chunk_index": chunk.chunk_index,
                "content": chunk.content,
                "metadata": chunk.metadata,
            }))

        doc = Document(
            id=doc_id,
            filename=path.name,
            path=str(path.resolve()),
            doc_type=ext.lstrip("."),
            num_chunks=len(chunks),
            ingested_at=datetime.now(UTC).isoformat(),
            size_bytes=len(content_bytes),
        )
        self._docs.append(doc)
        self._save_docs()

        return doc

    def search(self, query: str, max_results: int = 5) -> list[Chunk]:
        """Search the knowledge base for relevant chunks."""
        query_words = set(query.lower().split())
        scored: list[tuple[float, Chunk]] = []

        for chunk_file in self.chunks_dir.glob("*.json"):
            try:
                data = json.loads(chunk_file.read_text())
                chunk = Chunk(**data)

                # Simple TF-IDF-like scoring
                content_lower = chunk.content.lower()
                score = sum(
                    content_lower.count(word) for word in query_words if len(word) > 2
                )
                if score > 0:
                    scored.append((score, chunk))
            except Exception:
                continue

        scored.sort(key=lambda x: x[0], reverse=True)
        return [chunk for _, chunk in scored[:max_results]]

    def get_context(self, query: str, max_chunks: int = 5) -> str:
        """Get relevant context from the knowledge base for RAG."""
        chunks = self.search(query, max_results=max_chunks)
        if not chunks:
            return ""

        parts = ["<knowledge_base>", "Relevant information from your documents:"]
        for chunk in chunks:
            source = chunk.metadata.get("filename", "unknown")
            parts.append(f"\n[Source: {source}, chunk {chunk.chunk_index}]")
            parts.append(chunk.content)
        parts.append("</knowledge_base>")

        return "\n".join(parts)

    def list_documents(self) -> list[Document]:
        return list(self._docs)

    def remove_document(self, doc_id: str) -> bool:
        """Remove a document and its chunks."""
        found = False
        for d in self._docs:
            if d.id == doc_id or d.id.startswith(doc_id):
                # Remove chunk files
                for chunk_file in self.chunks_dir.glob(f"{d.id}_*.json"):
                    chunk_file.unlink()
                found = True
                self._docs = [x for x in self._docs if x.id != d.id]
                break
        if found:
            self._save_docs()
        return found

    def _chunk_text(self, text: str, chunk_size: int = 1000, overlap: int = 200) -> list[str]:
        """Split text into overlapping chunks."""
        words = text.split()
        chunks = []
        i = 0
        while i < len(words):
            chunk = " ".join(words[i:i + chunk_size])
            chunks.append(chunk)
            i += chunk_size - overlap
        return chunks if chunks else [text]

    def _extract_pdf(self, path: Path) -> str:
        """Extract text from PDF. Uses pdftotext if available."""
        import subprocess
        try:
            result = subprocess.run(
                ["pdftotext", str(path), "-"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                return result.stdout
        except FileNotFoundError:
            pass

        # Fallback: try reading as text (won't work for most PDFs but handles text-based ones)
        try:
            return path.read_text(errors="replace")
        except Exception:
            return f"[Could not extract text from {path.name}. Install pdftotext: apt install poppler-utils]"


# Singleton
_kb: KnowledgeBase | None = None

def get_knowledge_base() -> KnowledgeBase:
    global _kb
    if _kb is None:
        _kb = KnowledgeBase()
    return _kb
