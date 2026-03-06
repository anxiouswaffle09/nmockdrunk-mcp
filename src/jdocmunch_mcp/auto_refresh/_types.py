"""Shared types for auto-refresh."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ChangeSet:
    modified: set = field(default_factory=set)
    deleted: set = field(default_factory=set)
    new_commit: Optional[str] = None
