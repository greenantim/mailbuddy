"""Thin wrapper around the Gmail API plus the bulk operations MailBuddy needs."""

import random
import re
import time
import urllib.request
from email.utils import parseaddr, parsedate_to_datetime

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from . import auth

# Gmail caps batch modify/delete at 1000 ids per call.
BATCH_MODIFY_LIMIT = 1000
# Metadata fetch tuning. A metadata get costs 5 quota units and Gmail allows
# ~250 units/user/second, i.e. ~50 message fetches/second. We send larger HTTP
# batches (fewer round-trips) but pace to TARGET_MSGS_PER_SEC so we run just
# under the ceiling instead of overshooting, getting throttled, and stalling.
METADATA_BATCH = 100
TARGET_MSGS_PER_SEC = 45

_service = None


def _is_rate_limit(exc):
    if not isinstance(exc, HttpError):
        return False
    if exc.resp.status not in (403, 429):
        return False
    body = (exc.content or b"").decode("utf-8", "ignore").lower()
    return "ratelimitexceeded" in body or "userratelimitexceeded" in body or (
        "quota exceeded" in body
    )


def _execute(request, max_retries=7):
    """Execute an API request, retrying on rate-limit errors with backoff."""
    delay = 2.0
    for attempt in range(max_retries):
        try:
            return request.execute()
        except HttpError as e:
            if _is_rate_limit(e) and attempt < max_retries - 1:
                time.sleep(delay + random.random())
                delay = min(delay * 2, 64)
                continue
            raise


def service():
    global _service
    creds = auth.load_credentials()
    if creds is None:
        raise RuntimeError("Not authorized")
    # Rebuild if token rotated.
    _service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return _service


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


# --- Listing & metadata ---------------------------------------------------

def list_message_ids(page_token=None, query=None, page_size=500):
    """One page of message ids. Returns (ids, next_page_token, estimate)."""
    svc = service()
    params = {"userId": "me", "maxResults": page_size}
    if page_token:
        params["pageToken"] = page_token
    if query:
        params["q"] = query
    resp = _execute(svc.users().messages().list(**params))
    ids = [m["id"] for m in resp.get("messages", [])]
    return ids, resp.get("nextPageToken"), resp.get("resultSizeEstimate")


def _parse_message(msg):
    headers = {
        h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])
    }
    name, email = parseaddr(headers.get("from", ""))
    email = (email or "").lower().strip()
    subject = headers.get("subject", "")
    date_ts = 0
    if "internaldate" in msg:
        try:
            date_ts = int(msg["internalDate"]) // 1000
        except (ValueError, TypeError):
            date_ts = 0
    if not date_ts and headers.get("date"):
        try:
            date_ts = int(parsedate_to_datetime(headers["date"]).timestamp())
        except Exception:
            date_ts = 0
    return {
        "id": msg["id"],
        "thread_id": msg.get("threadId"),
        "from_email": email,
        "from_name": (name or "").strip(),
        "subject": subject,
        "date_ts": date_ts,
        "label_ids": ",".join(msg.get("labelIds", [])),
        "list_unsub": headers.get("list-unsubscribe", ""),
    }


def _fetch_batch(svc, chunk):
    """Fetch one batch of message metadata. Returns (parsed_rows, rate_limited_ids).

    Individual sub-requests that hit a rate limit are reported back so the
    caller can retry just those ids; other failures (e.g. a message deleted in
    the meantime) are skipped.
    """
    results = []
    retry_ids = []

    def _cb(request_id, response, exception):
        if exception is not None:
            if _is_rate_limit(exception):
                retry_ids.append(request_id)  # request_id == message id (set below)
            return
        results.append(_parse_message(response))

    batch = svc.new_batch_http_request(callback=_cb)
    for mid in chunk:
        batch.add(
            svc.users().messages().get(
                userId="me",
                id=mid,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date", "List-Unsubscribe"],
            ),
            request_id=mid,
        )
    _execute(batch)  # whole-batch rate limits are retried with backoff here
    return results, retry_ids


