"""Background sync: pull message metadata into the local cache.

Designed for tens of thousands of messages:
  * Pages through message ids and fetches only headers (no bodies).
  * Resumable via stored page tokens.
  * Reports progress (including which phase) so the UI can show live status.

On a first sync, the Inbox is synced first (usually a small fraction of a
big mailbox) so the sender dashboard becomes useful within a minute or two,
then the rest of the mailbox backfills automatically in the background.
"""

import threading
import time
from datetime import datetime, timedelta

from . import db, gmail

_state = {
    "running": False,
    "done": False,
    "phase": None,  # "inbox" | "all" | "incremental" | None
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


def _set(**kw):
    with _lock:
        _state.update(**kw)


def _page_through(query, token_meta_key, phase):
    """Page through message ids matching `query`, fetching metadata for each
    page and upserting into the cache. Resumable via a stored page token."""
    _set(phase=phase)
    page_token = db.get_meta(token_meta_key) or None
    while True:
        ids, next_token, _est = gmail.list_message_ids(page_token=page_token, query=query)
        if ids:
            rows = gmail.fetch_metadata(ids)
            if rows:
                db.upsert_messages(rows)
            _set(fetched=db.total_count())
        if next_token:
            db.set_meta(token_meta_key, next_token)
            page_token = next_token
        else:
            db.set_meta(token_meta_key, "")
            return


def _run(full=False):
    try:
        _set(running=True, done=False, error=None, phase=None,
             fetched=db.total_count(), started_at=time.time())
        try:
            _set(total=gmail.profile_total())
        except Exception:
            _set(total=0)

        try:
            db.set_labels(gmail.list_labels())
        except Exception:
            pass

        if full:
            # Full re-fetch of everything, from scratch.
            db.set_meta("sync_inbox_complete", "")
            db.set_meta("sync_complete", "")
            _page_through(None, "sync_page_token", "all")
            db.set_meta("sync_inbox_complete", "1")
            db.set_meta("sync_complete", "1")

        elif db.get_meta("sync_complete") == "1":
            # Steady state: only fetch mail newer than what we have, via a
            # Gmail date query. Day-granular, so back up a day to avoid gaps.
            newest = db.max_date_ts()
            query = None
            if newest:
                day = datetime.utcfromtimestamp(newest) - timedelta(days=1)
                query = f"after:{day.strftime('%Y/%m/%d')}"
            _page_through(query, "sync_incremental_token", "incremental")

        else:
            # First sync (or a previously interrupted one). Inbox first so
            # the dashboard is useful quickly, then the rest of the mailbox.
            if db.get_meta("sync_inbox_complete") != "1":
                _page_through("in:inbox", "sync_inbox_page_token", "inbox")
                db.set_meta("sync_inbox_complete", "1")

            _page_through(None, "sync_page_token", "all")
            db.set_meta("sync_complete", "1")

        _set(running=False, done=True, phase=None)
    except Exception as e:  # noqa: BLE001
        _set(running=False, error=str(e), phase=None)


def start(full=False):
    global _thread
    with _lock:
        if _state["running"]:
            return False
    _thread = threading.Thread(target=_run, kwargs={"full": full}, daemon=True)
    _thread.start()
    return True
