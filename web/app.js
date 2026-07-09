const state = {
  overview: null,
  activeView: "overview",
  selectedRow: null,
  selectedChat: null,
  orbit: null,
};

const signalColors = ["--signal-0", "--signal-1", "--signal-2", "--signal-3", "--signal-4"];
const skins = ["light", "dark", "neon", "pastel", "country"];

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

function el(tag, className, textValue) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (textValue !== undefined) node.textContent = textValue;
  return node;
}

function svgEl(tag, attrs = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, String(value)));
  return node;
}

function text(value, fallback = "") {
  if (value === null || value === undefined || value === "") return fallback;
  return String(value);
}

function nameOf(row) {
  return text(row.contacts) || text(row.name) || text(row.chat_name) || text(row.identifier) || `chat ${row.chat_id}`;
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

function showError(message) {
  const pane = document.getElementById("detailPane");
  pane.replaceChildren();
  const box = el("div", "empty-state");
  box.append(el("p", "eyebrow", "action failed"));
  box.append(el("h2", "", "Something needs attention"));
  box.append(el("p", "", message));
  pane.append(box);
}

async function runAction(button, action) {
  const original = button ? button.textContent : "";
  if (button) {
    button.disabled = true;
    button.textContent = "Working";
  }
  try {
    return await action();
  } catch (error) {
    showError(error.message);
    return null;
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = original;
    }
  }
}

function setActiveView(view) {
  state.activeView = view;
  if (view !== "overview") stopOrbit();
  document.querySelectorAll(".rail-button").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
  });
  document.getElementById("overviewView").classList.toggle("hidden", view !== "overview");
  document.getElementById("listView").classList.toggle(
    "hidden",
    !["pending", "attention", "recent", "views", "issues"].includes(view),
  );
  document.getElementById("searchView").classList.toggle("hidden", view !== "search");
  document.getElementById("opsView").classList.toggle("hidden", view !== "ops");
}

function renderStatus(status) {
  const strip = document.getElementById("statusStrip");
  strip.replaceChildren();
  const archive = status.archive || {};
  const running = (status.services || []).filter((service) => service.running).length;
  [
    ["chats", archive.chats],
    ["messages", archive.messages],
    ["indexed", archive.search_indexed_messages],
    ["issues", Number(archive.unresolved_chats || 0) + Number(archive.attachment_errors || 0)],
    ["services", `${running}/${(status.services || []).length}`],
  ].forEach(([label, value]) => {
    const metric = el("div", "metric");
    metric.append(el("strong", "", text(value, "0")));
    metric.append(el("span", "", label));
    strip.append(metric);
  });
}

function renderOrbit(rows) {
  stopOrbit();
  const orbit = document.getElementById("orbit");
  orbit.replaceChildren();
  const svg = svgEl("svg", { class: "orbit-links", "aria-hidden": "true" });
  const zap = el("div", "zap-burst", "ZAP!");
  zap.setAttribute("aria-hidden", "true");
  const core = el("button", "core");
  core.type = "button";
  core.append(el("strong", "", "Me"));
  core.append(el("span", "", "operator"));
  orbit.append(svg, core, zap);
  if (!rows.length) {
    orbit.append(el("p", "empty-state", "No inbound signals need attention."));
    return;
  }

  const rect = orbit.getBoundingClientRect();
  const width = Math.max(orbit.clientWidth, rect.width, 640);
  const height = Math.max(orbit.clientHeight, rect.height, 460);
  const center = { x: width / 2, y: height / 2 };
  const visibleRows = rows.slice(0, 16);
  const maxSizeForField = Math.max(
    76,
    Math.min(144, Math.min(width, height) / (visibleRows.length > 7 ? 4.7 : 3.9)),
  );
  const nodes = visibleRows.map((row, index) => {
    const angle = (index / visibleRows.length) * Math.PI * 2 - Math.PI / 2;
    const ring = index % 3;
    const homeRadius = Math.min(width, height) * (0.26 + ring * 0.07);
    const score = Number(row.score || 35);
    const size = Math.max(72, Math.min(maxSizeForField, 62 + score * 0.9));
    const line = svgEl("line", { class: "orbit-link" });
    const node = el("button", "signal");
    node.type = "button";
    node.style.setProperty("--size", `${size}px`);
    node.style.setProperty("--signal-color", `var(${signalColors[index % signalColors.length]})`);
    node.append(el("strong", "", nameOf(row)));
    node.append(el("span", "", `${text(row.hours_waiting, "?")}h`));
    svg.append(line);
    orbit.append(node);
    const body = {
      row,
      element: node,
      line,
      x: center.x + Math.cos(angle) * homeRadius,
      y: center.y + Math.sin(angle) * homeRadius,
      vx: Math.sin(index + 1) * 1.2,
      vy: -Math.cos(index + 1) * 1.2,
      radius: size / 2,
      angle,
      homeRadius,
      drag: null,
      focus: null,
    };
    wireSignalDrag(orbit, body);
    return body;
  });

  state.orbit = {
    container: orbit,
    svg,
    core,
    zap,
    center,
    nodes,
    width,
    height,
    lastTime: 0,
    animationId: 0,
    focusTimeout: 0,
    activeFocus: null,
  };
  orbit.addEventListener("pointerdown", handleOrbitBackgroundPointerDown);
  state.orbit.animationId = requestAnimationFrame(stepOrbit);
}

