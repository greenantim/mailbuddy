"""Background sync: pull message metadata into the local cache.

Designed for tens of thousands of messages:
  * Pages through message ids and fetches only headers (no bodies).
  * Resumable via a stored page token.
  * Reports progress so the UI can show a live count.
"""

import threading
import time

from . import db, gmail

_state = {
    "running": False,
    "done": False,
    "fetched": 0,
    "total": 0,
    "error": None,
    "started_at": None,
}
_lock = threading.Lock()
_thread = None


def status():
    with _lock:
        return dict(_state)


def _run(full=False):
    global _state
    try:
        with _lock:
            _state.update(
                running=True, done=False, error=None, fetched=db.total_count(),
                started_at=time.time(),
            )
        try:
            _state["total"] = gmail.profile_total()
        except Exception:
            _state["total"] = 0

        page_token = None if full else db.get_meta("sync_page_token")
        # If resuming a finished sync, start fresh from the top to catch new mail.
        if db.get_meta("sync_complete") == "1" and not full:
            page_token = None

        while True:
            ids, next_token, _est = gmail.list_message_ids(page_token=page_token)
            if ids:
                rows = gmail.fetch_metadata(ids)
                if rows:
                    db.upsert_messages(rows)
                with _lock:
                    _state["fetched"] = db.total_count()
            if next_token:
                db.set_meta("sync_page_token", next_token)
                page_token = next_token
            else:
                db.set_meta("sync_complete", "1")
                db.set_meta("sync_page_token", "")
                break

        with _lock:
            _state.update(running=False, done=True)
    except Exception as e:  # noqa: BLE001
        with _lock:
            _state.update(running=False, error=str(e))


def start(full=False):
    global _thread
    with _lock:
        if _state["running"]:
            return False
    _thread = threading.Thread(target=_run, kwargs={"full": full}, daemon=True)
    _thread.start()
    return True
