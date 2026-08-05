/* FRIDAY V4 — Companion PWA.
   The phone is another surface of the SAME Friday: pairing is
   consent-first (one-time code), talk flows through the one NLU brain
   (/api/talk), the thread is the shared one-presence session
   (/api/conversation), and live events stream over SSE /api/events
   with a durable replay cursor — a phone that was closed misses
   nothing when it reconnects.
   Never crashes: every fetch is guarded and the UI degrades to a
   readable state when the server is unreachable. */
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = {
  get: (k, d) => { try { return localStorage.getItem("friday." + k) ?? d; } catch (e) { return d; } },
  set: (k, v) => { try { localStorage.setItem("friday." + k, v); } catch (e) {} },
  del: (k) => { try { localStorage.removeItem("friday." + k); } catch (e) {} },
};
const deviceToken = () => {
  let t = store.get("device_token", "");
  if (!t) { t = "pwa-" + (crypto.randomUUID ? crypto.randomUUID() : Date.now() + "-" + Math.random().toString(36).slice(2, 10)); store.set("device_token", t); }
  return t;
};

// The companion token (set on the Status tab, stored locally) is sent
// as Bearer auth on every API call. The server is open on the LAN by
// default (no token needed); once you expose Friday over a public
// tunnel, run `friday4 mobile serve --token <secret>` and enter the
// same secret here — the API (the power) stays gated, the PWA shell
// stays public.
function authHeaders(extra) {
  const t = store.get("token", "");
  const h = Object.assign({ "Content-Type": "application/json" }, extra || {});
  if (t) h.Authorization = "Bearer " + t;
  return h;
}
async function api(path, opts) {
  const r = await fetch(path, Object.assign({ headers: authHeaders(), cache: "no-store" }, opts || {}));
  const body = await r.json().catch(() => ({}));
  return { ok: r.ok, status: r.status, body };
}
const post = (path, data) => api(path, { method: "POST", body: JSON.stringify(data) });
const get = (path) => api(path, { method: "GET" });

/* ── tabs ───────────────────────────────────────────────────── */
let currentTab = "chat";
const unread = { count: 0, shown: 0 };
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => switchTab(tab.dataset.screen));
});
function switchTab(name) {
  currentTab = name;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.screen === name));
  document.querySelectorAll(".screen").forEach((s) => s.classList.add("hidden"));
  $("scr-" + name).classList.remove("hidden");
  if (name === "feed") { unread.count = 0; renderBadge(); }
  if (name === "status") refreshStatus();
  if (name === "device") renderDevice();
}
function renderBadge() {
  const b = $("feedBadge");
  b.classList.toggle("hidden", unread.count === 0);
  b.textContent = unread.count > 99 ? "99+" : String(unread.count);
}

/* ── connection pill ────────────────────────────────────────── */
function setConn(ok, label) {
  const p = $("connPill");
  p.className = "pill " + (ok ? "ok" : "bad");
  p.innerHTML = `<span class="dot"></span>${esc(label)}`;
}

