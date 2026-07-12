const state = {
  overview: null,
  activeView: "overview",
  selectedRow: null,
  selectedChat: null,
  chatPanel: "messages",
  orbit: null,
  operator: null,
  preferences: null,
  revision: "",
  contactsQuery: "",
  contactsOffset: 0,
  orbitDirection: "incoming",
  orbitDays: 7,
  orbitOffset: 0,
  orbitPage: null,
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
  document.getElementById("contactsView").classList.toggle("hidden", view !== "contacts");
  document.getElementById("settingsView").classList.toggle("hidden", view !== "settings");
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

function renderOrbit(rows, operator = {}) {
  stopOrbit();
  const orbit = document.getElementById("orbit");
  orbit.replaceChildren();
  const svg = svgEl("svg", { class: "orbit-links", "aria-hidden": "true" });
  const zap = el("div", "zap-burst", "ZAP!");
  zap.setAttribute("aria-hidden", "true");
  const core = el("button", "core");
  core.type = "button";
  const avatar = text(operator.avatar_data_uri);
  if (avatar) {
    const image = document.createElement("img");
    image.className = "core-avatar";
    image.src = avatar;
    image.alt = "";
    core.append(image);
  }
  core.append(el("strong", "", text(operator.display_name, "Me")));
  core.append(el("span", "", text(operator.contact?.organization_name, "operator")));
  core.addEventListener("click", () => openOperatorProfile().catch((error) => showError(error.message)));
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
    node.append(el("span", "", `${text(row.hours_waiting, "?")}h / ${text(row.orbit_score, text(row.score, "0"))}`));
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
  closeWorkbench();
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

function renderPending(rows, preferences = {}) {
  const list = document.getElementById("pendingList");
  list.replaceChildren();
  if (!rows.length) {
    list.append(el("p", "empty-state", "Nothing is waiting for review."));
    return;
  }
  if (!preferences.group_pending_by_relationship) {
    rows.forEach((row) => list.append(rowButton(row)));
    return;
  }
  const groups = new Map();
  rows.forEach((row) => {
    const relationship = text(row.relationship, "unclassified");
    groups.set(relationship, [...(groups.get(relationship) || []), row]);
  });
  groups.forEach((groupRows, relationship) => {
    const group = el("section", "queue-group");
    group.append(el("h3", "queue-group-title", relationship));
    groupRows.forEach((row) => group.append(rowButton(row)));
    list.append(group);
  });
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
  state.operator = overview.operator || {};
  state.preferences = overview.preferences || {};
  state.revision = text(overview.revision);
  renderStatus(overview.status);
  renderPending(overview.pending || [], state.preferences);
  await loadOrbit();
}

async function loadOrbit(reset = false) {
  if (reset) state.orbitOffset = 0;
  const page = await api(
    `/api/orbit?limit=16&offset=${state.orbitOffset}&direction=${encodeURIComponent(state.orbitDirection)}&days=${state.orbitDays}`,
  );
  state.orbitPage = page;
  renderOrbit(page.items || [], state.operator || {});
  renderOrbitPagination(page);
}

function renderOrbitPagination(page) {
  const container = document.getElementById("orbitPagination");
  container.replaceChildren();
  const previous = el("button", "icon-button", "Previous");
  previous.type = "button";
  previous.disabled = Number(page.offset || 0) === 0;
  previous.addEventListener("click", () => {
    state.orbitOffset = Math.max(0, Number(page.offset || 0) - Number(page.limit || 16));
    loadOrbit().catch((error) => showError(error.message));
  });
  const next = el("button", "icon-button", "Next");
  next.type = "button";
  next.disabled = page.next_offset === null || page.next_offset === undefined;
  next.addEventListener("click", () => {
    state.orbitOffset = Number(page.next_offset);
    loadOrbit().catch((error) => showError(error.message));
  });
  const items = page.items || [];
  const first = items.length ? Number(page.offset || 0) + 1 : 0;
  container.append(previous, el("span", "", `${first}-${Number(page.offset || 0) + items.length} of ${Number(page.total || 0)}`), next);
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

async function loadContacts(query = state.contactsQuery, offset = 0) {
  setActiveView("contacts");
  state.contactsQuery = query;
  state.contactsOffset = offset;
  const page = await api(`/api/contacts/page?limit=40&offset=${offset}&q=${encodeURIComponent(query)}`);
  const rows = page.items || [];
  const list = document.getElementById("contactsList");
  list.replaceChildren();
  if (!rows.length) {
    list.append(el("p", "empty-state compact", "No synced contacts match this search."));
    return;
  }
  rows.forEach((row) => {
    const button = el("button", "row-item");
    button.type = "button";
    const title = el("div", "contact-row-title");
    if (row.photo_data_uri) {
      const image = document.createElement("img");
      image.className = "contact-avatar";
      image.src = row.photo_data_uri;
      image.alt = "";
      title.append(image);
    }
    title.append(el("h3", "", text(row.full_name, text(row.organization_name, row.contact_id))));
    button.append(title);
    button.append(el("p", "", text(row.organization_name)));
    button.append(el("p", "meta-line", `${text(row.contact_points, "0")} points / importance ${text(row.importance, "0")}`));
    button.addEventListener("click", () => openContact(row.contact_id));
    list.append(button);
  });
  list.append(renderPagination(page, (nextOffset) => loadContacts(query, nextOffset)));
}

function renderPagination(page, onPage) {
  const controls = el("div", "pagination");
  const previous = el("button", "icon-button", "Previous");
  previous.type = "button";
  previous.disabled = Number(page.offset || 0) === 0;
  previous.addEventListener("click", () => onPage(Math.max(0, Number(page.offset || 0) - Number(page.limit || 40))));
  const next = el("button", "icon-button", "Next");
  next.type = "button";
  next.disabled = page.next_offset === null || page.next_offset === undefined;
  next.addEventListener("click", () => onPage(Number(page.next_offset)));
  const items = page.items || [];
  const first = items.length ? Number(page.offset || 0) + 1 : 0;
  controls.append(previous, el("span", "", `${first}-${Number(page.offset || 0) + items.length} of ${Number(page.total || 0)}`), next);
  return controls;
}

async function openContact(contactId) {
  const contact = await api(`/api/contacts/${encodeURIComponent(contactId)}`);
  const list = document.getElementById("contactsList");
  list.replaceChildren();
  const panel = el("section", "contact-detail");
  panel.append(el("p", "eyebrow", "synced contact"));
  if (contact.photo_data_uri) {
    const image = document.createElement("img");
    image.className = "contact-avatar large";
    image.src = contact.photo_data_uri;
    image.alt = "";
    panel.append(image);
  }
  panel.append(el("h2", "", text(contact.full_name, contact.contact_id)));
  const name = input("Full name", text(contact.full_name));
  const organization = input("Organization", text(contact.organization_name));
  const email = input("Email", contactPoint(contact.points, "email"));
  const phone = input("Phone", contactPoint(contact.points, "phone"));
  const categories = input("Categories (comma separated)", safeJsonArray(contact.categories_json).join(", "));
  const importance = input("Orbit importance (0 to 5)", text(contact.importance, "0"));
  importance.input.type = "number";
  importance.input.min = "0";
  importance.input.max = "5";
  importance.input.step = "1";
  const notes = textarea("Notes", text(contact.notes), "short-textarea");
  panel.append(name.label, organization.label, email.label, phone.label, categories.label, importance.label, notes.label);
  const points = el("div", "contact-points");
  (contact.points || []).forEach((point) => points.append(el("p", "", `${point.kind}: ${point.original_value || point.value}`)));
  panel.append(points);
  const back = el("button", "ghost-button", "Back to contacts");
  back.type = "button";
  back.addEventListener("click", () => loadContacts(state.contactsQuery, state.contactsOffset));
  const save = el("button", "inline-action", "Save contact");
  save.type = "button";
  save.addEventListener("click", () => runAction(save, async () => {
    await api(`/api/contacts/${encodeURIComponent(contactId)}/update`, {
      method: "POST",
      body: JSON.stringify({ fields: contactFields(name, organization, email, phone, categories, notes) }),
    });
    await api(`/api/contacts/${encodeURIComponent(contactId)}/importance`, {
      method: "POST",
      body: JSON.stringify({ importance: Number(importance.input.value) }),
    });
    await openContact(contactId);
  }));
  const remove = el("button", "contact-delete", "Archive contact");
  remove.type = "button";
  remove.addEventListener("click", () => {
    if (!window.confirm(`Archive ${text(contact.full_name, contactId)} in contacts-mcp?`)) return;
    runAction(remove, async () => {
      await api(`/api/contacts/${encodeURIComponent(contactId)}/delete`, { method: "POST" });
      await loadContacts();
    });
  });
  panel.append(save, remove, back);
  list.append(panel);
}

function openNewContact() {
  const list = document.getElementById("contactsList");
  list.replaceChildren();
  const panel = el("section", "contact-detail");
  panel.append(el("p", "eyebrow", "new contact"));
  panel.append(el("h2", "", "Create in contacts-mcp"));
  const name = input("Full name", "");
  const organization = input("Organization", "");
  const email = input("Email", "");
  const phone = input("Phone", "");
  const categories = input("Categories (comma separated)", "");
  const notes = textarea("Notes", "", "short-textarea");
  const create = el("button", "inline-action", "Create contact");
  create.type = "button";
  create.addEventListener("click", () => runAction(create, async () => {
    await api("/api/contacts/create", {
      method: "POST",
      body: JSON.stringify({ fields: contactFields(name, organization, email, phone, categories, notes) }),
    });
    await loadContacts();
  }));
  panel.append(name.label, organization.label, email.label, phone.label, categories.label, notes.label, create);
  list.append(panel);
}

function contactFields(name, organization, email, phone, categories, notes) {
  const fields = {
    fullName: name.input.value.trim(),
    notes: notes.input.value,
    categories: categories.input.value.split(",").map((item) => item.trim()).filter(Boolean),
  };
  if (organization.input.value.trim()) fields.organization = { name: organization.input.value.trim() };
  if (email.input.value.trim()) fields.emails = [{ value: email.input.value.trim(), primary: true }];
  if (phone.input.value.trim()) fields.phones = [{ value: phone.input.value.trim(), primary: true }];
  return fields;
}

function contactPoint(points, kind) {
  const point = (points || []).find((item) => item.kind === kind);
  return point ? text(point.original_value || point.value) : "";
}

function safeJsonArray(value) {
  try {
    const parsed = JSON.parse(text(value, "[]"));
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

async function openChat(row) {
  const sameChat = Number(state.selectedRow?.chat_id) === Number(row.chat_id);
  state.selectedRow = row;
  if (!sameChat) state.chatPanel = "context";
  const chatId = row.chat_id;
  if (!chatId) return;
  document.body.classList.add("chat-selected");
  renderChatLoading(row);
  try {
    const [payload, contacts] = await Promise.all([
      api(`/api/chats/${chatId}/messages?limit=40`),
      api("/api/contacts?limit=100"),
    ]);
    payload.available_contacts = contacts;
    state.selectedChat = payload;
    renderChat(payload, row);
  } catch (error) {
    showError(error.message);
  }
}

function renderChatLoading(row) {
  const pane = document.getElementById("detailPane");
  pane.replaceChildren();
  const box = el("div", "empty-state");
  box.append(el("p", "eyebrow", "opening conversation"));
  box.append(el("h2", "", nameOf(row)));
  box.append(el("p", "", "Loading the recent thread and relationship context."));
  pane.append(box);
}

function closeWorkbench() {
  document.body.classList.remove("chat-selected");
  state.selectedRow = null;
  state.selectedChat = null;
  state.chatPanel = "messages";
  const pane = document.getElementById("detailPane");
  pane.replaceChildren();
  const box = el("div", "empty-state");
  box.append(el("p", "eyebrow", "selected chat"));
  box.append(el("h2", "", "Pick a signal"));
  pane.append(box);
}

function renderChat(payload, row) {
  const pane = document.getElementById("detailPane");
  pane.replaceChildren();
  pane.classList.remove("tray-zap");
  void pane.offsetHeight;
  pane.classList.add("tray-zap");
  const wrap = el("div", "chat-detail");
  const head = el("div", "chat-head");
  const close = el("button", "close-workbench", "Close");
  close.type = "button";
  close.addEventListener("click", () => {
    const orbit = state.orbit;
    if (orbit) releaseFocusedSignal(orbit);
    else closeWorkbench();
  });
  head.append(close);
  head.append(el("p", "eyebrow", `chat ${text(row.chat_id)}`));
  head.append(el("h2", "", nameOf(row)));
  head.append(el("p", "", snippet(row.last_text || "", 180)));
  const panelSwitch = el("div", "chat-panel-switch");
  [["context", "Context"], ["messages", "Messages"]].forEach(([panel, label]) => {
    const button = el("button", "", label);
    button.type = "button";
    button.classList.toggle("active", state.chatPanel === panel);
    button.addEventListener("click", () => switchChatPanel(panel));
    panelSwitch.append(button);
  });
  head.append(panelSwitch);
  wrap.append(head);

  const workspace = el("div", "chat-workspace");
  workspace.dataset.panel = state.chatPanel;
  const contextPane = el("section", "chat-pane context-pane");
  contextPane.append(renderContactBox(payload, row), renderContextBox(payload, row));
  const messagesPane = el("section", "chat-pane messages-pane");
  workspace.append(contextPane, messagesPane);

  if (row.draft_uuid && row.proposed_text && row.draft_status !== "outbox" && row.draft_status !== "sent") {
    messagesPane.append(renderDraftBox(row));
  } else if (row.draft_status === "missing" && row.message_rowid) {
    messagesPane.append(renderMissingDraftBox(row));
  } else if (row.draft_status) {
    const statusBox = el("div", "draft-box");
    statusBox.append(el("p", "eyebrow", "reply state"));
    statusBox.append(el("p", "", `This item is currently ${row.draft_status}.`));
    messagesPane.append(statusBox);
  }

  const timeline = el("div", "timeline");
  if (payload.has_more && payload.next_before_rowid) {
    const older = el("button", "load-older", "Load older messages");
    older.type = "button";
    older.addEventListener("click", () => runAction(older, () => loadOlderMessages(row, payload)));
    timeline.append(older);
  }
  (payload.messages || []).forEach((message) => {
    const rowNode = el("div", `message-row ${message.is_from_me ? "mine" : "theirs"}`);
    rowNode.append(el("div", "sender-label", senderLabel(message, payload, row)));
    const bubble = el("div", "message-bubble");
    bubble.append(el("div", "", text(message.text, "(no text)")));
    bubble.append(el("span", "time", when(message.message_at)));
    rowNode.append(bubble);
    timeline.append(rowNode);
  });
  messagesPane.append(timeline);
  wrap.append(workspace);
  pane.append(wrap);
}

function switchChatPanel(panel) {
  if (!state.selectedChat || !state.selectedRow || !["context", "messages"].includes(panel)) return;
  state.chatPanel = panel;
  renderChat(state.selectedChat, state.selectedRow);
}

async function loadOlderMessages(row, payload) {
  const older = await api(
    `/api/chats/${row.chat_id}/messages?limit=40&before_rowid=${encodeURIComponent(payload.next_before_rowid)}`,
  );
  payload.messages = [...(older.messages || []), ...(payload.messages || [])];
  payload.has_more = older.has_more;
  payload.next_before_rowid = older.next_before_rowid;
  state.selectedChat = payload;
  renderChat(payload, row);
}

function renderContactBox(payload, row) {
  const box = el("div", "context-box");
  const linked = payload.contacts || [];
  const matched = linked.map((contact) => text(contact.full_name)).filter(Boolean).join(", ") || text(row.contacts);
  box.append(el("p", "eyebrow", matched ? "contact" : "contact review"));
  box.append(el("p", "", matched || "No synced contact matches this conversation yet."));
  linked.forEach((contact) => {
    if (!contact.photo_data_uri) return;
    const image = document.createElement("img");
    image.className = "contact-avatar";
    image.src = contact.photo_data_uri;
    image.alt = "";
    box.append(image);
  });
  if (matched) {
    linked.forEach((contact) => {
      if (!contact.manual) return;
      const unlink = el("button", "inline-action", "Unlink contact");
      unlink.type = "button";
      unlink.addEventListener("click", () => runAction(unlink, async () => {
        await api("/api/contacts/unlink", { method: "POST", body: JSON.stringify({ chat_id: Number(row.chat_id), contact_id: contact.contact_id }) });
        await openChat(row);
      }));
      box.append(unlink);
    });
    return box;
  }
  const select = document.createElement("select");
  select.className = "contact-select";
  select.append(new Option("Link a synced contact", ""));
  (payload.available_contacts || []).forEach((contact) => {
    select.append(new Option(text(contact.full_name, contact.contact_id), contact.contact_id));
  });
  const link = el("button", "inline-action", "Link contact");
  link.type = "button";
  link.addEventListener("click", () => runAction(link, async () => {
    if (!select.value) throw new Error("Choose a synced contact first.");
    await api("/api/contacts/link", { method: "POST", body: JSON.stringify({ chat_id: Number(row.chat_id), contact_id: select.value }) });
    await openChat(row);
  }));
  box.append(select, link);
  const actions = el("div", "draft-actions contact-actions");
  [["keep_local", "Keep local"], ["prepare_contact", "Prepare contact"], ["ignore_spam", "Ignore / spam"]]
    .forEach(([decision, label]) => {
      const button = el("button", "", label);
      button.type = "button";
      button.addEventListener("click", () => runAction(button, async () => {
        await api("/api/contacts/review", {
          method: "POST",
          body: JSON.stringify({ chat_id: Number(row.chat_id), decision }),
        });
        if (decision === "ignore_spam") {
          state.chatPanel = "context";
          await openChat(row);
        }
      }));
      actions.append(button);
    });
  box.append(actions);
  return box;
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
  const name = input("Conversation name", context.name || text(payload.chat && payload.chat.name));
  const participants = input("Recipients (me excluded)", (payload.recipients || context.participants || []).join(", "));
  const model = input("Draft model", context.model || "");
  const agentNotes = textarea("Agent notes", context.agent_notes || "", "short-textarea");
  const professional = checkbox("Professional", Boolean(context.professional));
  const autoApprove = checkbox("Auto approve", Boolean(context.auto_approve));
  const doNotDraft = checkbox("Do not draft", Boolean(context.do_not_draft));
  const favorite = checkbox("Favorite", Boolean(context.favorite));
  const notes = textarea("Notes", payload.notes || "", "context-notes");

  box.append(name.label, participants.label, relationship.label, tone.label, model.label, agentNotes.label);
  const toggles = el("div", "toggle-row");
  toggles.append(professional.label, autoApprove.label, doNotDraft.label, favorite.label);
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
          name: name.input.value,
          participants: participants.input.value.split(",").map((item) => item.trim()).filter(Boolean),
          model: model.input.value || null,
          agent_notes: agentNotes.input.value,
          professional: professional.input.checked,
          auto_approve: autoApprove.input.checked,
          do_not_draft: doNotDraft.input.checked,
          favorite: favorite.input.checked,
        },
        notes: notes.input.value,
      }),
    });
    state.chatPanel = "messages";
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
  const archive = el("button", "", "Archive");
  const reject = el("button", "", "Reject");
  [approve, save, archive, reject].forEach((button) => {
    button.type = "button";
    actions.append(button);
  });
  approve.addEventListener("click", () => runAction(approve, () => approveDraft(row.draft_uuid, draft.input.value)));
  save.addEventListener("click", () => runAction(save, () => saveDraft(row.draft_uuid, draft.input.value)));
  archive.addEventListener("click", () => runAction(archive, () => archiveDraft(row.draft_uuid)));
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

async function archiveDraft(uuid) {
  await api(`/api/drafts/${encodeURIComponent(uuid)}/archive`, { method: "POST" });
  await refreshAfterMutation("Draft archived. It is preserved outside the default review queue.");
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
  closeWorkbench();
  const pane = document.getElementById("detailPane");
  pane.replaceChildren();
  const box = el("div", "empty-state");
  box.append(el("p", "eyebrow", "done"));
  box.append(el("h2", "", "Updated"));
  box.append(el("p", "", message));
  pane.append(box);
}

async function openSettings() {
  closeWorkbench();
  setActiveView("settings");
  await renderSettings();
}

async function renderSettings() {
  const [profile, preferences] = await Promise.all([api("/api/operator"), api("/api/preferences")]);
  state.operator = profile;
  state.preferences = preferences;
  const content = document.getElementById("settingsContent");
  content.replaceChildren();

  const identity = profile.identity || {};
  const identitySection = el("section", "settings-section");
  identitySection.append(el("p", "eyebrow", "operator identity"), el("h2", "", "Who am I?"));
  const name = input("Name", text(identity.name, text(profile.name, "Me")));
  const aliases = input("My addresses and handles (comma separated)", (identity.aliases || profile.aliases || []).join(", "));
  const vcard = input("vCard photo fallback", text(profile.vcard_path));
  const picker = el("div", "contact-picker");
  const filter = input("Filter contact cards", "");
  filter.input.placeholder = "Type any part of a name or organization";
  const contact = document.createElement("select");
  contact.className = "contact-select";
  contact.size = 8;
  const contactLabel = el("label", "field", "Matching contact cards");
  contactLabel.append(contact);
  const card = el("div", "operator-contact-card");
  const pickerPager = el("div", "");
  const pickerStatus = el("p", "picker-status");
  let selectedContactId = text(profile.contact_id);
  let contactPage = { items: [] };
  let filterTimer = 0;

  const renderCard = () => {
    card.replaceChildren();
    const selected = (contactPage.items || []).find((entry) => entry.contact_id === selectedContactId)
      || (profile.contact && profile.contact.contact_id === selectedContactId ? profile.contact : null);
    if (!selected) {
      card.append(el("p", "", "No contact card selected."));
      return;
    }
    if (selected.photo_data_uri) {
      const image = document.createElement("img");
      image.className = "contact-avatar";
      image.src = selected.photo_data_uri;
      image.alt = "";
      card.append(image);
    }
    const copy = el("div", "");
    copy.append(el("strong", "", text(selected.full_name, selected.contact_id)));
    if (selected.organization_name) copy.append(el("span", "", selected.organization_name));
    card.append(copy);
  };

  const loadContactPicker = async (offset = 0) => {
    contactPage = await api(`/api/contacts/page?limit=40&offset=${offset}&q=${encodeURIComponent(filter.input.value.trim())}`);
    contact.replaceChildren(new Option("No linked contact card", ""));
    (contactPage.items || []).forEach((entry) => {
      const organization = text(entry.organization_name);
      contact.append(new Option(
        organization ? `${text(entry.full_name, entry.contact_id)} - ${organization}` : text(entry.full_name, entry.contact_id),
        entry.contact_id,
      ));
    });
    if (selectedContactId && ![...contact.options].some((option) => option.value === selectedContactId)) {
      const linked = profile.contact || {};
      contact.append(new Option(text(linked.full_name, selectedContactId), selectedContactId));
    }
    contact.value = selectedContactId;
    pickerPager.replaceChildren(renderPagination(contactPage, loadContactPicker));
    pickerStatus.textContent = contactPage.total
      ? `${contactPage.total} matching contact cards`
      : "No contact cards match this filter.";
    renderCard();
  };

  contact.addEventListener("change", () => {
    selectedContactId = contact.value;
    renderCard();
  });
  filter.input.addEventListener("input", () => {
    window.clearTimeout(filterTimer);
    filterTimer = window.setTimeout(() => loadContactPicker(0), 180);
  });
  const saveIdentity = el("button", "inline-action", "Save identity");
  saveIdentity.type = "button";
  saveIdentity.addEventListener("click", () => runAction(saveIdentity, async () => {
    await api("/api/operator", {
      method: "POST",
      body: JSON.stringify({ fields: {
        name: name.input.value,
        vcard_path: vcard.input.value,
        contact_id: selectedContactId,
        aliases: aliases.input.value.split(",").map((item) => item.trim()).filter(Boolean),
      } }),
    });
    await loadOverview();
    await renderSettings();
  }));
  picker.append(filter.label, contactLabel, pickerPager, pickerStatus);
  identitySection.append(name.label, aliases.label, vcard.label, picker, card, saveIdentity);

  const queueSection = el("section", "settings-section");
  queueSection.append(el("p", "eyebrow", "queue rules"), el("h2", "", "Pending drafts"));
  const days = input("Show the last N days (0 is all history)", String(preferences.pending_days ?? 7));
  const relationships = input("Connection types (comma separated)", (preferences.relationship_types || []).join(", "));
  const grouped = checkbox("Group by connection type", preferences.group_pending_by_relationship !== false);
  const archived = checkbox("Show archived drafts", Boolean(preferences.show_archived_drafts));
  const saveQueue = el("button", "inline-action", "Save queue settings");
  saveQueue.type = "button";
  saveQueue.addEventListener("click", () => runAction(saveQueue, async () => {
    await api("/api/preferences", {
      method: "POST",
      body: JSON.stringify({ fields: {
        pending_days: Number(days.input.value),
        relationship_types: relationships.input.value.split(",").map((item) => item.trim()).filter(Boolean),
        group_pending_by_relationship: grouped.input.checked,
        show_archived_drafts: archived.input.checked,
      } }),
    });
    await loadOverview();
    await renderSettings();
  }));
  queueSection.append(days.label, relationships.label, grouped.label, archived.label, saveQueue);
  content.append(identitySection, queueSection);
  await loadContactPicker();
}

function openOperatorProfile() {
  return openSettings();
}

function openPreferences() {
  return openSettings();
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
document.querySelectorAll("[data-orbit-direction]").forEach((button) => {
  button.addEventListener("click", () => {
    state.orbitDirection = button.dataset.orbitDirection;
    state.orbitOffset = 0;
    document.querySelectorAll("[data-orbit-direction]").forEach((item) => {
      item.classList.toggle("active", item.dataset.orbitDirection === state.orbitDirection);
    });
    loadOrbit().catch((error) => showError(error.message));
  });
});
document.getElementById("orbitWindow").addEventListener("change", (event) => {
  state.orbitDays = Number(event.currentTarget.value);
  state.orbitOffset = 0;
  loadOrbit().catch((error) => showError(error.message));
});
document.getElementById("operatorButton").addEventListener("click", () => openOperatorProfile().catch((error) => showError(error.message)));
document.getElementById("preferencesButton").addEventListener("click", () => openPreferences().catch((error) => showError(error.message)));
document.getElementById("opsRefreshButton").addEventListener("click", (event) => {
  runAction(event.currentTarget, loadOps);
});
document.getElementById("searchForm").addEventListener("submit", (event) => {
  runAction(event.submitter, () => runSearch(event));
});
document.getElementById("contactsSearchForm").addEventListener("submit", (event) => {
  event.preventDefault();
  runAction(event.submitter, () => loadContacts(document.getElementById("contactsSearchInput").value.trim(), 0));
});
document.getElementById("contactsSyncButton").addEventListener("click", (event) => {
  runAction(event.currentTarget, async () => {
    await api("/api/contacts/sync", { method: "POST" });
    await loadContacts(state.contactsQuery, 0);
  });
});
document.getElementById("contactsNewButton").addEventListener("click", () => openNewContact());
document.querySelectorAll(".rail-button").forEach((button) => {
  button.addEventListener("click", () => runAction(button, async () => {
    const view = button.dataset.view;
    setActiveView(view);
    if (view === "overview") await loadOverview();
    if (["pending", "attention", "recent", "views", "issues"].includes(view)) await loadList(view);
    if (view === "contacts") await loadContacts();
    if (view === "ops") await loadOps();
  }));
});

initSkinControls();
loadOverview().catch((error) => showError(error.message));
