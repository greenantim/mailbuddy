// MailBuddy frontend — talks to the local FastAPI backend.

const $ = (sel) => document.querySelector(sel);
const api = async (path, opts = {}) => {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
};

let SYNC_POLL = null;

function toast(msg, ms = 3000) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), ms);
}

function fmtDate(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

// --- Boot -----------------------------------------------------------------

async function refreshStatus() {
  const s = await api("/api/status");
  const pill = $("#status-pill");

  if (!s.credentials_present) {
    pill.textContent = "Setup needed"; pill.className = "pill warn";
    showSetup(`
      <p>MailBuddy needs a Google OAuth client to talk to Gmail. This is a
      one-time, free setup.</p>
      <ol>
        <li>Open the <a href="https://console.cloud.google.com/" target="_blank">Google Cloud Console</a> and create a project.</li>
        <li>Enable the <strong>Gmail API</strong>.</li>
        <li>Configure the OAuth consent screen (External), add yourself as a test user.</li>
        <li>Create an <strong>OAuth client ID</strong> of type <em>Web application</em> with redirect URI
            <code>http://localhost:8000/api/auth/callback</code>.</li>
        <li>Download the JSON, save it as <code>credentials.json</code> in the project root, and restart the app.</li>
      </ol>
      <p class="muted">Full details are in the README.</p>`);
    return s;
  }
  if (!s.authorized) {
    pill.textContent = "Not connected"; pill.className = "pill warn";
    showSetup(`<p>Credentials found. Connect your Gmail account to continue.</p>
      <button onclick="location.href='/api/auth/login'">Connect Gmail</button>`);
    return s;
  }

  pill.textContent = "Connected"; pill.className = "pill ok";
  $("#setup").classList.add("hidden");
  $("#sync-bar").classList.remove("hidden");
  $("#tabs").classList.remove("hidden");
  $("#tab-senders").classList.remove("hidden");

  renderSync(s);
  if (!s.sync_complete && !s.sync.running && s.cached_messages === 0) {
    // First run — nothing cached yet.
    $("#sync-detail").textContent = " · run a sync to get started";
  }
  return s;
}

function showSetup(html) {
  $("#setup").classList.remove("hidden");
  $("#setup-body").innerHTML = html;
  $("#sync-bar").classList.add("hidden");
  $("#tabs").classList.add("hidden");
  $("#tab-senders").classList.add("hidden");
  $("#tab-vips").classList.add("hidden");
}

function renderSync(s) {
  $("#sync-count").textContent = (s.cached_messages || 0).toLocaleString();
  const sy = s.sync || {};
  const prog = $("#sync-progress");
  if (sy.running) {
    prog.classList.remove("hidden");
    const pct = sy.total ? Math.min(100, Math.round((sy.fetched / sy.total) * 100)) : 0;
    prog.firstElementChild.style.width = pct + "%";
    const phaseLabel = {
      inbox: "syncing Inbox first…",
      all: "backfilling full mailbox…",
      incremental: "checking for new mail…",
    }[sy.phase] || "syncing…";
    $("#sync-detail").textContent = ` · ${phaseLabel} ${sy.fetched.toLocaleString()} / ${(sy.total||0).toLocaleString()}`;
    $("#btn-sync").disabled = true;
  } else {
    $("#btn-sync").disabled = false;
    prog.classList.add("hidden");
    if (sy.error) $("#sync-detail").textContent = " · error: " + sy.error;
  }
}

let lastPhase = null;
let pollTicks = 0;
async function pollSync() {
  const sy = await api("/api/sync");
  const s = { cached_messages: sy.fetched, sync: sy, sync_complete: sy.done };
  renderSync(s);
  if (sy.running) {
    pollTicks++;
    // Refresh the dashboard live as data streams in: immediately on a phase
    // change (inbox -> full-mailbox handoff), and periodically during long
    // phases so newly-discovered senders/counts show up.
    if (sy.phase !== lastPhase) {
      if (lastPhase === "inbox") toast("Inbox synced — refining as the rest of your mailbox loads…");
      lastPhase = sy.phase;
      loadSenders();
    } else if (pollTicks % 8 === 0) {
      loadSenders();
    }
  } else {
    clearInterval(SYNC_POLL); SYNC_POLL = null;
    lastPhase = null;
    pollTicks = 0;
    loadSenders();
    if (sy.done) toast("Sync complete");
  }
}

// --- Sync buttons ---------------------------------------------------------

$("#btn-sync").onclick = async () => {
  await api("/api/sync", { method: "POST" });
  startSyncPolling();
};
$("#btn-resync").onclick = async () => {
  await api("/api/sync?full=true", { method: "POST" });
  startSyncPolling();
};
function startSyncPolling() {
  if (SYNC_POLL) return;
  SYNC_POLL = setInterval(pollSync, 1500);
  pollSync();
}

// --- Tabs -----------------------------------------------------------------

document.querySelectorAll(".tab").forEach((t) => {
  t.onclick = () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    const tab = t.dataset.tab;
    $("#tab-senders").classList.toggle("hidden", tab !== "senders");
    $("#tab-vips").classList.toggle("hidden", tab !== "vips");
    if (tab === "vips") loadVips();
  };
});

