"""MailBuddy — local web app to clean up and organize Gmail."""

import os
from datetime import datetime

from fastapi import FastAPI, Body, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import auth, db, gmail, sync

ROOT = os.path.dirname(os.path.dirname(__file__))
FRONTEND = os.path.join(ROOT, "frontend")

app = FastAPI(title="MailBuddy")

db.init_db()


# --- Auth -----------------------------------------------------------------

@app.get("/api/status")
def status():
    return {
        "credentials_present": auth.credentials_present(),
        "authorized": auth.is_authorized(),
        "cached_messages": db.total_count(),
        "sync": sync.status(),
        "sync_complete": db.get_meta("sync_complete") == "1",
    }


@app.get("/api/auth/login")
def login():
    if not auth.credentials_present():
        return JSONResponse(
            {"error": "Missing credentials.json. See the README setup steps."},
            status_code=400,
        )
    return RedirectResponse(auth.auth_url())


@app.get("/api/auth/callback")
def callback(request: Request):
    try:
        auth.finish_auth(str(request.url))
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(f"<h3>Authorization failed</h3><pre>{e}</pre>", 400)
    return RedirectResponse("/")


# --- Sync -----------------------------------------------------------------

@app.post("/api/sync")
def start_sync(full: bool = False):
    if not auth.is_authorized():
        return JSONResponse({"error": "Not authorized"}, status_code=401)
    started = sync.start(full=full)
    return {"started": started, "sync": sync.status()}


@app.get("/api/sync")
def sync_status():
    return sync.status()


# --- Senders dashboard ----------------------------------------------------

@app.get("/api/senders")
def senders(search: str = "", sort: str = "count", limit: int = 500,
            after: str = "", before: str = "", include_hidden: bool = False):
    after_ts, before_ts = _parse_date_range(after, before)
    return {
        "senders": db.sender_summary(
            search or None, sort, limit, after_ts, before_ts, include_hidden
        ),
        "total": db.total_count(),
        "hidden_count": db.hidden_count(),
    }


@app.post("/api/senders/hide")
def hide_sender(payload: dict = Body(...)):
    email = (payload.get("from_email") or "").lower().strip()
    if not email:
        return JSONResponse({"error": "from_email required"}, 400)
    db.hide_sender(email, payload.get("name"))
    return {"ok": True}


@app.post("/api/senders/unhide")
def unhide_sender(payload: dict = Body(...)):
    email = (payload.get("from_email") or "").lower().strip()
    if not email:
        return JSONResponse({"error": "from_email required"}, 400)
    db.unhide_sender(email)
    return {"ok": True}


def _parse_date_range(after, before):
    """Parse 'YYYY-MM-DD' query params into unix-second bounds (inclusive)."""
    after_ts = None
    before_ts = None
    if after:
        after_ts = int(datetime.strptime(after, "%Y-%m-%d").timestamp())
    if before:
        # Include the whole end day.
        before_ts = int(datetime.strptime(before, "%Y-%m-%d").timestamp()) + 86399
    return after_ts, before_ts


@app.post("/api/senders/trash")
def trash_sender(payload: dict = Body(...)):
    """Trash all mail from a sender (or subject), optionally unsubscribing first."""
    if not auth.is_authorized():
        return JSONResponse({"error": "Not authorized"}, status_code=401)

    from_email = payload.get("from_email")
    subject = payload.get("subject")
    do_unsub = bool(payload.get("unsubscribe"))
    permanent = bool(payload.get("permanent"))
    after_ts, before_ts = _parse_date_range(payload.get("after", ""), payload.get("before", ""))

    result = {"unsubscribed": None}

    if do_unsub and from_email:
        result["unsubscribed"] = _do_unsubscribe(from_email)

    if from_email:
        ids = db.message_ids_for_sender(from_email, after_ts=after_ts, before_ts=before_ts)
    elif subject:
        ids = db.message_ids_for_subject(subject)
    else:
        return JSONResponse({"error": "from_email or subject required"}, 400)

    if not ids:
        return {"trashed": 0, **result}

    if permanent:
        gmail.delete_messages_permanent(ids)
    else:
        gmail.trash_messages(ids)
    db.delete_messages(ids)  # drop from cache to keep dashboard accurate

    return {"trashed": len(ids), "permanent": permanent, **result}


