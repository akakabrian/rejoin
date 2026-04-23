let selectedSessionId = null;

function fmtAge(seconds) {
  if (seconds == null) return "never indexed";
  const s = Math.round(seconds);
  if (s < 60)  return `indexed ${s}s ago`;
  if (s < 3600) return `indexed ${Math.round(s / 60)}m ago`;
  return `indexed ${Math.round(s / 3600)}h ago`;
}

async function refreshAge() {
  try {
    const r = await fetch("/status");
    const data = await r.json();
    const el = document.getElementById("refresh-age");
    if (el) el.textContent = fmtAge(data.last_indexed_age_s);
  } catch { /* ignore */ }
}

async function triggerReindex() {
  const btn = document.getElementById("refresh-btn");
  if (!btn || btn.disabled) return;
  btn.disabled = true;
  btn.classList.add("spinning");
  try {
    await fetch("/reindex?titles=true", { method: "POST" });
    await refreshAge();
    const form = document.getElementById("filters");
    if (form) htmx.trigger(form, "submit");
  } finally {
    setTimeout(() => btn.classList.remove("spinning"), 400);
    btn.disabled = false;
  }
}

refreshAge();
setInterval(refreshAge, 10000);

function allRows() {
  return Array.from(document.querySelectorAll(".session-row"));
}

function markSelected(row, { scroll = true } = {}) {
  document.querySelectorAll(".session-row.selected").forEach(r => r.classList.remove("selected"));
  if (!row) return;
  row.classList.add("selected");
  selectedSessionId = row.dataset.sessionId;
  if (scroll) row.scrollIntoView({ block: "nearest" });
}

function moveSelection(delta) {
  const rows = allRows();
  if (!rows.length) return;
  const cur = rows.findIndex(r => r.dataset.sessionId === selectedSessionId);
  const next = rows[Math.max(0, Math.min(rows.length - 1, (cur === -1 ? 0 : cur + delta)))];
  if (next) {
    markSelected(next);
    htmx.trigger(next, "click");
  }
}

async function copyText(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
    const prev = btn.textContent;
    btn.textContent = "copied ✓";
    setTimeout(() => { btn.textContent = prev; }, 1200);
  } catch (e) {
    alert("copy failed: " + e);
  }
}

async function togglePin(sessionId, btn) {
  const r = await fetch(`/session/${sessionId}/pin`, { method: "POST" });
  const data = await r.json();
  if (btn) {
    btn.classList.toggle("pinned", data.pinned);
    btn.title = data.pinned ? "unpin" : "pin";
  }
  const form = document.getElementById("filters");
  if (form) htmx.trigger(form, "submit");
}

function resumeSelected() {
  const btn = document.getElementById("resume-btn");
  if (btn && !btn.disabled) btn.click();
}

document.body.addEventListener("htmx:afterSwap", (e) => {
  if (e.target.id === "session-list" && selectedSessionId) {
    const row = document.querySelector(`.session-row[data-session-id="${selectedSessionId}"]`);
    if (row) row.classList.add("selected");
  }
});

document.body.addEventListener("click", async (e) => {
  if (e.target.id === "refresh-btn") {
    triggerReindex();
    return;
  }

  // expand/collapse long turn body
  const expandBtn = e.target.closest(".expand-btn");
  if (expandBtn) {
    e.stopPropagation();
    const body = expandBtn.previousElementSibling;
    if (body && body.classList.contains("turn-body")) {
      const nowExpanded = body.classList.toggle("expanded");
      body.classList.toggle("clipped", !nowExpanded);
      expandBtn.classList.toggle("is-expanded", nowExpanded);
      expandBtn.textContent = nowExpanded ? "collapse" : expandBtn.dataset.label;
    }
    return;
  }

  // pin button inside detail
  const pin = e.target.closest(".pin-btn");
  if (pin) {
    e.stopPropagation();
    togglePin(pin.dataset.sessionId, pin);
    return;
  }
  // row selection
  const row = e.target.closest(".session-row");
  if (row) markSelected(row, { scroll: false });

  // resume button
  if (e.target.id === "resume-btn") {
    const url = e.target.dataset.url;
    e.target.disabled = true;
    e.target.textContent = "launching…";
    try {
      const r = await fetch(url, { method: "POST" });
      const data = await r.json();
      const out = document.getElementById("resume-result");
      if (data.error) {
        out.innerHTML = `<span style="color:#e88">error: ${data.error}</span>`;
      } else {
        const verb = data.created ? "started" : "already running";
        out.innerHTML = `tmux session <code>${data.tmux_name}</code> ${verb}. attach: <code>${data.attach}</code>`;
      }
    } finally {
      e.target.disabled = false;
      e.target.textContent = "rejoin in tmux";
    }
  }
});

// keyboard navigation
document.addEventListener("keydown", (e) => {
  const target = e.target;
  const typing = target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable);

  // "/" focuses search even while not typing
  if (e.key === "/" && !typing) {
    e.preventDefault();
    const s = document.querySelector("input[name=q]");
    if (s) { s.focus(); s.select(); }
    return;
  }
  // Escape blurs inputs
  if (e.key === "Escape" && typing) {
    target.blur();
    return;
  }
  if (typing) return;

  if (e.key === "j" || e.key === "ArrowDown") { e.preventDefault(); moveSelection(1); }
  else if (e.key === "k" || e.key === "ArrowUp") { e.preventDefault(); moveSelection(-1); }
  else if (e.key === "Enter") { e.preventDefault(); resumeSelected(); }
  else if (e.key === "p") {
    const sid = selectedSessionId || document.querySelector(".session-row.selected")?.dataset.sessionId;
    if (!sid) return;
    const pin = document.querySelector(`#detail-pane .pin-btn[data-session-id="${sid}"]`)
      || document.querySelector(`.session-row[data-session-id="${sid}"] .pin-btn[data-session-id="${sid}"]`);
    e.preventDefault();
    togglePin(sid, pin);
  }
  else if (e.key === "g") {
    const rows = allRows();
    if (rows.length) { markSelected(rows[0]); htmx.trigger(rows[0], "click"); }
  }
});
