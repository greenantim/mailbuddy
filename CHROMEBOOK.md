# Running MailBuddy on a Chromebook

MailBuddy is a small local Python web app. On a Chromebook you run it inside the
**Linux (Crostini)** environment. These are the full, beginner-friendly steps.

---

## 1. Turn on Linux

**Settings → Advanced → Developers → Linux development environment → Turn on.**

This installs a **Terminal** app and adds a **"Linux files"** area that shows up in
the **Files** app. It takes a few minutes the first time.

## 2. Get the project onto your Chromebook

Open the **Terminal** app and run:

```bash
git clone https://github.com/greenantim/mailbuddy.git
cd mailbuddy
git checkout claude/pensive-dirac-uap0u8
```

The project now lives at `~/mailbuddy`, visible in the **Files** app under
**"Linux files" → mailbuddy**.

> If `git` isn't installed: `sudo apt update && sudo apt install -y git`

## 3. Create your Google OAuth credentials

Follow the steps in the main [README](README.md#1-one-time-google-setup-5-10-min)
to create an OAuth **Web application** client in Google Cloud, with redirect URI:

```
http://localhost:8000/api/auth/callback
```

Download the client JSON. It lands in **Files → Downloads** with a long name like
`client_secret_…json`.

## 4. Put `credentials.json` in the project folder

1. In the **Files** app, **right-click the downloaded file → Rename** to
   `credentials.json`.
2. **Drag it** from **Downloads** into **Linux files → mailbuddy**.

> ⚠️ Files in plain **Downloads** are *not* visible to Linux apps. The file must
> be inside **Linux files → mailbuddy** for MailBuddy to find it.

## 5. Install and run

In the Terminal:

```bash
cd ~/mailbuddy
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn backend.main:app --port 8000
```

> If `python3`/`pip`/`venv` are missing:
> `sudo apt update && sudo apt install -y python3 python3-pip python3-venv`

Open **http://localhost:8000** in your Chromebook browser.

---

## Troubleshooting

### "Access blocked: MailBuddy has not completed the Google verification process" (Error 403: access_denied)
Your OAuth consent screen is in **Testing** mode and only approved accounts can
sign in. Add yourself as a test user:

1. **Google Cloud Console → APIs & Services → OAuth consent screen**
   (newer console: **APIs & Services → Branding → Audience** tab).
2. Under **Test users**, click **+ Add users**, enter your Gmail address
   (e.g. `greenantim@gmail.com`), and **Save**.
3. Refresh MailBuddy and click **Connect Gmail** again.

You do **not** need to publish the app or pass Google verification for personal
use. Note: in Testing mode the login refresh token expires after 7 days, so you
may need to click **Connect Gmail** again occasionally.

### "This app isn't verified" warning
Expected for your own personal client. Click **Advanced → Go to MailBuddy
(unsafe)** to continue — it's your own app talking to your own account.

### Port 8000 already in use
Run on another port and update the redirect URI in Google Cloud to match:
```bash
uvicorn backend.main:app --port 8080
```
(then use `http://localhost:8080/...` everywhere, including
`MAILBUDDY_REDIRECT`).

### `credentials.json` not found
Confirm it's named exactly `credentials.json` and sits in `~/mailbuddy`
(run `ls ~/mailbuddy` — you should see it listed).
