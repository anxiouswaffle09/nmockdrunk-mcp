"""AI summarization queue — disabled. Stub retained for import compatibility."""

from ..storage.doc_store import DocStore


def queue_ai_summarization(
    owner: str,
    name: str,
    sections_needing_ai: list,
    store: DocStore,
) -> None:
    """No-op: AI summarization has been removed."""
    pass
