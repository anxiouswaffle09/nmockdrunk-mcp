"""DocIndex + DocStore: CRUD, search scoring, and byte-range content reads."""

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..parser.sections import Section

INDEX_VERSION = 1


def _file_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclass
class DocIndex:
    """Index for a repository's documentation."""
    repo: str
    owner: str
    name: str
    indexed_at: str
    doc_paths: list
    doc_types: dict        # {".md": 5, ".txt": 2}
    sections: list         # Serialized Section dicts (without content by default)
    index_version: int = INDEX_VERSION
    file_hashes: dict = field(default_factory=dict)

    def get_section(self, section_id: str) -> Optional[dict]:
        """Find a section dict by ID."""
        for sec in self.sections:
            if sec.get("id") == section_id:
                return sec
        return None

    def search(self, query: str, doc_path: Optional[str] = None, max_results: int = 10) -> list:
        """Search sections with weighted scoring.

        Scoring weights:
          title exact match:    +20
          title substring:      +10
          title word overlap:   +5 per word
          summary match:        +8 (substring), +2 per word
          tag match:            +3 per matching tag
          content word match:   +1 per word (capped to avoid noise)

        Returns sections sorted by score descending, with content excluded.
        """
        query_lower = query.lower()
        query_words = set(query_lower.split())
        scored = []

        for sec in self.sections:
            if doc_path and sec.get("doc_path") != doc_path:
                continue

            score = self._score_section(sec, query_lower, query_words)
            if score > 0:
                scored.append((score, sec))

        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for _, sec in scored[:max_results]:
            summary_sec = {k: v for k, v in sec.items() if k != "content"}
            results.append(summary_sec)
        return results

    def _score_section(self, sec: dict, query_lower: str, query_words: set) -> int:
        score = 0

        title_lower = sec.get("title", "").lower()
        if query_lower == title_lower:
            score += 20
        elif query_lower in title_lower:
            score += 10
        for word in query_words:
            if word in title_lower:
                score += 5

        summary_lower = sec.get("summary", "").lower()
        if query_lower in summary_lower:
            score += 8
        for word in query_words:
            if word in summary_lower:
                score += 2

        tags = sec.get("tags", [])
        for tag in tags:
            if tag.lower() in query_words:
                score += 3

        content_lower = sec.get("content", "").lower()
        word_hits = sum(1 for w in query_words if w in content_lower)
        score += min(word_hits, 5)

        return score


