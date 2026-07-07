const state = {
  overview: null,
  activeView: "overview",
  selectedRow: null,
};

const colors = ["#e85d4f", "#2ca58d", "#305fbd", "#d18b21", "#7254a3"];

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || "Request failed");
  }
  return payload;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function text(value, fallback = "") {
  if (value === null || value === undefined || value === "") return fallback;
  return String(value);
}

function nameOf(row) {
  return text(row.contacts) || text(row.name) || text(row.identifier) || `chat ${row.chat_id}`;
}

function when(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function snippet(value, max = 130) {
  const clean = text(value).replace(/\s+/g, " ").trim();
  if (clean.length <= max) return clean;
  return `${clean.slice(0, max - 3)}...`;
}

function setActiveView(view) {
  state.activeView = view;
  document.querySelectorAll(".rail-button").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
  });
  document.getElementById("overviewView").classList.toggle("hidden", view !== "overview");
  document.getElementById("listView").classList.toggle(
    "hidden",
    !["pending", "attention", "issues"].includes(view),
  );
  document.getElementById("searchView").classList.toggle("hidden", view !== "search");
}

function renderStatus(status) {
  const strip = document.getElementById("statusStrip");
  strip.replaceChildren();
  const archive = status.archive || {};
  [
    ["chats", archive.chats],
    ["messages", archive.messages],
    ["draft db", archive.search_indexed_messages],
    ["issues", Number(archive.unresolved_chats || 0) + Number(archive.attachment_errors || 0)],
  ].forEach(([label, value]) => {
    const metric = el("div", "metric");
    metric.append(el("strong", "", text(value, "0")));
    metric.append(el("span", "", label));
    strip.append(metric);
  });
}

function renderOrbit(rows) {
  const orbit = document.getElementById("orbit");
  orbit.replaceChildren();
  orbit.append(el("div", "core", "reply gravity"));
  if (!rows.length) {
    orbit.append(el("p", "empty-state", "No inbound signals need attention."));
    return;
  }
  rows.slice(0, 14).forEach((row, index) => {
    const angle = (index / rows.length) * Math.PI * 2 - Math.PI / 2;
    const ring = index % 3;
    const radius = 30 + ring * 11;
    const x = 50 + Math.cos(angle) * radius;
    const y = 50 + Math.sin(angle) * radius;
    const score = Number(row.score || 35);
    const node = el("button", "signal");
    node.type = "button";
    node.style.left = `${x}%`;
    node.style.top = `${y}%`;
    node.style.setProperty("--size", `${Math.max(82, Math.min(144, 78 + score))}px`);
    node.style.setProperty("--signal-color", colors[index % colors.length]);
    node.append(el("strong", "", nameOf(row)));
    node.append(el("span", "", `${text(row.hours_waiting, "?")}h`));
    node.addEventListener("click", () => openChat(row));
    orbit.append(node);
  });
}

function rowButton(row, extra = {}) {
  const button = el("button", "row-item");
  button.type = "button";
  button.append(el("h3", "", nameOf(row)));
  button.append(el("p", "", snippet(row.last_text || row.text || row.proposed_text || "")));
  const meta = el("div", "meta-line");
  meta.append(el("span", "", when(row.last_message_at || row.message_at)));
  if (row.draft_status) meta.append(el("span", "status-chip", row.draft_status));
  if (row.score) meta.append(el("span", "", `score ${row.score}`));
  button.append(meta);
  button.addEventListener("click", () => openChat({ ...row, ...extra }));
  return button;
}

function renderPending(rows) {
  const list = document.getElementById("pendingList");
  list.replaceChildren();
  if (!rows.length) {
    list.append(el("p", "empty-state", "Nothing is waiting for review."));
    return;
  }
  rows.forEach((row) => list.append(rowButton(row)));
}

function renderTable(title, eyebrow, rows) {
  document.getElementById("listTitle").textContent = title;
  document.getElementById("listEyebrow").textContent = eyebrow;
  const list = document.getElementById("tableList");
  list.replaceChildren();
  if (!rows.length) {
    list.append(el("p", "empty-state", "No rows for this view."));
    return;
  }
  rows.forEach((row) => list.append(rowButton(row)));
}

async function loadOverview() {
  const overview = await api("/api/overview?limit=14");
  state.overview = overview;
  renderStatus(overview.status);
  renderOrbit(overview.attention || []);
  renderPending(overview.pending || []);
}

async function loadList(view) {
  setActiveView(view);
  if (view === "pending") {
    const rows = await api("/api/pending?limit=50");
    renderTable("Pending drafts", "review queue", rows);
  } else if (view === "attention") {
    const rows = await api("/api/attention?limit=60");
    renderTable("Needs attention", "inbound latest messages", rows);
  } else if (view === "issues") {
    const payload = await api("/api/issues?limit=80");
    const contacts = (payload.unresolved_contacts || []).map((row) => ({
      ...row,
      last_text: row.source_identifier || row.normalized_value,
      draft_status: "unresolved contact",
    }));
    const attachments = (payload.attachment_issues || []).map((row) => ({
      ...row,
      name: row.chat_name,
      last_text: row.archive_error || row.transfer_name || row.original_path,
      last_message_at: row.message_at,
      draft_status: "attachment",
    }));
    renderTable("Operational issues", "repair queue", [...contacts, ...attachments]);
  }
}