function stopOrbit() {
  if (state.orbit && state.orbit.animationId) {
    cancelAnimationFrame(state.orbit.animationId);
  }
  if (state.orbit && state.orbit.focusTimeout) {
    window.clearTimeout(state.orbit.focusTimeout);
  }
  state.orbit = null;
}

function handleOrbitBackgroundPointerDown(event) {
  const orbit = state.orbit;
  const target = event.target;
  if (!orbit || !(target instanceof Element)) return;
  if (target.closest(".signal, .core")) return;
  releaseFocusedSignal(orbit);
}

function wireSignalDrag(container, body) {
  const move = (event) => {
    if (!body.drag || body.drag.pointerId !== event.pointerId) return;
    const point = orbitPoint(container, event);
    const dx = point.x - body.drag.startX;
    const dy = point.y - body.drag.startY;
    body.drag.moved ||= Math.hypot(dx, dy) > 5;
    body.vx = (point.x - body.x) * 0.55;
    body.vy = (point.y - body.y) * 0.55;
    body.x = point.x;
    body.y = point.y;
    event.preventDefault();
  };

  const finish = (event) => {
    if (!body.drag || body.drag.pointerId !== event.pointerId) return;
    const moved = body.drag.moved;
    body.drag = null;
    body.element.classList.remove("dragging");
    try {
      body.element.releasePointerCapture(event.pointerId);
    } catch {
      // Pointer capture may already be released by the browser.
    }
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", finish);
    window.removeEventListener("pointercancel", finish);
    if (!moved) focusSignal(body);
  };

  body.element.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    const point = orbitPoint(container, event);
    body.drag = {
      pointerId: event.pointerId,
      startX: point.x,
      startY: point.y,
      moved: false,
    };
    body.element.setPointerCapture(event.pointerId);
    body.element.classList.add("dragging");
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", finish);
    window.addEventListener("pointercancel", finish);
    event.preventDefault();
  });
}

function focusSignal(body) {
  const orbit = state.orbit;
  if (!orbit) return;
  if (orbit.activeFocus && orbit.activeFocus !== body) {
    sendSignalHome(orbit.activeFocus, orbit);
  }
  if (orbit.focusTimeout) {
    window.clearTimeout(orbit.focusTimeout);
    orbit.focusTimeout = 0;
  }
  orbit.activeFocus = body;
  orbit.container.classList.add("is-focusing");
  orbit.zap.classList.remove("active");
  orbit.zap.style.left = `${body.x}px`;
  orbit.zap.style.top = `${body.y}px`;
  body.element.classList.add("focused");
  body.line.classList.add("focused");
  const startX = body.x;
  const startY = body.y;
  body.focus = {
    phase: "in",
    startTime: 0,
    duration: 340,
    startX,
    startY,
    progress: 0,
  };
  body.vx = 0;
  body.vy = 0;
  window.requestAnimationFrame(() => orbit.zap.classList.add("active"));
  orbit.focusTimeout = window.setTimeout(() => {
    openChat(body.row).catch((error) => showError(error.message));
    window.setTimeout(() => {
      if (state.orbit !== orbit || orbit.activeFocus !== body || !body.focus) return;
      body.focus.phase = "hold";
      body.focus.startTime = 0;
      body.focus.progress = 1;
      body.x = orbit.center.x;
      body.y = orbit.center.y;
      body.vx = 0;
      body.vy = 0;
      orbit.focusTimeout = 0;
    }, 180);
  }, 320);
}

