# 📬 MailBuddy

A small, **local** web app to take control of an overwhelmed Gmail inbox.

- **Sender dashboard** — see every sender ranked by how many emails they've sent you.
- **Bulk delete** — trash *all* mail from a sender (or matching a subject) in one click. Trash by default (recoverable 30 days); permanent delete is an explicit opt‑in.
- **Unsubscribe** — when you delete a sender, MailBuddy tries the standard one‑click `List‑Unsubscribe` so they stop emailing you.
- **Folders (labels)** — create folders and move a sender's mail into them, optionally archiving out of the Inbox.
- **Auto‑rules** — optionally create a Gmail filter so *future* mail from that sender is auto‑filed, even outside this app.
- **VIPs** — flag important senders and get a "new unread from VIPs" view.

Everything runs on your machine. Your emails and OAuth token never leave it — there's no server, no third party.

> **Scale note:** MailBuddy is built for tens of thousands of emails. It syncs lightweight *metadata only* (sender, subject, date — never message bodies) into a local SQLite cache so the dashboard is instant. The first sync of a very large mailbox can take a while; it's resumable and runs in the background.

---

## 1. One‑time Google setup (~5–10 min)

MailBuddy talks to Gmail through Google's official API, which needs an OAuth client you create (free) in your own Google Cloud project.

1. Go to the **[Google Cloud Console](https://console.cloud.google.com/)** and create a new project (e.g. "MailBuddy").
2. **Enable the Gmail API**: APIs & Services → Library → search "Gmail API" → **Enable**.
3. **OAuth consent screen**: APIs & Services → OAuth consent screen → choose **External** → fill in app name/email → on the *Test users* step, **add your own Gmail address**. (Leaving the app in "testing" is fine for personal use.)
4. **Create credentials**: APIs & Services → Credentials → **Create Credentials → OAuth client ID** → Application type **Web application**.
   - Under **Authorized redirect URIs**, add exactly:
     ```
     http://localhost:8000/api/auth/callback
     ```
5. **Download** the client JSON and save it in this project's root as:
   ```
   credentials.json
   ```

`credentials.json` and `token.json` are git‑ignored — never commit them.

### Scopes MailBuddy requests
| Scope | Why |
|---|---|
| `gmail.modify` | Read metadata, trash mail, add/remove labels |
| `gmail.labels` | Create & manage folders (labels) |
| `gmail.settings.basic` | Create filters (auto‑rules) |
| `https://mail.google.com/` | Only used for the optional **permanent delete** |

If you never want permanent deletion, remove the last scope from `backend/auth.py`.

---

## 2. Install & run

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

uvicorn backend.main:app --port 8000
```

Then open **http://localhost:8000**.

1. Click **Connect Gmail** and approve access (you'll see an "unverified app" warning because it's your own personal client — that's expected; continue).
2. Click **Sync mailbox**. Watch the counter climb. You can start using the dashboard as soon as data appears.
3. Use the **Senders** tab to delete/move/flag, and the **VIPs** tab for important mail.

---

## 3. How things map to Gmail

| In MailBuddy | In Gmail |
|---|---|
| Delete sender | Move all their mail to **Trash** (or permanent delete) |
| Folder | **Label** |
| Move to folder | Add label + remove from **Inbox** |
| Auto‑rule | A **filter** (`from:` → label, skip inbox) |
| VIP | Stored locally; "new VIP mail" = `is:unread from:…` search |

---

## 4. Project layout

```
backend/
  main.py    FastAPI app & routes
  auth.py    Google OAuth flow
  gmail.py   Gmail API wrapper + bulk actions, unsubscribe
  sync.py    Background, resumable metadata sync
  db.py      Local SQLite cache & sender aggregation
frontend/
  index.html, app.js, style.css
```

## 5. Safety & notes

- **Trash is the default.** Permanent delete requires ticking an extra box.
- Bulk actions always show a **confirmation with the exact count** first.
- The local cache is rebuilt by re‑syncing; deleting `mailbuddy.db` is harmless.
- Unsubscribe is best‑effort: one‑click works for senders that support RFC 8058; otherwise MailBuddy surfaces the unsubscribe link/`mailto`.

## 6. Roadmap ideas
- Multi‑select senders for batch delete
- Subject/topic clustering
- Size‑based cleanup (largest attachments)
- Scheduled auto‑sync