def fetch_metadata(ids):
    """Fetch metadata for many ids, retrying throttled ids and pacing to a
    target rate so we stay just under Gmail's per-user quota."""
    svc = service()
    results = []
    for chunk in _chunks(ids, METADATA_BATCH):
        start = time.monotonic()
        pending = list(chunk)
        backoff = 2.0
        while pending:
            rows, retry_ids = _fetch_batch(svc, pending)
            results.extend(rows)
            if retry_ids:
                time.sleep(backoff + random.random())
                backoff = min(backoff * 2, 64)
            pending = retry_ids
        # Pace to the target rate: only sleep if the fetch itself was faster
        # than our quota budget for this many messages.
        budget = len(chunk) / TARGET_MSGS_PER_SEC
        elapsed = time.monotonic() - start
        if elapsed < budget:
            time.sleep(budget - elapsed)
    return results


def profile_total():
    return _execute(service().users().getProfile(userId="me")).get("messagesTotal", 0)


# --- Bulk actions ---------------------------------------------------------

def trash_messages(ids):
    """Move messages to Trash (recoverable for 30 days). Batched."""
    svc = service()
    for chunk in _chunks(ids, BATCH_MODIFY_LIMIT):
        _execute(svc.users().messages().batchModify(
            userId="me", body={"ids": chunk, "addLabelIds": ["TRASH"],
                                "removeLabelIds": ["INBOX", "UNREAD"]}
        ))


def delete_messages_permanent(ids):
    """Permanently delete. Irreversible. Requires full mail scope."""
    svc = service()
    for chunk in _chunks(ids, BATCH_MODIFY_LIMIT):
        _execute(svc.users().messages().batchDelete(userId="me", body={"ids": chunk}))


def modify_labels(ids, add=None, remove=None):
    svc = service()
    body = {}
    if add:
        body["addLabelIds"] = add
    if remove:
        body["removeLabelIds"] = remove
    for chunk in _chunks(ids, BATCH_MODIFY_LIMIT):
        _execute(svc.users().messages().batchModify(
            userId="me", body={"ids": chunk, **body}
        ))


# --- Labels (folders) -----------------------------------------------------

def list_labels():
    svc = service()
    labels = _execute(svc.users().labels().list(userId="me")).get("labels", [])
    # Return user-created labels first, but include all so we can resolve ids.
    return [
        {"id": l["id"], "name": l["name"], "type": l.get("type")}
        for l in labels
    ]


def get_or_create_label(name):
    for l in list_labels():
        if l["name"].lower() == name.lower():
            return l["id"]
    created = _execute(service().users().labels().create(
        userId="me",
        body={
            "name": name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        },
    ))
    return created["id"]


# --- Filters (auto-rules) -------------------------------------------------

def create_filter_from_sender(from_email, label_id, archive=True):
    """Create a Gmail filter: future mail from sender -> label (+ skip inbox)."""
    svc = service()
    add_labels = [label_id]
    remove_labels = ["INBOX"] if archive else []
    body = {
        "criteria": {"from": from_email},
        "action": {"addLabelIds": add_labels, "removeLabelIds": remove_labels},
    }
    return _execute(svc.users().settings().filters().create(userId="me", body=body))


# --- Unsubscribe ----------------------------------------------------------

_URL_RE = re.compile(r"<(https?://[^>]+)>")
_MAILTO_RE = re.compile(r"<mailto:([^>]+)>")


def parse_unsubscribe(header_value):
    """Return dict with optional 'http' and 'mailto' unsubscribe targets."""
    if not header_value:
        return {}
    out = {}
    m = _URL_RE.search(header_value)
    if m:
        out["http"] = m.group(1)
    m = _MAILTO_RE.search(header_value)
    if m:
        out["mailto"] = m.group(1)
    return out


def try_http_unsubscribe(url, timeout=10):
    """Best-effort one-click unsubscribe via HTTP POST (RFC 8058)."""
    try:
        req = urllib.request.Request(
            url,
            data=b"List-Unsubscribe=One-Click",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except Exception:
        # Fall back to a GET — many providers accept a plain visit.
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return 200 <= resp.status < 400
        except Exception:
            return False