@app.get("/api/senders/unsubscribe-info")
def unsubscribe_info(from_email: str):
    raw = db.unsub_for_sender(from_email)
    return {"raw": raw, "targets": gmail.parse_unsubscribe(raw)}


@app.post("/api/senders/unsubscribe")
def unsubscribe(payload: dict = Body(...)):
    if not auth.is_authorized():
        return JSONResponse({"error": "Not authorized"}, status_code=401)
    return _do_unsubscribe(payload.get("from_email"))


def _do_unsubscribe(from_email):
    raw = db.unsub_for_sender(from_email)
    targets = gmail.parse_unsubscribe(raw)
    out = {"attempted": bool(targets), "http_ok": None, "mailto": targets.get("mailto"),
           "http_url": targets.get("http")}
    if targets.get("http"):
        out["http_ok"] = gmail.try_http_unsubscribe(targets["http"])
    return out


# --- Labels (folders) & rules ---------------------------------------------

@app.get("/api/labels")
def labels():
    if not auth.is_authorized():
        return JSONResponse({"error": "Not authorized"}, status_code=401)
    return {"labels": gmail.list_labels()}


@app.post("/api/labels")
def create_label(payload: dict = Body(...)):
    if not auth.is_authorized():
        return JSONResponse({"error": "Not authorized"}, status_code=401)
    name = (payload.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name required"}, 400)
    return {"id": gmail.get_or_create_label(name), "name": name}


@app.post("/api/senders/move")
def move_sender(payload: dict = Body(...)):
    """Move all of a sender's mail into a label (folder), out of the inbox."""
    if not auth.is_authorized():
        return JSONResponse({"error": "Not authorized"}, status_code=401)
    from_email = payload.get("from_email")
    label_name = (payload.get("label") or "").strip()
    make_rule = bool(payload.get("make_rule"))
    archive = payload.get("archive", True)
    if not from_email or not label_name:
        return JSONResponse({"error": "from_email and label required"}, 400)

    after_ts, before_ts = _parse_date_range(payload.get("after", ""), payload.get("before", ""))
    label_id = gmail.get_or_create_label(label_name)
    ids = db.message_ids_for_sender(from_email, after_ts=after_ts, before_ts=before_ts)
    remove = ["INBOX"] if archive else []
    gmail.modify_labels(ids, add=[label_id], remove=remove)

    rule = None
    if make_rule:
        rule = gmail.create_filter_from_sender(from_email, label_id, archive=archive)

    # Drop the moved messages from the local cache so this sender leaves the
    # triage list (or its count shrinks if only a date-filtered subset moved).
    db.delete_messages(ids)

    return {"moved": len(ids), "label_id": label_id, "rule_created": bool(rule)}


# --- VIPs -----------------------------------------------------------------

@app.get("/api/vips")
def vips():
    return {"vips": db.list_vips()}


@app.post("/api/vips")
def add_vip(payload: dict = Body(...)):
    email = (payload.get("from_email") or "").lower().strip()
    if not email:
        return JSONResponse({"error": "from_email required"}, 400)
    db.add_vip(email, payload.get("name"))
    return {"ok": True}


@app.delete("/api/vips")
def remove_vip(from_email: str):
    db.remove_vip(from_email.lower().strip())
    return {"ok": True}


@app.get("/api/vips/new")
def vip_new():
    """New (unread) mail from VIP senders, newest first — from the live mailbox."""
    if not auth.is_authorized():
        return JSONResponse({"error": "Not authorized"}, status_code=401)
    vip_list = db.list_vips()
    if not vip_list:
        return {"messages": [], "vips": 0}
    from_clause = " OR ".join(f"from:{v['email']}" for v in vip_list)
    query = f"is:unread ({from_clause})"
    ids, _tok, _est = gmail.list_message_ids(query=query, page_size=50)
    msgs = gmail.fetch_metadata(ids) if ids else []
    msgs.sort(key=lambda m: m.get("date_ts", 0), reverse=True)
    return {"messages": msgs, "vips": len(vip_list)}


# --- Frontend (mounted last so /api takes priority) -----------------------

if os.path.isdir(FRONTEND):
    app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