async function openChat(row) {
  state.selectedRow = row;
  const chatId = row.chat_id;
  if (!chatId) return;
  const payload = await api(`/api/chats/${chatId}/messages?limit=90`);
  renderChat(payload, row);
}

function renderChat(payload, row) {
  const pane = document.getElementById("detailPane");
  pane.replaceChildren();
  const wrap = el("div", "chat-detail");
  const head = el("div", "chat-head");
  head.append(el("p", "eyebrow", `chat ${text(row.chat_id)}`));
  head.append(el("h2", "", nameOf(row)));
  head.append(el("p", "", snippet(row.last_text || "", 180)));
  wrap.append(head);

  const context = payload.context || {};
  const contextBox = el("div", "context-box");
  contextBox.append(el("p", "eyebrow", "relationship context"));
  contextBox.append(
    el(
      "p",
      "",
      [
        context.relationship && `relationship: ${context.relationship}`,
        context.tone && `tone: ${context.tone}`,
        context.professional !== undefined && `professional: ${context.professional}`,
      ]
        .filter(Boolean)
        .join(" / ") || "No context fields yet.",
    ),
  );
  if (payload.notes) contextBox.append(el("p", "", snippet(payload.notes, 260)));
  wrap.append(contextBox);

  if (row.draft_uuid && row.proposed_text) {
    wrap.append(renderDraftBox(row));
  } else if (row.draft_status === "missing" && row.message_rowid) {
    const draftBox = el("div", "draft-box");
    draftBox.append(el("p", "eyebrow", "draft"));
    draftBox.append(el("p", "", "No proposal exists for this inbound message yet."));
    const actions = el("div", "draft-actions");
    const request = el("button", "", "Draft");
    request.type = "button";
    request.addEventListener("click", () => requestDraft(row));
    actions.append(request);
    draftBox.append(actions);
    wrap.append(draftBox);
  }

  const timeline = el("div", "timeline");
  (payload.messages || []).forEach((message) => {
    const bubble = el("div", `message ${message.is_from_me ? "mine" : ""}`);
    bubble.append(el("div", "", text(message.text, "(no text)")));
    bubble.append(el("span", "time", when(message.message_at)));
    timeline.append(bubble);
  });
  wrap.append(timeline);
  pane.append(wrap);
}

function renderDraftBox(row) {
  const box = el("div", "draft-box");
  box.append(el("p", "eyebrow", "proposed reply"));
  if (row.reasoning) box.append(el("p", "", snippet(row.reasoning, 220)));
  const label = el("label", "", "Draft text");
  const textarea = el("textarea");
  textarea.value = row.proposed_text;
  label.append(textarea);
  box.append(label);
  const actions = el("div", "draft-actions");
  const approve = el("button", "", "Approve");
  const save = el("button", "", "Save");
  const discard = el("button", "", "Discard");
  [approve, save, discard].forEach((button) => {
    button.type = "button";
    actions.append(button);
  });
  approve.addEventListener("click", () => approveDraft(row.draft_uuid, textarea.value));
  save.addEventListener("click", () => saveDraft(row.draft_uuid, textarea.value));
  discard.addEventListener("click", () => discardDraft(row.draft_uuid));
  box.append(actions);
  return box;
}

async function approveDraft(uuid, draftText) {
  await api(`/api/drafts/${encodeURIComponent(uuid)}/approve`, {
    method: "POST",
    body: JSON.stringify({ text: draftText }),
  });
  await loadOverview();
  document.getElementById("detailPane").replaceChildren(
    el("div", "empty-state", "Queued in outbox. The existing sender path will handle delivery."),
  );
}

async function saveDraft(uuid, draftText) {
  await api(`/api/drafts/${encodeURIComponent(uuid)}/edit`, {
    method: "POST",
    body: JSON.stringify({ text: draftText }),
  });
  await loadOverview();
}

async function discardDraft(uuid) {
  await api(`/api/drafts/${encodeURIComponent(uuid)}/discard`, { method: "POST" });
  await loadOverview();
  document.getElementById("detailPane").replaceChildren(
    el("div", "empty-state", "Draft discarded. The file-based workflow remains unchanged."),
  );
}

async function requestDraft(row) {
  await api("/api/drafts/request", {
    method: "POST",
    body: JSON.stringify({ message_rowid: Number(row.message_rowid) }),
  });
  await loadOverview();
}

async function runSearch(event) {
  event.preventDefault();
  const query = document.getElementById("searchInput").value.trim();
  const results = await api(`/api/search?q=${encodeURIComponent(query)}&limit=60`);
  const list = document.getElementById("searchResults");
  list.replaceChildren();
  if (!results.length) {
    list.append(el("p", "empty-state", "No matching messages."));
    return;
  }
  results.forEach((row) => list.append(rowButton({ ...row, last_text: row.text })));
}

document.getElementById("refreshButton").addEventListener("click", loadOverview);
document.getElementById("searchForm").addEventListener("submit", runSearch);
document.querySelectorAll(".rail-button").forEach((button) => {
  button.addEventListener("click", async () => {
    const view = button.dataset.view;
    setActiveView(view);
    if (view === "overview") await loadOverview();
    if (["pending", "attention", "issues"].includes(view)) await loadList(view);
  });
});

loadOverview().catch((error) => {
  document.getElementById("detailPane").replaceChildren(el("div", "empty-state", error.message));
});