/* ── chat (one presence) ────────────────────────────────────── */
function chatLine(role, text, time) {
  const li = document.createElement("li");
  li.className = role;
  let who = role === "you" ? "You" : "Friday";
  // CLAUDE: messages open Friday's agent session — label both sides so
  // the operator sees the conversation handed to Claude Code.
  if (String(text || "").trim().toUpperCase().startsWith("CLAUDE:") && role === "you") {
    li.className += " agent";
    who = "You → Claude";
  }
  const t = time ? `<span class="t">${esc(time)}</span>` : "";
  li.innerHTML = `<span class="who">${who}</span>${esc(text)}${t}`;
  return li;
}
async function hydrateChat() {
  const log = $("chatlog");
  try {
    const r = await get("/api/conversation");
    const ex = r.body.exchanges || [];
    for (const e of ex) {
      const text = String(e.content || "").trim();
      if (!text) continue;
      log.appendChild(chatLine(e.role === "user" ? "you" : "fri", text, fmtTime(e.created_at)));
    }
    if (ex.length) {
      const note = document.createElement("li");
      note.className = "note";
      note.textContent = "↳ resuming today's shared conversation";
      log.appendChild(note);
    } else {
      const g = document.createElement("li");
      g.className = "fri";
      g.innerHTML = `<span class="who">Friday</span>I'm your FRIDAY companion — the same presence as your terminal, dashboard, and voice. I can run tests, check git, start missions, and answer questions.`;
      log.appendChild(g);
    }
  } catch (e) {
    const g = document.createElement("li");
    g.className = "fri";
    g.innerHTML = `<span class="who">Friday</span>Can't reach the companion server yet — is \`friday4 mobile serve\` running?`;
    log.appendChild(g);
  }
  log.scrollTop = log.scrollHeight;
}
function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
// A Friday ask ("May I clone X?") gets inline Yes/No buttons — the
// durable permission ask's request_id rides in the talk reply, so the
// phone answers it the same way it asked: through /api/talk.
function askBubble(response, requestId, time) {
  const li = document.createElement("li");
  li.className = "fri ask";
  li.innerHTML = `<span class="who">Friday</span>${esc(response || "")}<span class="t">${time || ""}</span>` +
    `<div class="ask-btns"><button class="ask-yes">✓ Yes</button>` +
    `<button class="ask-no">✕ No</button></div>`;
  li.querySelector(".ask-yes").addEventListener("click", () => postText("yes, run it"));
  li.querySelector(".ask-no").addEventListener("click", () => postText("no"));
  return li;
}
async function postText(text) {
  // Send one utterance and append Friday's reply to the chat log.
  const log = $("chatlog");
  const wait = document.createElement("li");
  wait.className = "fri typing";
  wait.textContent = "…";
  log.appendChild(wait);
  log.scrollTop = log.scrollHeight;
  try {
    const r = await post("/api/talk", { text });
    wait.className = "fri";
    const body = r.body || {};
    if (body.action === "asked" && body.request_id) {
      const ask = askBubble(body.response, body.request_id, fmtTime(new Date().toISOString()));
      wait.replaceWith(ask);
    } else {
      const isAgent = body.intent === "agent";
      if (isAgent) wait.className += " agent";
      const who = isAgent ? "Claude Code" : "Friday";
      wait.innerHTML = `<span class="who">${who}</span>${esc(body.response || "(no response)")}<span class="t">${fmtTime(new Date().toISOString())}</span>`;
    }
  } catch (e) {
    wait.className = "fri";
    wait.innerHTML = `<span class="who">Friday</span>could not reach the companion server`;
  }
  log.scrollTop = log.scrollHeight;
}
async function sendChat() {
  const input = $("chatInput");
  const btn = $("chatBtn");
  const text = (input.value || "").trim();
  if (!text) return;
  input.value = "";
  const log = $("chatlog");
  log.appendChild(chatLine("you", text, fmtTime(new Date().toISOString())));
  btn.disabled = true;
  await postText(text);
  btn.disabled = false;
  input.focus();
}
$("chatBtn").addEventListener("click", sendChat);
$("chatInput").addEventListener("keydown", (e) => { if (e.key === "Enter") sendChat(); });
if ($("tokenBtn")) $("tokenBtn").addEventListener("click", saveToken);
if ($("tokenInput")) $("tokenInput").addEventListener("keydown", (e) => { if (e.key === "Enter") saveToken(); });

