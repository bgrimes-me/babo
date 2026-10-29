"""Search over the town's knowledge: SQLite FTS5 with BM25, no embedding model.

What is indexed: ``commons/knowledge``, ``commons/skills`` and every resident's
own ``knowledge/``. Never sessions, inboxes or checkpoints.

Every document has an owner: ``commons`` or a resident id. A search always
names the caller, and only ever returns the caller's own notes and the
commons. Paraphrase is handled at write time: aliases and keywords are indexed
in their own columns and weighted above the body.

The index is derived data stored outside the repo; ``rebuild`` recreates it
from the files at any time.

Upgrade path (not built): add a ``VectorBackend`` storing embeddings in the
same database (e.g. sqlite-vec), and a ``HybridBackend`` that queries both and
merges the two rankings with reciprocal rank fusion. Callers use
``SearchBackend`` only, so the tools don't change.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from nanobot.baton.notes import Note
from nanobot.baton.town import Town

# Column weights for bm25(): path, owner, kind, status are UNINDEXED.
_WEIGHTS = {"title": 4.0, "aliases": 6.0, "keywords": 5.0, "body": 1.0}
_TOKEN_RE = re.compile(r"[\w][\w'-]*", re.UNICODE)
_STOPWORDS = frozenset(
    "a an and are as at be by for from how i in is it of on or that the this to was what when where which who why with you"
    .split()
)


@dataclass(frozen=True)
class Document:
    path: str  # relative to the town root
    owner: str  # "commons" or a resident id
    kind: str  # knowledge | skill
    title: str
    body: str
    aliases: str = ""
    keywords: str = ""
    status: str = "active"


@dataclass(frozen=True)
class Hit:
    path: str
    owner: str
    kind: str
    title: str
    snippet: str
    status: str
    score: float


class SearchBackend(Protocol):
    def upsert(self, doc: Document) -> None: ...
    def remove(self, path: str) -> None: ...
    def search(self, query: str, *, owners: Iterable[str], k: int = 3) -> list[Hit]: ...
    def rebuild(self, docs: Iterable[Document]) -> int: ...


def fts_query(text: str) -> str | None:
    """Turn free text into a safe FTS5 query: each word quoted, ORed, prefix-matched."""
    words = [w.lower() for w in _TOKEN_RE.findall(text)]
    terms = [w for w in words if w not in _STOPWORDS and len(w) > 1] or [w for w in words if len(w) > 1]
    if not terms:
        return None
    seen: list[str] = []
    for term in terms:
        term = term.replace('"', "")
        if term and term not in seen:
            seen.append(term)
    return " OR ".join(f'"{term}"*' for term in seen[:16])


class Fts5Search:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create()

    def _create(self) -> None:
        self._conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5("
            "path UNINDEXED, owner UNINDEXED, kind UNINDEXED, status UNINDEXED, "
            "title, aliases, keywords, body, tokenize='porter unicode61')"
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def upsert(self, doc: Document) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM docs WHERE path = ?", (doc.path,))
            self._conn.execute(
                "INSERT INTO docs(path, owner, kind, status, title, aliases, keywords, body) VALUES (?,?,?,?,?,?,?,?)",
                (doc.path, doc.owner, doc.kind, doc.status, doc.title, doc.aliases, doc.keywords, doc.body),
            )
            self._conn.commit()

    def remove(self, path: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM docs WHERE path = ?", (path,))
            self._conn.commit()

    def search(self, query: str, *, owners: Iterable[str], k: int = 3) -> list[Hit]:
        match = fts_query(query)
        owner_list = sorted(set(owners))
        if not match or not owner_list or k <= 0:
            return []
        placeholders = ",".join("?" for _ in owner_list)
        weights = ", ".join(str(w) for w in (0, 0, 0, 0, *_WEIGHTS.values()))
        sql = (
            f"SELECT path, owner, kind, title, status, snippet(docs, 7, '', '', '…', 16), bm25(docs, {weights}) AS score "
            f"FROM docs WHERE docs MATCH ? AND owner IN ({placeholders}) "
            # Superseded notes sink below active ones but stay findable.
            "ORDER BY (status = 'superseded'), score LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, (match, *owner_list, k)).fetchall()
        return [Hit(path=r[0], owner=r[1], kind=r[2], title=r[3], status=r[4], snippet=r[5], score=r[6]) for r in rows]

    def rebuild(self, docs: Iterable[Document]) -> int:
        with self._lock:
            self._conn.execute("DROP TABLE IF EXISTS docs")
            self._conn.commit()
        self._create()
        count = 0
        for doc in docs:
            self.upsert(doc)
            count += 1
        return count


# -- documents from the town ---------------------------------------------
def document_for(town: Town, path: Path) -> Document | None:
    """Build the index document for a file, or None when it isn't indexable."""
    try:
        rel = path.resolve().relative_to(town.root)
    except ValueError:
        return None
    parts = rel.parts
    if path.suffix != ".md" or not path.is_file():
        return None
    if parts[:2] == ("commons", "knowledge"):
        owner, kind = "commons", "knowledge"
    elif parts[:2] == ("commons", "skills") and path.name == "SKILL.md":
        owner, kind = "commons", "skill"
    elif len(parts) >= 3 and parts[0] == "residents" and parts[2] == "knowledge":
        owner, kind = parts[1], "knowledge"
    else:
        return None
    note = Note.load(path)
    title = note.title
    if kind == "skill":
        title = str(note.extra.get("name") or path.parent.name)
        desc = str(note.extra.get("description") or "")
        body = f"{desc}\n\n{note.body}"
    else:
        body = note.body
    return Document(
        path=str(rel),
        owner=owner,
        kind=kind,
        title=title,
        body=body,
        aliases=" ; ".join(note.aliases),
        keywords=" ; ".join(note.keywords),
        status=note.status,
    )


def town_documents(town: Town) -> Iterable[Document]:
    roots = [town.knowledge, town.skills]
    roots += [d / "knowledge" for d in sorted(town.residents_dir.glob("*")) if d.is_dir() and not d.name.startswith(".")]
    for root in roots:
        for path in sorted(root.rglob("*.md")):
            doc = document_for(town, path)
            if doc is not None:
                yield doc


_INDEXES: dict[Path, Fts5Search] = {}
_INDEX_LOCK = threading.Lock()


def open_index(db_path: Path) -> Fts5Search:
    db_path = db_path.expanduser().resolve()
    with _INDEX_LOCK:
        index = _INDEXES.get(db_path)
        if index is None:
            index = Fts5Search(db_path)
            _INDEXES[db_path] = index
        return index


def reindex(town: Town, db_path: Path) -> int:
    return open_index(db_path).rebuild(town_documents(town))


def index_file(town: Town, db_path: Path, path: Path) -> None:
    """Keep the index current after a write. Removes the entry if the file is gone."""
    index = open_index(db_path)
    try:
        rel = str(path.resolve().relative_to(town.root))
    except ValueError:
        return
    doc = document_for(town, path) if path.exists() else None
    if doc is None:
        index.remove(rel)
    else:
        index.upsert(doc)