// --- Senders --------------------------------------------------------------

let searchTimer = null;
$("#search").oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadSenders, 250); };
$("#sort").onchange = loadSenders;
$("#date-after").onchange = loadSenders;
$("#date-before").onchange = loadSenders;
$("#date-clear").onclick = () => {
  $("#date-after").value = "";
  $("#date-before").value = "";
  loadSenders();
};
$("#show-hidden").onchange = loadSenders;

// The current date range, included in delete/move requests so bulk actions
// only touch emails within the filtered range (when a range is set).
function dateRange() {
  return { after: $("#date-after").value, before: $("#date-before").value };
}

async function loadSenders() {
  const search = $("#search").value.trim();
  const sort = $("#sort").value;
  const { after, before } = dateRange();
  let url = `/api/senders?search=${encodeURIComponent(search)}&sort=${sort}`;
  if (after) url += `&after=${after}`;
  if (before) url += `&before=${before}`;
  const includeHidden = $("#show-hidden").checked;
  if (includeHidden) url += `&include_hidden=true`;
  const { senders, hidden_count } = await api(url);

  $("#hidden-count").textContent = hidden_count ? `(${hidden_count})` : "";
  const tbody = $("#senders-table tbody");
  tbody.innerHTML = "";
  $("#senders-empty").classList.toggle("hidden", senders.length > 0);

  for (const s of senders) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>
        <div class="sender-name">${escapeHtml(s.from_name)}
          ${s.is_vip ? '<span class="badge vip">VIP</span>' : ""}
          ${s.is_hidden ? '<span class="badge">hidden</span>' : ""}
          ${s.has_unsub ? '<span class="badge unsub" title="Click to see unsubscribe options">unsub</span>' : ""}
        </div>
        <div class="sender-email">${escapeHtml(s.from_email)}</div>
      </td>
      <td class="num">${s.count.toLocaleString()}</td>
      <td>${fmtDate(s.last_ts)}</td>
      <td></td>`;
    const actions = tr.lastElementChild;
    actions.append(
      btn("Delete", "danger small", () => openDeleteModal(s)),
      btn("Move", "ghost small", () => openMoveModal(s)),
      btn(s.is_vip ? "★" : "☆", "ghost small", () => toggleVip(s)),
      s.is_hidden
        ? btn("Unhide", "ghost small", () => unhideSender(s))
        : btn("Hide", "ghost small", () => hideSender(s)),
    );
    const unsubBadge = tr.querySelector(".badge.unsub");
    if (unsubBadge) unsubBadge.onclick = () => openUnsubModal(s);
    tbody.appendChild(tr);
  }
}

async function hideSender(s) {
  await api("/api/senders/hide", {
    method: "POST",
    body: JSON.stringify({ from_email: s.from_email, name: s.from_name }),
  });
  toast(`Hid ${s.from_name} — tick "Show hidden" to bring it back`);
  loadSenders();
}

async function unhideSender(s) {
  await api("/api/senders/unhide", {
    method: "POST",
    body: JSON.stringify({ from_email: s.from_email }),
  });
  toast(`Unhid ${s.from_name}`);
  loadSenders();
}

function btn(label, cls, onclick) {
  const b = document.createElement("button");
  b.textContent = label; b.className = "button " + cls; b.onclick = onclick;
  return b;
}

// --- Delete modal ---------------------------------------------------------

function openDeleteModal(s) {
  const { after, before } = dateRange();
  const rangeNote = (after || before)
    ? `<p class="muted">Date filter is active — only emails ${after ? `from ${after} ` : ""}${before ? `through ${before} ` : ""}will be deleted (the count above reflects this).</p>`
    : "";
  openModal({
    title: `Delete mail from ${s.from_name}`,
    bodyHtml: `
      <p>This will move <strong>${s.count.toLocaleString()}</strong> email(s) from
         <code>${escapeHtml(s.from_email)}</code> to Trash (recoverable for 30 days).</p>
      ${rangeNote}
      ${s.has_unsub ? `<label class="check"><input type="checkbox" id="opt-unsub" checked> Try to unsubscribe first</label>` : ""}
      <label class="check"><input type="checkbox" id="opt-perm"> Permanently delete instead (irreversible)</label>`,
    confirmLabel: "Delete",
    onConfirm: async () => {
      const unsubscribe = $("#opt-unsub")?.checked || false;
      const permanent = $("#opt-perm")?.checked || false;
      const r = await api("/api/senders/trash", {
        method: "POST",
        body: JSON.stringify({ from_email: s.from_email, unsubscribe, permanent, after, before }),
      });
      let msg = `${permanent ? "Deleted" : "Trashed"} ${r.trashed.toLocaleString()} email(s)`;
      if (r.unsubscribed?.attempted)
        msg += r.unsubscribed.http_ok ? " · unsubscribed" : " · unsubscribe link opened";
      toast(msg);
      loadSenders();
    },
  });
}

// --- Unsubscribe modal ------------------------------------------------------

async function openUnsubModal(s) {
  const { raw, targets } = await api(`/api/senders/unsubscribe-info?from_email=${encodeURIComponent(s.from_email)}`);

  let body = `<p>This sender includes a <code>List-Unsubscribe</code> header — the
    standard way bulk/marketing senders let mail clients unsubscribe you
    automatically, without opening their site.</p>`;

  if (targets.http) {
    body += `<p><strong>One-click link found:</strong><br>
      <code style="word-break:break-all">${escapeHtml(targets.http)}</code></p>
      <p class="muted">Clicking "Unsubscribe" below will send a request to this
      link on your behalf (the same thing Gmail's own "Unsubscribe" button does).
      It does <em>not</em> delete any emails.</p>`;
  }
  if (targets.mailto) {
    body += `<p><strong>Unsubscribe email address:</strong><br>
      <code>${escapeHtml(targets.mailto)}</code></p>
      <p class="muted">Some senders only support unsubscribing by sending an
      email to this address. MailBuddy can't send mail on your behalf, so
      you'd need to email this address yourself (often with the subject
      "unsubscribe").</p>`;
  }
  if (!targets.http && !targets.mailto) {
    body += `<p class="muted">No usable link was found in the header
      (<code>${escapeHtml(raw || "")}</code>). You may need to unsubscribe from
      inside one of their emails directly.</p>`;
  }

  openModal({
    title: `Unsubscribe from ${s.from_name}`,
    bodyHtml: body,
    confirmLabel: targets.http ? "Unsubscribe" : "Close",
    confirmClass: targets.http ? "" : "ghost",
    onConfirm: async () => {
      if (!targets.http) return; // just closes
      const r = await api("/api/senders/unsubscribe", {
        method: "POST",
        body: JSON.stringify({ from_email: s.from_email }),
      });
      toast(r.http_ok ? "Unsubscribe request sent" : "Sent, but couldn't confirm success — sender may take a few days");
    },
  });
}

// --- Move modal -----------------------------------------------------------

async function openMoveModal(s) {
  const { labels } = await api("/api/labels");
  const userLabels = labels.filter((l) => l.type === "user");
  const opts = userLabels.map((l) => `<option>${escapeHtml(l.name)}</option>`).join("");
  const { after, before } = dateRange();
  const rangeNote = (after || before)
    ? `<p class="muted">Date filter is active — only emails ${after ? `from ${after} ` : ""}${before ? `through ${before} ` : ""}will be moved.</p>`
    : "";
  openModal({
    title: `Move mail from ${s.from_name}`,
    bodyHtml: `
      <label>Folder (label)</label>
      <input id="move-label" list="label-list" placeholder="Type a new or existing folder" />
      <datalist id="label-list">${opts}</datalist>
      <label class="check"><input type="checkbox" id="move-archive" checked> Remove from Inbox (archive)</label>
      <label class="check"><input type="checkbox" id="move-rule"> Auto-create a rule for future mail</label>
      ${rangeNote}`,
    confirmLabel: "Move",
    confirmClass: "",
    onConfirm: async () => {
      const label = $("#move-label").value.trim();
      if (!label) { toast("Enter a folder name"); throw new Error("no label"); }
      const r = await api("/api/senders/move", {
        method: "POST",
        body: JSON.stringify({
          from_email: s.from_email, label,
          archive: $("#move-archive").checked,
          make_rule: $("#move-rule").checked,
          after, before,
        }),
      });
      toast(`Moved ${r.moved.toLocaleString()} email(s)${r.rule_created ? " · rule created" : ""}`);
      loadSenders();
    },
  });
}

// --- VIPs -----------------------------------------------------------------

async function toggleVip(s) {
  if (s.is_vip) {
    await api(`/api/vips?from_email=${encodeURIComponent(s.from_email)}`, { method: "DELETE" });
    toast("Removed from VIPs");
  } else {
    await api("/api/vips", {
      method: "POST",
      body: JSON.stringify({ from_email: s.from_email, name: s.from_name }),
    });
    toast("Added to VIPs");
  }
  loadSenders();
}

async function loadVips() {
  const { vips } = await api("/api/vips");
  const ul = $("#vip-list");
  ul.innerHTML = vips.length ? "" : '<li class="muted">No VIPs yet.</li>';
  for (const v of vips) {
    const li = document.createElement("li");
    li.innerHTML = `<span>${escapeHtml(v.name || v.email)} <span class="sender-email">${escapeHtml(v.email)}</span></span>`;
    li.append(btn("Remove", "ghost small", async () => {
      await api(`/api/vips?from_email=${encodeURIComponent(v.email)}`, { method: "DELETE" });
      loadVips();
    }));
    ul.appendChild(li);
  }
}

$("#btn-vip-refresh").onclick = async () => {
  const ul = $("#vip-new");
  ul.innerHTML = '<li class="muted">Checking…</li>';
  const { messages, vips } = await api("/api/vips/new");
  if (!vips) { ul.innerHTML = '<li class="muted">Add some VIPs first.</li>'; return; }
  ul.innerHTML = messages.length ? "" : '<li class="muted">No new unread VIP mail 🎉</li>';
  for (const m of messages) {
    const li = document.createElement("li");
    li.innerHTML = `<span><strong>${escapeHtml(m.from_name || m.from_email)}</strong> —
      ${escapeHtml(m.subject || "(no subject)")}</span>
      <span class="sender-email">${fmtDate(m.date_ts)}</span>`;
    ul.appendChild(li);
  }
};

// --- Modal helper ---------------------------------------------------------

function openModal({ title, bodyHtml, confirmLabel, confirmClass = "danger", onConfirm }) {
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = bodyHtml;
  const confirm = $("#modal-confirm");
  confirm.textContent = confirmLabel;
  confirm.className = "button " + confirmClass;
  $("#modal").classList.remove("hidden");

  const close = () => $("#modal").classList.add("hidden");
  $("#modal-cancel").onclick = close;
  confirm.onclick = async () => {
    confirm.disabled = true;
    try { await onConfirm(); close(); }
    catch (e) { if (e.message !== "no label") toast("Error: " + e.message); }
    finally { confirm.disabled = false; }
  };
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// --- Init -----------------------------------------------------------------

(async function init() {
  const s = await refreshStatus();
  if (s.authorized) {
    if (s.sync?.running) startSyncPolling();
    else loadSenders();
  }
})();