/* ── live feed (SSE, durable replay) ────────────────────────── */
function feedItem(ev) {
  const li = document.createElement("li");
  const pri = Number(ev.priority || 0);
  if (pri >= 2) li.className = "p3";
  else if (pri >= 1) li.className = "p2";
  const topic = esc(ev.topic || "system");
  const t = ev.created_at ? fmtTime(ev.created_at) : fmtTime(new Date().toISOString());
  li.innerHTML = `<span class="t">${t}</span><b>${topic}</b> — ${esc(ev.payload || "")}`;
  return li;
}
function toast(text, cls) {
  let t = $("toast");
  if (!t) {
    t = document.createElement("div");
    t.id = "toast";
    document.body.appendChild(t);
  }
  t.textContent = text;
  t.className = "show";
  clearTimeout(t._h);
  t._h = setTimeout(() => { t.className = ""; }, 4200);
}
function startFeed() {
  if (!window.EventSource) { $("feedState").textContent = "polling unavailable — refresh for updates"; return; }
  // Mutable cursor, NOT const: reconnect must re-request with the
  // LATEST seen id so the server replays only what this client
  // missed. (EventSource auto-reconnect would re-open the original
  // URL and replay everything since boot — duplicate feed items.)
  let cursor = Number(store.get("feed_cursor", "0")) || 0;
  function connect() {
    // EventSource can't set headers, so the token rides as a query
    // param (the API accepts ?token= too) — same secret, same gate.
    const tok = store.get("token", "");
    const es = new EventSource("/api/events?since=" + cursor +
                               (tok ? "&token=" + encodeURIComponent(tok) : ""));
    $("feedState").textContent = "subscribed · cursor " + cursor;
    es.onopen = () => { $("feedState").textContent = "live"; };
    es.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        const id = Number(data.id || 0);
        if (id > cursor) { cursor = id; store.set("feed_cursor", String(id)); }
        const feed = $("feed");
        feed.insertBefore(feedItem(data), feed.firstChild);
        const pri = Number(data.priority || 0);
        if (pri >= 1) {
          unread.count += 1;
          renderBadge();
          if (currentTab !== "feed") toast(`◈ ${data.topic || "event"} · ${data.payload || ""}`);
        }
        while (feed.children.length > 80) feed.removeChild(feed.lastChild);
      } catch (e) { /* malformed event — ignore */ }
    };
    es.onerror = () => {
      $("feedState").textContent = "reconnecting…";
      // Manual reconnect with the current cursor — never duplicates.
      es.close();
      setTimeout(connect, 3000);
    };
  }
  connect();
}

/* ── status ─────────────────────────────────────────────────── */
function row(k, v) { return `<div class="row"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`; }
function saveToken() {
  const t = ($("tokenInput") || {}).value || "";
  store.set("token", t.trim());
  toast(t.trim() ? "Token saved — API gated" : "Token cleared — API open (LAN)", "ok");
  refreshStatus();
}
async function refreshStatus() {
  const tok = store.get("token", "");
  const tokInput = $("tokenInput");
  if (tokInput && tokInput.value !== tok) tokInput.value = tok;
  try {
    const r = await get("/api/status");
    const b = r.body || {};
    if (r.status === 401) {
      $("statusRows").innerHTML = row("transport", "needs token") +
        row("hint", "Enter the companion token below and save it");
      $("presenceNote").textContent = "This Friday is token-gated (it's exposed " +
        "beyond the LAN). Enter the token to connect.";
      return;
    }
    const sess = b.shared_session || {};
    const rows = row("transport", b.available ? "online" : "offline") +
                 row("shared session", sess.id ? "active" : "—") +
                 row("surface started on", sess.surface || "—") +
                 row("exchanges today", b.exchanges_today ?? "—") +
                 row("this device", paired() ? "paired" : "not paired");
    $("statusRows").innerHTML = rows;
    const start = sess.started_at ? new Date(sess.started_at) : null;
    $("presenceNote").textContent = sess.id
      ? `One shared thread since ${start && !isNaN(start) ? start.toLocaleString() : "…"} — anything you say here, in the terminal, or on the dashboard is the same conversation.`
      : "No conversation yet — say something in Chat and Friday will open the shared thread.";
  } catch (e) {
    $("statusRows").innerHTML = row("transport", "unreachable");
    $("presenceNote").textContent = "Could not reach the companion server.";
  }
}