function releaseFocusedSignal(orbit) {
  if (!orbit.activeFocus) return;
  sendSignalHome(orbit.activeFocus, orbit);
  orbit.activeFocus = null;
  orbit.container.classList.remove("is-focusing");
  orbit.zap.classList.remove("active");
  if (orbit.focusTimeout) {
    window.clearTimeout(orbit.focusTimeout);
    orbit.focusTimeout = 0;
  }
}

function sendSignalHome(body, orbit) {
  if (state.orbit !== orbit || !body.focus) return;
  const target = signalHomePoint(body, orbit);
  body.focus = {
    phase: "out",
    startTime: 0,
    duration: 520,
    startX: body.x,
    startY: body.y,
    endX: target.x,
    endY: target.y,
    progress: 0,
  };
  body.vx = 0;
  body.vy = 0;
}

function signalHomePoint(node, orbit) {
  const targetRadius = Math.min(orbit.width, orbit.height) * (0.24 + (orbit.nodes.indexOf(node) % 3) * 0.075);
  const x = orbit.center.x + Math.cos(node.angle) * targetRadius;
  const y = orbit.center.y + Math.sin(node.angle) * targetRadius;
  const padding = node.radius + 18;
  return {
    x: Math.max(padding, Math.min(orbit.width - padding, x)),
    y: Math.max(padding, Math.min(orbit.height - padding, y)),
  };
}

function orbitPoint(container, event) {
  const rect = container.getBoundingClientRect();
  return {
    x: event.clientX - rect.left,
    y: event.clientY - rect.top,
  };
}

function stepOrbit(timestamp) {
  const orbit = state.orbit;
  if (!orbit) return;
  const rect = orbit.container.getBoundingClientRect();
  orbit.width = Math.max(orbit.container.clientWidth, rect.width, 320);
  orbit.height = Math.max(orbit.container.clientHeight, rect.height, 320);
  orbit.center.x = orbit.width / 2;
  orbit.center.y = orbit.height / 2;
  const dt = Math.min(2, orbit.lastTime ? (timestamp - orbit.lastTime) / 16.67 : 1);
  orbit.lastTime = timestamp;

  orbit.nodes.forEach((node, index) => {
    if (node.drag) return;
    if (node.focus) {
      if (!node.focus.startTime) node.focus.startTime = timestamp;
      const progress = node.focus.phase === "hold"
        ? 1
        : Math.min(1, (timestamp - node.focus.startTime) / node.focus.duration);
      const eased = easeOutExpo(progress);
      node.focus.progress = progress;
      const targetX = node.focus.phase === "out" ? node.focus.endX : orbit.center.x;
      const targetY = node.focus.phase === "out" ? node.focus.endY : orbit.center.y;
      node.x = node.focus.startX + (targetX - node.focus.startX) * eased;
      node.y = node.focus.startY + (targetY - node.focus.startY) * eased;
      node.vx = 0;
      node.vy = 0;
      if (node.focus.phase === "out" && progress >= 1) {
        node.focus = null;
        node.element.classList.remove("focused");
        node.line.classList.remove("focused");
      }
      return;
    }
    const drift = Math.sin(timestamp / 1400 + index) * 0.16;
    const targetRadius = Math.min(orbit.width, orbit.height) * (0.24 + (index % 3) * 0.075);
    node.homeRadius += (targetRadius - node.homeRadius) * 0.02;
    const targetX = orbit.center.x + Math.cos(node.angle + drift) * node.homeRadius;
    const targetY = orbit.center.y + Math.sin(node.angle + drift) * node.homeRadius;
    node.vx += (targetX - node.x) * 0.012 * dt;
    node.vy += (targetY - node.y) * 0.012 * dt;

    const coreClearance = Math.max(80, Math.min(104, Math.min(orbit.width, orbit.height) * 0.17))
      + node.radius * 0.6;
    const fromCenterX = node.x - orbit.center.x;
    const fromCenterY = node.y - orbit.center.y;
    const fromCenterDistance = Math.max(1, Math.hypot(fromCenterX, fromCenterY));
    if (fromCenterDistance < coreClearance) {
      const force = (coreClearance - fromCenterDistance) * 0.018 * dt;
      node.vx += (fromCenterX / fromCenterDistance) * force;
      node.vy += (fromCenterY / fromCenterDistance) * force;
    }
  });

  for (let i = 0; i < orbit.nodes.length; i += 1) {
    for (let j = i + 1; j < orbit.nodes.length; j += 1) {
      separateSignals(orbit.nodes[i], orbit.nodes[j], dt);
    }
  }

  orbit.nodes.forEach((node) => {
    if (!node.drag) {
      node.vx *= 0.91;
      node.vy *= 0.91;
      node.x += node.vx * dt;
      node.y += node.vy * dt;
      bounceInBounds(node, orbit.width, orbit.height);
    }
    paintSignal(node, orbit.center);
  });
  orbit.animationId = requestAnimationFrame(stepOrbit);
}