class DocStore:
    """Storage for doc indexes with byte-offset content retrieval."""

    def __init__(self, base_path: Optional[str] = None):
        if base_path:
            self.base_path = Path(base_path)
        else:
            self.base_path = Path.home() / ".doc-index"
        self.base_path.mkdir(parents=True, exist_ok=True)

    def _safe_repo_component(self, value: str, field_name: str) -> str:
        import re
        if not value or value in {".", ".."}:
            raise ValueError(f"Invalid {field_name}: {value!r}")
        if "/" in value or "\\" in value:
            raise ValueError(f"Invalid {field_name}: {value!r}")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", value):
            raise ValueError(f"Invalid {field_name}: {value!r}")
        return value

    def _index_path(self, owner: str, name: str) -> Path:
        o = self._safe_repo_component(owner, "owner")
        n = self._safe_repo_component(name, "name")
        return self.base_path / o / f"{n}.json"

    def _content_dir(self, owner: str, name: str) -> Path:
        o = self._safe_repo_component(owner, "owner")
        n = self._safe_repo_component(name, "name")
        return self.base_path / o / n

    def _safe_content_path(self, content_dir: Path, relative_path: str) -> Optional[Path]:
        try:
            base = content_dir.resolve()
            candidate = (content_dir / relative_path).resolve()
            if os.path.commonpath([str(base), str(candidate)]) != str(base):
                return None
            return candidate
        except (OSError, ValueError):
            return None

    def save_index(
        self,
        owner: str,
        name: str,
        sections: list,         # list[Section]
        raw_files: dict,        # {doc_path: content}
        doc_types: dict,        # {".md": N}
        file_hashes: Optional[dict] = None,
    ) -> "DocIndex":
        """Save index and raw files to storage atomically."""
        if file_hashes is None:
            file_hashes = {fp: _file_hash(c) for fp, c in raw_files.items()}

        doc_paths = sorted(raw_files.keys())

        index = DocIndex(
            repo=f"{owner}/{name}",
            owner=owner,
            name=name,
            indexed_at=datetime.now().isoformat(),
            doc_paths=doc_paths,
            doc_types=doc_types,
            sections=[s.to_dict() for s in sections],
            index_version=INDEX_VERSION,
            file_hashes=file_hashes,
        )

        index_path = self._index_path(owner, name)
        index_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = index_path.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self._index_to_dict(index), f, indent=2)
        tmp_path.replace(index_path)

        # Cache raw files for byte-range reads
        content_dir = self._content_dir(owner, name)
        content_dir.mkdir(parents=True, exist_ok=True)

        for doc_path, content in raw_files.items():
            dest = self._safe_content_path(content_dir, doc_path)
            if not dest:
                raise ValueError(f"Unsafe doc path in raw_files: {doc_path}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                f.write(content.encode("utf-8"))

        return index

    def load_index(self, owner: str, name: str) -> Optional[DocIndex]:
        """Load index from storage."""
        index_path = self._index_path(owner, name)
        if not index_path.exists():
            return None

        with open(index_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        stored_version = data.get("index_version", 1)
        if stored_version > INDEX_VERSION:
            return None

        return DocIndex(
            repo=data["repo"],
            owner=data["owner"],
            name=data["name"],
            indexed_at=data["indexed_at"],
            doc_paths=data["doc_paths"],
            doc_types=data["doc_types"],
            sections=data["sections"],
            index_version=stored_version,
            file_hashes=data.get("file_hashes", {}),
        )

    def get_section_content(self, owner: str, name: str, section_id: str) -> Optional[str]:
        """Read section content using stored byte offsets. O(1) — no re-parsing."""
        index = self.load_index(owner, name)
        if not index:
            return None

        section = index.get_section(section_id)
        if not section:
            return None

        doc_path = section.get("doc_path", "")
        byte_start = section.get("byte_start", 0)
        byte_end = section.get("byte_end", 0)

        file_path = self._safe_content_path(self._content_dir(owner, name), doc_path)
        if not file_path or not file_path.exists():
            return None

        with open(file_path, "rb") as f:
            f.seek(byte_start)
            raw = f.read(byte_end - byte_start)

        return raw.decode("utf-8", errors="replace")

    def list_repos(self) -> list:
        """List all indexed doc sets."""
        repos = []
        for index_file in self.base_path.glob("*/*.json"):
            if index_file.name.startswith("_"):
                continue
            try:
                with open(index_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                repos.append({
                    "repo": data["repo"],
                    "indexed_at": data["indexed_at"],
                    "section_count": len(data["sections"]),
                    "doc_count": len(data["doc_paths"]),
                    "doc_types": data["doc_types"],
                    "index_version": data.get("index_version", 1),
                })
            except Exception:
                continue
        return repos

    def delete_index(self, owner: str, name: str) -> bool:
        """Delete an index and its raw content cache."""
        index_path = self._index_path(owner, name)
        content_dir = self._content_dir(owner, name)

        deleted = False
        if index_path.exists():
            index_path.unlink()
            deleted = True
        if content_dir.exists():
            shutil.rmtree(content_dir)
            deleted = True
        return deleted

    def _index_to_dict(self, index: DocIndex) -> dict:
        return {
            "repo": index.repo,
            "owner": index.owner,
            "name": index.name,
            "indexed_at": index.indexed_at,
            "doc_paths": index.doc_paths,
            "doc_types": index.doc_types,
            "sections": index.sections,
            "index_version": index.index_version,
            "file_hashes": index.file_hashes,
        }

    def _resolve_repo(self, repo: str) -> tuple:
        """Resolve a 'owner/name' or bare 'name' string.

        Returns (owner, name). For bare names without a slash, tries to find
        a matching index file using glob.
        """
        if "/" in repo:
            parts = repo.split("/", 1)
            return parts[0], parts[1]

        # Try to find by name glob — sanitize first to prevent glob injection
        try:
            repo = self._safe_repo_component(repo, "repo")
        except ValueError:
            return "local", repo
        matches = list(self.base_path.glob(f"*/{repo}.json"))
        if len(matches) == 1:
            owner = matches[0].parent.name
            return owner, repo

        # Default to local/name
        return "local", repo
