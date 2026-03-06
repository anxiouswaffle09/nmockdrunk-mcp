"""Background AI summarization thread for newly indexed sections."""

import json
import threading

from ..storage.doc_store import DocStore
from ..summarizer.batch_summarize import _create_summarizer, title_fallback

_lock = threading.Lock()


def queue_ai_summarization(
    owner: str,
    name: str,
    sections_needing_ai: list,
    store: DocStore,
) -> None:
    """Spawn a background daemon thread to run AI summarization.

    Non-blocking. If sections_needing_ai is empty, does nothing.
    The last writer wins on concurrent writes — no corruption, just occasional
    summary re-generation on the next change.
    """
    if not sections_needing_ai:
        return

    def _run():
        try:
            summarizer = _create_summarizer()
            if not summarizer:
                return

            # Clear summaries so batch_summarize processes them
            for sec in sections_needing_ai:
                sec.summary = ""
            summarizer.summarize_batch(sections_needing_ai)

            # Re-load current index (may have been updated since we started)
            current_index = store.load_index(owner, name)
            if not current_index:
                return

            # Merge updated summaries by section ID
            summary_map = {sec.id: sec.summary for sec in sections_needing_ai}
            for sec_dict in current_index.sections:
                sid = sec_dict.get("id")
                if sid in summary_map and summary_map[sid]:
                    sec_dict["summary"] = summary_map[sid]

            # Atomic write
            index_path = store._index_path(owner, name)
            tmp_path = index_path.with_suffix(".json.tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(store._index_to_dict(current_index), f, indent=2)
            tmp_path.replace(index_path)

        except Exception:
            pass  # Never crash the background thread

    t = threading.Thread(target=_run, daemon=True)
    t.start()