function separateSignals(a, b, dt) {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const distance = Math.max(1, Math.hypot(dx, dy));
  const target = a.radius + b.radius + 16;
  if (distance >= target) return;
  const push = (target - distance) * 0.025 * dt;
  const nx = dx / distance;
  const ny = dy / distance;
  if (!a.drag) {
    a.vx -= nx * push;
    a.vy -= ny * push;
  }
  if (!b.drag) {
    b.vx += nx * push;
    b.vy += ny * push;
  }
}

function bounceInBounds(node, width, height) {
  const padding = 18;
  const minX = node.radius + padding;
  const maxX = width - node.radius - padding;
  const minY = node.radius + padding;
  const maxY = height - node.radius - padding;
  if (node.x < minX) {
    node.x = minX;
    node.vx = Math.abs(node.vx) * 0.72;
  } else if (node.x > maxX) {
    node.x = maxX;
    node.vx = -Math.abs(node.vx) * 0.72;
  }
  if (node.y < minY) {
    node.y = minY;
    node.vy = Math.abs(node.vy) * 0.72;
  } else if (node.y > maxY) {
    node.y = maxY;
    node.vy = -Math.abs(node.vy) * 0.72;
  }
}

function paintSignal(node, center) {
  node.element.style.left = `${node.x}px`;
  node.element.style.top = `${node.y}px`;
  const focusScale = node.focus && node.focus.phase === "out"
    ? 1 + 0.72 * (1 - node.focus.progress)
    : 1.72;
  node.element.style.setProperty("--signal-scale", node.focus ? String(focusScale) : "1");
  node.line.setAttribute("x1", center.x);
  node.line.setAttribute("y1", center.y);
  node.line.setAttribute("x2", node.x);
  node.line.setAttribute("y2", node.y);
}

function easeOutExpo(value) {
  return value >= 1 ? 1 : 1 - 2 ** (-10 * value);
}