/* ── device / pairing ───────────────────────────────────────── */
const paired = () => !!store.get("device_id", "");
let installEvt = null;
window.addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); installEvt = e; renderDevice(); });
function renderDevice() {
  const body = $("deviceBody");
  if (!paired()) {
    body.innerHTML = `
      <p class="dim">Pair this phone with your Friday. On the computer run:</p>
      <div class="big-code">friday4 mobile pair</div>
      <p class="code-hint">then type the 6-character code below</p>
      <input class="code" id="pairCode" maxlength="6" placeholder="••••••" autocomplete="off" inputmode="text">
      <button class="act" id="pairBtn">PAIR THIS DEVICE</button>
      <div class="msg" id="pairMsg"></div>`;
    $("pairBtn").addEventListener("click", doPair);
    const inp = $("pairCode");
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") doPair(); });
    inp.focus();
  } else {
    const name = store.get("device_name", "this phone");
    body.innerHTML = `
      <p class="dim">Paired as <b>${esc(name)}</b> — this phone is a surface of the same Friday.</p>
      <div class="rows" style="margin-top:10px">
        ${row("device", esc(store.get("device_name", "—")))}
        ${row("id", store.get("device_id", "—"))}
      </div>
      <button class="act mini" id="installBtn" ${installEvt ? "" : "style='display:none'"}>⤓ INSTALL FRIDAY ON THIS PHONE</button>
      <button class="act danger" id="unpairBtn">UNPAIR THIS PHONE</button>
      <div class="msg" id="pairMsg"></div>`;
    if (installEvt) $("installBtn").addEventListener("click", async () => { if (installEvt) { installEvt.prompt(); installEvt = null; renderDevice(); } });
    $("unpairBtn").addEventListener("click", doUnpair);
  }
}
async function doPair() {
  const code = ($("pairCode").value || "").trim().toUpperCase();
  const msg = $("pairMsg");
  if (code.length < 6) { msg.textContent = "Enter the 6-character code."; msg.className = "msg err"; return; }
  msg.textContent = "pairing…"; msg.className = "msg";
  const name = (navigator.platform || "phone") + " " + (navigator.userAgent.match(/Android|iPhone|iPad/)?.[0] || "");
  const r = await post("/api/devices/register", {
    code, token: deviceToken(), platform: "web-pwa", name: name.slice(0, 40),
  });
  if (r.ok && r.body.ok) {
    store.set("device_id", String(r.body.device_id || ""));
    store.set("device_name", name.slice(0, 40));
    msg.textContent = "✔ Paired — Friday is in your pocket."; msg.className = "msg ok";
    renderDevice();
    setConn(true, "paired");
  } else {
    msg.textContent = r.body.error || "Pairing failed — is the code correct and unexpired?"; msg.className = "msg err";
  }
}
async function doUnpair() {
  const id = store.get("device_id", "");
  const msg = $("pairMsg");
  msg.textContent = "unpairing…"; msg.className = "msg";
  if (id) { try { await api("/api/devices/" + encodeURIComponent(id), { method: "DELETE" }); } catch (e) {} }
  store.del("device_id"); store.del("device_name");
  msg.textContent = "Unpaired. This phone will stop receiving pushes."; msg.className = "msg ok";
  renderDevice();
}
async function touchDevice() {
  const id = store.get("device_id", "");
  if (!id) return;
  try { await post("/api/devices/touch", { device_id: id }); } catch (e) {}
}

/* ── boot ───────────────────────────────────────────────────── */
(async function boot() {
  hydrateChat();
  refreshStatus();
  renderDevice();
  startFeed();
  touchDevice();
  setInterval(touchDevice, 5 * 60 * 1000);
  // Connectivity probe: keep the pill honest when the server dies.
  setInterval(async () => {
    try {
      const r = await get("/api/status");
      setConn(r.ok && r.body.available, paired() ? "connected · paired" : "connected");
    } catch (e) { setConn(false, "offline"); }
  }, 15000);
})();