function rowButton(row, extra = {}) {
  const button = el("button", "row-item");
  button.type = "button";
  button.append(el("h3", "", nameOf(row)));
  button.append(el("p", "", snippet(row.last_text || row.text || row.proposed_text || row.reason || "")));
  const meta = el("div", "meta-line");
  meta.append(el("span", "", when(row.last_message_at || row.message_at)));
  if (row.draft_status) meta.append(el("span", "status-chip", row.draft_status));
  if (row.score) meta.append(el("span", "", `score ${row.score}`));
  button.append(meta);
  if (row.chat_id) {
    button.addEventListener("click", () => openChat({ ...row, ...extra }));
  } else {
    button.disabled = true;
  }
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

function renderGroupedViews(payload) {
  document.getElementById("listTitle").textContent = "Saved views";
  document.getElementById("listEyebrow").textContent = "operator cuts";
  const list = document.getElementById("tableList");
  list.replaceChildren();
  [
    ["Unanswered", payload.unanswered || []],
    ["Recently active", payload.recently_active || []],
    ["Quiet relationships", payload.quiet_relationships || []],
    ["Attachment issues", payload.attachment_issues || []],
  ].forEach(([title, rows]) => {
    const section = el("section", "view-section");
    section.append(el("h3", "", title));
    if (!rows.length) {
      section.append(el("p", "empty-state compact", "No rows."));
    } else {
      rows.forEach((row) => section.append(rowButton({
        ...row,
        name: row.chat_name || row.name,
        last_text: row.last_text || row.archive_error || row.transfer_name,
        last_message_at: row.last_message_at || row.message_at,
        draft_status: row.draft_status || title.toLowerCase(),
      })));
    }
    list.append(section);
  });
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
    renderTable("Pending drafts", "review queue", await api("/api/pending?limit=50"));
  } else if (view === "attention") {
    renderTable("Needs attention", "inbound latest messages", await api("/api/attention?limit=60"));
  } else if (view === "recent") {
    renderTable("Recently active", "archive timeline", await api("/api/recent?limit=60"));
  } else if (view === "views") {
    renderGroupedViews(await api("/api/views?limit=40"));
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
  state.selectedChat = payload;
  renderChat(payload, row);
}

function renderChat(payload, row) {
  const pane = document.getElementById("detailPane");
  pane.replaceChildren();
  pane.classList.remove("tray-zap");
  void pane.offsetHeight;
  pane.classList.add("tray-zap");
  const wrap = el("div", "chat-detail");
  const head = el("div", "chat-head");
  head.append(el("p", "eyebrow", `chat ${text(row.chat_id)}`));
  head.append(el("h2", "", nameOf(row)));
  head.append(el("p", "", snippet(row.last_text || "", 180)));
  wrap.append(head);
  wrap.append(renderContextBox(payload, row));

  if (row.draft_uuid && row.proposed_text && row.draft_status !== "outbox" && row.draft_status !== "sent") {
    wrap.append(renderDraftBox(row));
  } else if (row.draft_status === "missing" && row.message_rowid) {
    wrap.append(renderMissingDraftBox(row));
  } else if (row.draft_status) {
    const statusBox = el("div", "draft-box");
    statusBox.append(el("p", "eyebrow", "reply state"));
    statusBox.append(el("p", "", `This item is currently ${row.draft_status}.`));
    wrap.append(statusBox);
  }

  const timeline = el("div", "timeline");
  (payload.messages || []).forEach((message) => {
    const rowNode = el("div", `message-row ${message.is_from_me ? "mine" : "theirs"}`);
    rowNode.append(el("div", "sender-label", senderLabel(message, payload, row)));
    const bubble = el("div", "message-bubble");
    bubble.append(el("div", "", text(message.text, "(no text)")));
    bubble.append(el("span", "time", when(message.message_at)));
    rowNode.append(bubble);
    timeline.append(rowNode);
  });
  wrap.append(timeline);
  pane.append(wrap);
}

function senderLabel(message, payload, row) {
  if (message.is_from_me) return "Me";
  return (
    text(message.sender_name) ||
    text(message.sender) ||
    singleContactName(message.contacts) ||
    singleContactName(payload.chat && payload.chat.contacts) ||
    text(message.chat_name) ||
    nameOf(row)
  );
}

function singleContactName(value) {
  const clean = text(value);
  if (!clean) return "";
  const names = clean.split(",").map((name) => name.trim()).filter(Boolean);
  return names.length === 1 ? names[0] : "";
}

function renderContextBox(payload, row) {
  const context = payload.context || {};
  const box = el("div", "context-box");
  box.append(el("p", "eyebrow", "relationship context"));

  const relationship = input("Relationship", context.relationship || "");
  const tone = input("Tone", context.tone || "");
  const professional = checkbox("Professional", Boolean(context.professional));
  const autoApprove = checkbox("Auto approve", Boolean(context.auto_approve));
  const doNotDraft = checkbox("Do not draft", Boolean(context.do_not_draft));
  const notes = textarea("Notes", payload.notes || "", "context-notes");

  box.append(relationship.label, tone.label);
  const toggles = el("div", "toggle-row");
  toggles.append(professional.label, autoApprove.label, doNotDraft.label);
  box.append(toggles, notes.label);

  const save = el("button", "inline-action", "Save context");
  save.type = "button";
  save.addEventListener("click", () => runAction(save, async () => {
    await api(`/api/chats/${row.chat_id}/context`, {
      method: "POST",
      body: JSON.stringify({
        fields: {
          relationship: relationship.input.value,
          tone: tone.input.value,
          professional: professional.input.checked,
          auto_approve: autoApprove.input.checked,
          do_not_draft: doNotDraft.input.checked,
        },
        notes: notes.input.value,
      }),
    });
    await openChat(state.selectedRow);
  }));
  box.append(save);
  return box;
}

function renderMissingDraftBox(row) {
  const box = el("div", "draft-box");
  box.append(el("p", "eyebrow", "draft"));
  box.append(el("p", "", "No proposal exists for this inbound message yet."));
  const reason = textarea("No-reply reason", "Handled elsewhere or no reply needed.", "short-textarea");
  box.append(reason.label);
  const actions = el("div", "draft-actions two");
  const request = el("button", "", "Draft");
  const noReply = el("button", "", "No reply");
  request.type = "button";
  noReply.type = "button";
  request.addEventListener("click", () => runAction(request, () => requestDraft(row)));
  noReply.addEventListener("click", () => runAction(noReply, () => markNoReply(row, reason.input.value)));
  actions.append(request, noReply);
  box.append(actions);
  return box;
}

function renderDraftBox(row) {
  const box = el("div", "draft-box");
  box.append(el("p", "eyebrow", "proposed reply"));
  if (row.reasoning) box.append(el("p", "", snippet(row.reasoning, 220)));
  const draft = textarea("Draft text", row.proposed_text, "");
  box.append(draft.label);
  const rejectReason = textarea("Reject reason", "Not the right reply.", "short-textarea");
  box.append(rejectReason.label);
  const actions = el("div", "draft-actions");
  const approve = el("button", "", "Approve");
  const save = el("button", "", "Save");
  const discard = el("button", "", "Discard");
  const reject = el("button", "", "Reject");
  [approve, save, discard, reject].forEach((button) => {
    button.type = "button";
    actions.append(button);
  });
  approve.addEventListener("click", () => runAction(approve, () => approveDraft(row.draft_uuid, draft.input.value)));
  save.addEventListener("click", () => runAction(save, () => saveDraft(row.draft_uuid, draft.input.value)));
  discard.addEventListener("click", () => runAction(discard, () => discardDraft(row.draft_uuid)));
  reject.addEventListener("click", () => runAction(reject, () => rejectDraft(row.draft_uuid, rejectReason.input.value)));
  box.append(actions);
  return box;
}

function input(labelText, value) {
  const label = el("label", "field", labelText);
  const node = document.createElement("input");
  node.value = value;
  label.append(node);
  return { label, input: node };
}

function textarea(labelText, value, className) {
  const label = el("label", "field", labelText);
  const node = document.createElement("textarea");
  if (className) node.className = className;
  node.value = value;
  label.append(node);
  return { label, input: node };
}

function checkbox(labelText, value) {
  const label = el("label", "check-field");
  const node = document.createElement("input");
  node.type = "checkbox";
  node.checked = value;
  label.append(node, el("span", "", labelText));
  return { label, input: node };
}

async function approveDraft(uuid, draftText) {
  await api(`/api/drafts/${encodeURIComponent(uuid)}/approve`, {
    method: "POST",
    body: JSON.stringify({ text: draftText }),
  });
  await refreshAfterMutation("Queued in outbox. The existing sender path will handle delivery.");
}

async function saveDraft(uuid, draftText) {
  await api(`/api/drafts/${encodeURIComponent(uuid)}/edit`, {
    method: "POST",
    body: JSON.stringify({ text: draftText }),
  });
  await refreshAfterMutation("Draft saved and left unapproved.");
}

async function discardDraft(uuid) {
  await api(`/api/drafts/${encodeURIComponent(uuid)}/discard`, { method: "POST" });
  await refreshAfterMutation("Draft discarded. The file-based workflow remains unchanged.");
}

async function rejectDraft(uuid, reasoning) {
  await api(`/api/drafts/${encodeURIComponent(uuid)}/reject`, {
    method: "POST",
    body: JSON.stringify({ reasoning }),
  });
  await refreshAfterMutation("Draft rejected and recorded as no reply needed.");
}

async function requestDraft(row) {
  await api("/api/drafts/request", {
    method: "POST",
    body: JSON.stringify({ message_rowid: Number(row.message_rowid) }),
  });
  await loadOverview();
  const pending = await api("/api/pending?limit=80");
  const updated = pending.find((item) => Number(item.message_rowid) === Number(row.message_rowid));
  if (updated) await openChat(updated);
}

async function markNoReply(row, reasoning) {
  await api("/api/no-reply", {
    method: "POST",
    body: JSON.stringify({
      chat_id: Number(row.chat_id),
      source_rowid: Number(row.message_rowid),
      reasoning,
    }),
  });
  await refreshAfterMutation("No-reply decision recorded.");
}

async function refreshAfterMutation(message) {
  await loadOverview();
  if (state.activeView !== "overview") await loadList(state.activeView);
  const pane = document.getElementById("detailPane");
  pane.replaceChildren();
  const box = el("div", "empty-state");
  box.append(el("p", "eyebrow", "done"));
  box.append(el("h2", "", "Updated"));
  box.append(el("p", "", message));
  pane.append(box);
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
  results.forEach((row) => list.append(rowButton({ ...row, name: row.chat_name, last_text: row.text })));
}

async function loadOps() {
  setActiveView("ops");
  const status = await api("/api/status");
  renderStatus(status);
  const grid = document.getElementById("opsGrid");
  grid.replaceChildren();
  (status.services || []).forEach((service) => {
    const card = el("div", "service-card");
    card.append(el("p", "eyebrow", service.service));
    card.append(el("h3", "", service.running ? "Running" : service.loaded ? "Loaded" : "Stopped"));
    card.append(el("p", "", service.detail || service.label));
    const actions = el("div", "service-actions");
    ["start", "restart", "stop"].forEach((action) => {
      const button = el("button", "", action);
      button.type = "button";
      button.addEventListener("click", () => runAction(button, async () => {
        await api(`/api/services/${service.service}/${action}`, { method: "POST" });
        await loadOps();
      }));
      actions.append(button);
    });
    const logs = el("button", "", "logs");
    logs.type = "button";
    logs.addEventListener("click", () => runAction(logs, () => loadLogs(service.service, false)));
    const errors = el("button", "", "errors");
    errors.type = "button";
    errors.addEventListener("click", () => runAction(errors, () => loadLogs(service.service, true)));
    actions.append(logs, errors);
    card.append(actions);
    grid.append(card);
  });
}

async function loadLogs(service, errors) {
  const payload = await api(`/api/logs/${service}/${errors ? "errors" : "output"}?lines=120`);
  const panel = document.getElementById("logPanel");
  panel.replaceChildren();
  panel.append(el("p", "eyebrow", payload.path));
  const pre = el("pre", "", (payload.lines || []).join("\n"));
  panel.append(pre);
}

function setSkin(skin) {
  const selected = skins.includes(skin) ? skin : "light";
  document.body.dataset.skin = selected;
  document.body.classList.remove("active");
  localStorage.setItem("imsg-agent-skin", selected);
  document.querySelectorAll(".skin-switch [data-skin]").forEach((button) => {
    button.classList.toggle("active", button.dataset.skin === selected);
  });
}

function initSkinControls() {
  setSkin(localStorage.getItem("imsg-agent-skin") || "light");
  document.querySelectorAll(".skin-switch [data-skin]").forEach((button) => {
    button.addEventListener("click", () => setSkin(button.dataset.skin));
  });
}

document.getElementById("refreshButton").addEventListener("click", (event) => {
  runAction(event.currentTarget, loadOverview);
});
document.getElementById("opsRefreshButton").addEventListener("click", (event) => {
  runAction(event.currentTarget, loadOps);
});
document.getElementById("searchForm").addEventListener("submit", (event) => {
  runAction(event.submitter, () => runSearch(event));
});
document.querySelectorAll(".rail-button").forEach((button) => {
  button.addEventListener("click", () => runAction(button, async () => {
    const view = button.dataset.view;
    setActiveView(view);
    if (view === "overview") await loadOverview();
    if (["pending", "attention", "recent", "views", "issues"].includes(view)) await loadList(view);
    if (view === "ops") await loadOps();
  }));
});

initSkinControls();
loadOverview().catch((error) => showError(error.message));
