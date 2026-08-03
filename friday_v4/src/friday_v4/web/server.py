"""Local dashboard server for `friday4 web` — pure-stdlib ``http.server``.

Serves a self-contained HTML dashboard plus a small JSON API:

    GET  /              → the dashboard page
    GET  /api/overview  → aggregated subsystem JSON
    GET  /api/security  → persisted security scan state
    GET  /api/ambient   → recent V3 ambient feed events
    GET  /api/ambient-events → Wave 11 ambient bus events (durable queue)
    GET  /api/briefing  → today's briefing from real V4 state
    GET  /api/projects  → project-directory suggestions for the picker
    POST /api/scan      → trigger a security scan (runs the daemon's
                          SecurityScanner once; persists state). Body may
                          carry {"path": "/abs/or/~/path"} to target a
                          project; invalid paths get a 400.

Design:
- Local-first: binds 127.0.0.1 by default (override with ``--host``).
- No framework, no external JS/CSS (fully inline, works offline).
- Every payload comes from ``dashboard.py``'s guarded accessors, so a
  missing subsystem renders as an empty card instead of a 500.
- Thread-safe: scans run on a daemon thread; accessors are read-only.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import dashboard

logger = logging.getLogger("friday_v4.web.server")

#: Serializes on-demand scans — two concurrent `write_text` calls on the
#: same state file could interleave and corrupt it.
_scan_lock = threading.Lock()

_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FRIDAY V4 — Dashboard</title>
<style>
  :root {
    --bg: #0a0e17; --panel: #111a2c; --panel2: #0d1524;
    --line: #1e2b44; --text: #dfe6f3; --dim: #7c8aa5;
    --cyan: #38d4f5; --green: #3ddc97; --red: #ff5c6c;
    --amber: #ffb454; --yellow: #ffd166;
    --radius: 12px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: radial-gradient(1200px 600px at 20% -10%, #14233f 0%, var(--bg) 55%) fixed;
    color: var(--text);
    font-family: "SF Mono", "JetBrains Mono", ui-monospace, Menlo, Consolas, monospace;
    min-height: 100vh; padding: 24px 28px 40px;
  }
  header { display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; margin-bottom: 22px; }
  .logo { font-size: 24px; font-weight: 700; letter-spacing: 2px; color: var(--cyan); }
  .logo small { color: var(--dim); font-weight: 400; letter-spacing: 4px; font-size: 12px; }
  .pill { font-size: 11px; padding: 4px 12px; border-radius: 999px; border: 1px solid var(--line); color: var(--dim); }
  .pill.running { color: var(--green); border-color: rgba(61,220,151,.4); }
  .pill.stopped { color: var(--red); border-color: rgba(255,92,108,.4); }
  .pill .dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 6px; background: currentColor; animation: pulse 2s infinite; }
  @keyframes pulse { 50% { opacity: .35; } }
  .updated { margin-left: auto; color: var(--dim); font-size: 12px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; }
  .card { background: linear-gradient(180deg, var(--panel), var(--panel2)); border: 1px solid var(--line); border-radius: var(--radius); padding: 16px 18px; }
  .card h2 { font-size: 11px; text-transform: uppercase; letter-spacing: 3px; color: var(--dim); margin-bottom: 12px; display: flex; align-items: center; gap: 8px; }
  .card h2 .led { width: 8px; height: 8px; border-radius: 50%; background: var(--dim); }
  .card h2 .led.ok { background: var(--green); box-shadow: 0 0 8px var(--green); }
  .card h2 .led.warn { background: var(--amber); box-shadow: 0 0 8px var(--amber); }
  .card h2 .led.bad { background: var(--red); box-shadow: 0 0 8px var(--red); }
  .row { display: flex; justify-content: space-between; padding: 4px 0; font-size: 13px; border-bottom: 1px dashed rgba(126,138,165,.12); }
  .row:last-child { border-bottom: none; }
  .row .k { color: var(--dim); }
  .row .v { text-align: right; max-width: 60%; overflow-wrap: anywhere; }
  .grade { font-size: 42px; font-weight: 800; line-height: 1; margin-right: 12px; }
  .grade.A, .grade.B { color: var(--green); }
  .grade.C { color: var(--yellow); }
  .grade.D { color: var(--amber); }
  .grade.F { color: var(--red); }
  .scanbar { display: flex; gap: 8px; align-items: center; margin-top: 12px; }
  .scanbar input {
    flex: 1; min-width: 0; background: var(--panel2); color: var(--text);
    border: 1px solid var(--line); border-radius: 8px; padding: 7px 10px;
    font-family: inherit; font-size: 12px;
  }
  .scanbar input:focus { outline: none; border-color: rgba(56,212,245,.5); }
  .scanbar button { margin-top: 0; }
  .sevs { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 10px; }
  .sev { font-size: 11px; padding: 3px 9px; border-radius: 999px; border: 1px solid var(--line); color: var(--dim); }
  .sev.critical { color: var(--red); border-color: rgba(255,92,108,.45); }
  .sev.high { color: var(--amber); border-color: rgba(255,180,84,.45); }
  .sev.medium { color: var(--yellow); border-color: rgba(255,209,102,.4); }
  button {
    background: transparent; color: var(--cyan); border: 1px solid rgba(56,212,245,.5);
    padding: 7px 14px; border-radius: 8px; font-family: inherit; font-size: 12px;
    cursor: pointer; margin-top: 12px; transition: all .15s;
  }
  button:hover { background: rgba(56,212,245,.12); }
  button:disabled { opacity: .5; cursor: wait; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
  .chip { font-size: 11px; padding: 3px 8px; border-radius: 6px; border: 1px solid var(--line); color: var(--dim); }
  .chip.up { color: var(--green); border-color: rgba(61,220,151,.35); }
  .chip.down { color: var(--red); border-color: rgba(255,92,108,.35); }
  .feed { list-style: none; max-height: 300px; overflow-y: auto; }
  .feed li { font-size: 12px; padding: 7px 0; border-bottom: 1px dashed rgba(126,138,165,.12); color: var(--dim); }
  .feed li b { color: var(--text); font-weight: 500; }
  .feed li .t { float: right; font-size: 11px; }
  .empty { color: var(--dim); font-size: 12px; padding: 8px 0; font-style: italic; }
  /* ── Chat (Wave 9 NLU surface) ── */
  .chat { display: flex; flex-direction: column; }
  .chatlog { list-style: none; max-height: 280px; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; padding: 2px 0 8px; }
  .chatlog li { font-size: 12px; line-height: 1.45; max-width: 88%; padding: 7px 11px; border-radius: 10px; }
  .chatlog li.you { align-self: flex-end; background: rgba(56,212,245,.1); border: 1px solid rgba(56,212,245,.25); color: var(--text); }
  .chatlog li.fri { align-self: flex-start; background: var(--panel2); border: 1px solid var(--line); color: var(--text); }
  .chatlog li .who { display: block; font-size: 10px; text-transform: uppercase; letter-spacing: 1px; color: var(--dim); margin-bottom: 3px; }
  .chatbar { display: flex; gap: 8px; align-items: center; margin-top: 6px; }
  .chatbar input {
    flex: 1; min-width: 0; background: var(--panel2); color: var(--text);
    border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px;
    font-family: inherit; font-size: 12px;
  }
  .chatbar input:focus { outline: none; border-color: rgba(56,212,245,.5); }
  .chatbar button { margin-top: 0; }
  footer { margin-top: 26px; color: var(--dim); font-size: 11px; text-align: center; letter-spacing: 1px; }
</style>
</head>
<body>
<header>
  <div class="logo">◆ FRIDAY <small>V4 · DASHBOARD</small></div>
  <span class="pill" id="daemonPill"><span class="dot"></span>probe…</span>
  <span class="updated" id="updated">—</span>
</header>

<main class="grid" id="grid"></main>
<section id="chatSlot"></section>

<footer>friday4 web · local-first · data from your machine only</footer>

<script>
"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function card(title, led, inner) {
  return `<section class="card"><h2><span class="led ${led}"></span>${title}</h2>${inner}</section>`;
}
function row(k, v) { return `<div class="row"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`; }
function fmtUptime(s) {
  if (!s) return "—";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = Math.floor(s % 60);
  return h ? `${h}h ${m}m` : m ? `${m}m ${sec}s` : `${sec}s`;
}

/* ── Daemon ─────────────────────────────────────────────────── */
function daemonCard(d) {
  const running = !!d.running;
  const comps = d.components || {};
  const chips = Object.entries(comps).map(([k, up]) =>
    `<span class="chip ${up ? "up" : "down"}">${esc(k)}</span>`).join("");
  return card("Daemon", running ? "ok" : "bad",
    row("state", running ? "RUNNING" : "stopped") +
    row("uptime", fmtUptime(d.uptime_seconds)) +
    row("notifications", d.notification_count ?? 0) +
    `<div class="chips">${chips || '<span class="empty">no component data</span>'}</div>`);
}

/* ── Security ────────────────────────────────────────────────── */
let scanPathVal = "";  // survives re-renders (refresh() rebuilds the grid)
let scanMsg = "";
let projectsCache = [];  // picker suggestions survive re-renders too

function securityCard(s) {
  const rep = s.report || {};
  const counts = rep.counts_by_severity || {};    const sevs = Object.entries(counts).filter(([, n]) => n > 0)
    .map(([k, n]) => `<span class="sev ${esc(k)}">${esc(n)} ${esc(k)}</span>`).join("");
  const grade = rep.grade || "–";
  const gradeCls = ["A", "B", "C", "D", "F"].includes(grade) ? grade : "";
  return card("Security", s.last_error ? "bad" : (gradeCls === "F" || gradeCls === "D") ? "warn" : "ok",
    `<div style="display:flex;align-items:center">
       <span class="grade ${gradeCls}">${esc(grade)}</span>
       <div><div class="row"><span class="k">score</span><span class="v">${esc(rep.score ?? "—")} / 100</span></div>
       <div class="row"><span class="k">scans</span><span class="v">${esc(s.scans ?? 0)}</span></div>
       <div class="row"><span class="k">scanned</span><span class="v">${esc((rep.scanned_at || "").replace("T", " ").slice(0, 19) || "—")}</span></div></div>
     </div>` +
    `<div class="sevs">${sevs || '<span class="empty">clean — no findings</span>'}</div>` +
    (s.last_error ? `<div class="empty">last error: ${esc(s.last_error)}</div>` : "") +
    `<div class="scanbar">
       <input id="scanPath" list="projectList" placeholder="path to scan (e.g. ~/code)"
              value="${esc(scanPathVal)}" oninput="scanPathVal=this.value">
       <datalist id="projectList">${projectsCache.map((p) =>
         `<option value="${esc(p)}">`).join("")}</datalist>
       <button id="scanBtn" onclick="runScan()">⟳ run security scan</button>
     </div>` +
    `<div class="empty" id="scanMsg">${scanMsg ? "⚠ " + esc(scanMsg) : ""}</div>`);
}

/* ── Intelligence ────────────────────────────────────────────── */
function intelligenceCard(i) {
  const drift = i.drift || {};
  const anom = i.anomaly || {};
  const recent = (anom.recent_anomalies || []).slice(0, 4);
  const feed = recent.length
    ? `<ul class="feed">${recent.map((a) =>
        `<li><b>${esc(a.category)}</b> z=${esc(a.z_score)} <span class="t">${esc((a.timestamp || "").replace("T", " ").slice(5, 16))}</span></li>`).join("")}</ul>`
    : '<div class="empty">no anomalies logged</div>';
  return card("Intelligence", i.available ? "ok" : "bad",
    row("drift metrics", (drift.metrics_tracked || []).length) +
    row("drift samples", drift.total_samples ?? 0) +
    row("anomalies logged", anom.anomalies_logged ?? 0) +
    feed);
}

/* ── Proactive ───────────────────────────────────────────────── */
function proactiveCard(p) {
  const pat = p.patterns || {};
  return card("Proactive", p.available ? "ok" : "bad",
    row("sessions", p.sessions ?? 0) +
    row("patterns learned", pat.action_pairs_learned ?? 0) +
    row("state transitions", pat.state_transitions ?? "—"));
}

/* ── V3 bridge ───────────────────────────────────────────────── */
function v3Card(v) {
  const counts = v.counts || {};
  const bySource = Object.entries(counts.by_source || {}).slice(0, 4)
    .map(([k, n]) => `${esc(k)}:${esc(n)}`).join(", ") || "—";
  return card("V3 bridge", v.available ? "ok" : "bad",
    row("available", v.available ? "yes" : "no") +
    row("daemon", v.daemon_state || "—") +
    row("observations 24h", bySource));
}

/* ── Voice ───────────────────────────────────────────────────── */
function voiceCard(v) {
  return card("Voice", v.available ? "ok" : "bad",
    row("tts provider", v.tts_provider || "—") +
    row("hotword", v.hotword || "—"));
}

/* ── Memory (Wave 10) ────────────────────────────────────────── */
function memoryCard(m) {
  const facts = (m.facts || []).slice(0, 8);
  const feed = facts.length
    ? `<ul class="feed">${facts.map((f) =>
        `<li><b>${esc(f.predicate || f.key)}</b> — ${esc(f.value)}<span class="t">${esc(f.source || "")}</span></li>`).join("")}</ul>`
    : '<div class="empty">nothing remembered yet</div>';
  const working = m.working
    ? `<div class="empty">${esc(m.working.split("\n").slice(1).join(" · "))}</div>`
    : "";
  return card("Memory", m.available ? "ok" : "bad",
    row("facts remembered", (m.facts || []).length) + feed + working);
}

/* ── Relationship (Wave 10 + Wave 17 tone-direction) ─────────── */
function relationshipCard(r) {
  const depth = r.depth ?? 0;
  const bar = "█".repeat(Math.max(0, Math.min(10, Math.round(depth * 10)))) +
              "░".repeat(Math.max(0, 10 - Math.round(depth * 10)));
  const sig = r.signals || {};
  const dir = r.tone_direction || {};
  const dirRow = (dir.tone || dir.verbosity)
    ? row("explicit tone", `${esc(dir.tone || "—")} · v${dir.verbosity ?? "—"} ${esc((dir.request || "").slice(0, 28) || "")}`)
    : "";
  return card("Relationship", r.available ? "ok" : "bad",
    row("level", `${esc(r.level || "—")} · ${depth.toFixed(2)}`) +
    `<div class="empty" style="letter-spacing:2px">${esc(bar)}</div>` +
    row("tone", `${esc(r.tone || "—")} · verbosity ${r.verbosity ?? "—"}`) +
    row("briefing", esc(r.briefing || "—")) +
    dirRow +
    row("conversations", sig.exchanges ?? 0) +
    row("missions completed", sig.missions_completed ?? 0));
}

/* ── Capability registry (Wave 16, Law 7) ────────────────────── */
function capabilityCard(c) {
  const byLayer = c.by_layer || {};
  const chips = Object.entries(byLayer)
    .map(([k, n]) => `<span class="chip up">${esc(k)} ${esc(n)}</span>`).join("");
  const skills = (c.recent_skills || []).slice(0, 4);
  const feed = skills.length
    ? `<ul class="feed">${skills.map((s) =>
        `<li><b>${esc(s.name)}</b> — learned<span class="t">${esc(s.source)}</span></li>`).join("")}</ul>`
    : "";
  return card("Capabilities", c.available ? "ok" : "bad",
    row("registered", c.total ?? 0) +
    row("learned skills", c.skills ?? 0) +
    `<div class="chips">${chips || '<span class="empty">no data</span>'}</div>` +
    feed);
}

/* ── Skills (Wave 10) ────────────────────────────────────────── */
function skillsCard(k) {
  const recent = (k.recent || []).slice(0, 5);
  const feed = recent.length
    ? `<ul class="feed">${recent.map((s) =>
        `<li><b>${esc(s.name)}</b> — ${esc(s.state)}<span class="t">conf ${esc(s.confidence)}</span></li>`).join("")}</ul>`
    : '<div class="empty">no skills formed yet</div>';
  return card("Skills", k.available ? "ok" : "bad",
    row("shadow / verified / promoted",
        `${k.shadow ?? 0} / ${k.verified ?? 0} / ${k.promoted ?? 0}`) +
    feed);
}

/* ── Autonomy (Friday's own judgment → action) ───────────────── */
function autonomyCard(a) {
  const pending = (a.pending || []).slice(0, 8);
  const rows = pending.length
    ? `<ul class="feed">${pending.map((r) => {
        const rid = esc(String(r.id || "").slice(0, 8));
        const what = esc(r.description || r.command || r.action_type || "?");
        return `<li style="border:none;padding:6px 0"><b>${what}</b>
          <div style="margin-top:6px;display:flex;gap:6px">
            <button style="padding:3px 10px;margin-top:0" onclick="autonomyAct('approve','${esc(String(r.id || ""))}')">✓ yes</button>
            <button style="padding:3px 10px;margin-top:0" onclick="autonomyAct('deny','${esc(String(r.id || ""))}')">✗ no</button>
          </div>
          <span class="t">${rid} · ${esc((r.created_at || "").replace("T", " ").slice(5, 16))}</span></li>`;
      }).join("")}</ul>`
    : '<div class="empty">nothing waiting on your permission</div>';
  return card("Autonomy", a.available ? (pending.length ? "warn" : "ok") : "bad",
    row("pending asks", pending.length) +
    row("operator overrides", a.overrides ?? 0) +
    rows);
}

async function autonomyAct(action, id) {
  try {
    const r = await fetch(`/api/autonomy/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_id: id }),
    });
    const d = await r.json().catch(() => ({}));
    alert(d.response || (action === "approve" ? "approved" : "declined"));
  } catch (e) { alert("request failed"); }
  refresh();
}

/* ── Render ──────────────────────────────────────────────────── */
function render(d) {
  $("grid").innerHTML =
    daemonCard(d.daemon) + autonomyCard(d.autonomy) + securityCard(d.security) +
    intelligenceCard(d.intelligence) + proactiveCard(d.proactive) +
    memoryCard(d.memory) + relationshipCard(d.relationship) +
    skillsCard(d.skills) + capabilityCard(d.capability) +
    v3Card(d.v3) + voiceCard(d.voice) + ambientCard(d.v3);
  const pill = $("daemonPill");
  pill.className = "pill " + (d.daemon?.running ? "running" : "stopped");
  pill.innerHTML = `<span class="dot"></span>${d.daemon?.running ? "daemon running" : "daemon stopped"}`;
  $("updated").textContent = "updated " + new Date().toLocaleTimeString();
}

async function refresh() {
  try {
    const r = await fetch("/api/overview", { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    render(await r.json());
  } catch (e) {
    $("grid").innerHTML = `<div class="card"><h2><span class="led bad"></span>Connection</h2>
      <div class="empty">could not reach the dashboard API — is `friday4 web` still running?</div></div>`;
  }
}

async function loadProjects() {
  // Cache the picker suggestions; securityCard() renders them from the
  // cache on every refresh so they survive grid re-renders.
  try {
    const r = await fetch("/api/projects", { cache: "no-store" });
    const data = await r.json();
    if (Array.isArray(data)) projectsCache = data;
  } catch (e) { /* suggestions are optional */ }
}

async function runScan() {
  const btn = $("scanBtn");
  btn.disabled = true; btn.textContent = "scanning…";
  scanMsg = "";
  try {
    const r = await fetch("/api/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: scanPathVal || "." }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) scanMsg = data.error || ("HTTP " + r.status);
  } catch (e) { scanMsg = "scan request failed"; }
  setTimeout(async () => {
    await refresh();
    const btn2 = $("scanBtn");
    if (btn2) { btn2.disabled = false; btn2.textContent = "⟳ run security scan"; }
  }, 1500);
}

/* ── Chat — Friday's Wave 9 NLU surface in the browser ─────────── */
function chatCard() {
  return card("Talk to Friday", "ok",
    `<div class="chat">
       <ul class="chatlog" id="chatlog"><li class="fri"><span class="who">Friday</span>I can run tests, check git, start missions, and answer questions. Try: run the tests · git status · what's the status of my projects?</li></ul>
       <div class="chatbar">
         <input id="chatInput" placeholder="Say it like a person…" autocomplete="off"
                onkeydown="if(event.key==='Enter')sendChat()">
         <button id="chatBtn" onclick="sendChat()">➤</button>
       </div>
     </div>`);
}

/* Wave 15 — One Presence: hydrate the chat log from the SHARED
   session, so a conversation started in the terminal (or via voice)
   continues visibly in the browser. Oldest first; the live turns the
   user types today append below it. Guarded: a missing conversation
   leaves the greeting only. */
async function hydrateChat() {
  try {
    const r = await fetch("/api/conversation", { cache: "no-store" });
    if (!r.ok) return;
    const data = await r.json();
    const exchanges = data.exchanges || [];
    const log = $("chatlog");
    const who = (role) => (role === "user" ? "you" : "fri");
    for (const e of exchanges) {
      const text = String(e.content || "").trim();
      if (!text) continue;
      const li = document.createElement("li");
      li.className = who(e.role);
      li.innerHTML = `<span class="who">${who(e.role) === "you" ? "You" : "Friday"}</span>${esc(text)}`;
      log.appendChild(li);
    }
    if (exchanges.length) {
      const note = document.createElement("li");
      note.className = "fri";
      note.style.cssText = "font-size:11px;color:var(--dim);font-style:italic";
      note.textContent = "↳ resuming today's shared conversation";
      log.appendChild(note);
    }
    log.scrollTop = log.scrollHeight;
  } catch (e) { /* conversation is optional polish */ }
}

function chatLine(who, text) {
  const li = document.createElement("li");
  li.className = who;
  li.innerHTML = `<span class="who">${who === "you" ? "You" : "Friday"}</span>${esc(text)}`;
  return li;
}

async function sendChat() {
  const input = $("chatInput");
  const btn = $("chatBtn");
  const text = (input.value || "").trim();
  if (!text) return;
  input.value = "";
  const log = $("chatlog");
  log.appendChild(chatLine("you", text));
  const wait = chatLine("fri", "…");
  log.appendChild(wait);
  log.scrollTop = log.scrollHeight;
  btn.disabled = true;
  try {
    const r = await fetch("/api/talk", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    const data = await r.json().catch(() => ({}));
    wait.innerHTML = `<span class="who">Friday</span>${esc(data.response || "(no response)")}`;
  } catch (e) {
    wait.innerHTML = `<span class="who">Friday</span>could not reach the dashboard API`;
  }
  log.scrollTop = log.scrollHeight;
  btn.disabled = false;
  input.focus();
}

/* Render the chat card ONCE. #chatSlot lives outside #grid, so the
   10s poll (which rebuilds only #grid.innerHTML) can never touch the
   chat history or anything the user is typing. */
$("chatSlot").innerHTML = chatCard();
hydrateChat();

loadProjects();
refresh();
setInterval(refresh, 10000);

/* ── Live ambient push (Wave 11) — SSE replaces the poll ────────────
   The /api/events stream pushes durable ambient events (security,
   suggestions, collab, briefing) as they happen. We refresh the grid
   on each event; the 10s setInterval above stays as a fallback when
   EventSource is unavailable or the stream drops. */
(function liveAmbient() {
  if (!window.EventSource) return;  // old browsers → poll fallback
  const es = new EventSource("/api/events");
  es.onmessage = (ev) => { try { refresh(); } catch (e) {} };
  es.onerror = () => { /* the poll fallback keeps the dashboard alive */ };
})();
</script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    """Serves the dashboard page + JSON API."""

    server_version = "FridayV4/1.0"

    def log_message(self, fmt, *args):  # keep the console quiet
        logger.debug(fmt, *args)

    # ── helpers ──────────────────────────────────────────────────

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._send(status, json.dumps(obj, default=str).encode(), "application/json")

    def _page(self, status: int = 200) -> None:
        self._send(status, _PAGE.encode(), "text/html; charset=utf-8")

    # ── routing ──────────────────────────────────────────────────

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._page()
        elif path == "/api/overview":
            self._json(dashboard.overview())
        elif path == "/api/security":
            self._json(dashboard.security_state())
        elif path == "/api/ambient":
            self._json(dashboard.v3_state().get("ambient_recent", []))
        elif path == "/api/projects":
            self._json(dashboard.project_candidates())
        elif path == "/api/memory":
            self._json(dashboard.memory_state())
        elif path == "/api/relationship":
            self._json(dashboard.relationship_state())
        elif path == "/api/skills":
            self._json(dashboard.skills_state())
        elif path == "/api/capability":
            self._json(dashboard.capability_state())
        elif path == "/api/ambient-events":
            self._json(dashboard.ambient_state())
        elif path == "/api/conversation":
            # Wave 15 One Presence: today's shared-session exchanges —
            # the browser resumes the thread the terminal/voice append
            # to (a conversation started in the terminal continues here).
            self._json(dashboard.conversation_state())
        elif path == "/api/briefing":
            self._json(dashboard.briefing_state())
        elif path == "/api/autonomy":
            self._json(dashboard.autonomy_state())
        elif path == "/api/events":
            # Wave 11 push: SSE stream over the durable ambient queue.
            # The dashboard subscribes instead of polling; a tab that
            # opens late replays events it missed via the `since` cursor.
            self._stream_events()
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._json({"error": "not found"}, status=404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/talk":
            # The dashboard is a first-class Friday surface: text here
            # flows through the same Wave 9 NLU brain (resolve →
            # execute / missions / reasoning) as voice and `friday4 talk`.
            text = self._body_text()
            if not text:
                self._json({"action": "failed",
                            "response": "no text sent"}, status=400)
                return
            self._json(dashboard.talk(text))
        elif path == "/api/autonomy/approve":
            rid = self._body_request_id()
            if not rid:
                self._json({"ok": False,
                            "response": "no request id sent"}, status=400)
                return
            self._json(dashboard.autonomy_approve(rid))
        elif path == "/api/autonomy/deny":
            rid = self._body_request_id()
            if not rid:
                self._json({"ok": False,
                            "response": "no request id sent"}, status=400)
                return
            self._json(dashboard.autonomy_deny(rid))
        elif path == "/api/scan":
            # Best-effort guard (advisory, TOCTOU): the 409 may not fire for
            # two near-simultaneous POSTs — the lock below is what actually
            # serializes the state-file writes, so double-starts are safe.
            if _scan_lock.locked():
                self._json({"started": False, "error": "scan already running"},
                           status=409)
                return
            try:
                scan_path = self._validated_path()
            except ValueError as exc:
                self._json({"started": False, "error": str(exc)}, status=400)
                return
            # Scan on a daemon thread; SecurityScanner persists state and
            # dedups notifications, so a dashboard-triggered scan behaves
            # exactly like the daemon's own periodic scan. The lock guards
            # the shared state file against concurrent writes.
            def _scan():
                with _scan_lock:
                    try:
                        from friday_v4.daemon import SecurityScanner
                        SecurityScanner(path=scan_path).scan_once()
                    except Exception as exc:  # pragma: no cover - defensive
                        logger.debug(f"web scan failed: {exc}")
            threading.Thread(target=_scan, daemon=True).start()
            self._json({"started": True})
        else:
            self._json({"error": "not found"}, status=404)

    def _stream_events(self, poll_interval: float = 1.0) -> None:
        """Server-Sent Events stream of durable ambient events (Wave 11).

        The dashboard keeps a single EventSource open here; every event
        the daemon/security/suggestions/collab publish to the AmbientBus
        (durable queue) is pushed as a JSON event with an auto-increment
        ``id`` so a late tab replays what it missed. Never raises — a
        missing DB streams an empty feed (heartbeat only) and a dropped
        client ends the handler quietly.
        """
        import time
        from urllib.parse import parse_qs

        query = parse_qs(urlparse(self.path).query)
        try:
            since = int((query.get("since") or ["0"])[0])
        except ValueError:
            since = 0

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        # One read-only connection for the stream's lifetime (the queue
        # is the same DB the bus writes to; a fresh connect per poll
        # would churn file handles for long-lived tabs).
        import sqlite3
        from friday_v4 import db as db_mod
        conn = None
        try:
            conn = db_mod.connect(read_only=True)
        except (sqlite3.Error, OSError):
            conn = None

        last_id = since
        last_heartbeat = time.monotonic()
        try:
            while True:
                events: list = []
                if conn is not None:
                    try:
                        events = db_mod.ambient_events_since(conn, last_id)
                    except sqlite3.Error:
                        events = []
                for ev in events:
                    rowid = int(ev.get("rowid") or 0)
                    if rowid <= last_id:
                        continue
                    last_id = rowid
                    data = json.dumps({
                        "id": rowid,
                        "topic": ev.get("topic"),
                        "payload": ev.get("payload"),
                        "priority": ev.get("priority"),
                        "source": ev.get("source"),
                        "created_at": ev.get("created_at"),
                    })
                    self.wfile.write(f"id: {rowid}\ndata: {data}\n\n"
                                     .encode())
                    self.wfile.flush()
                # Keep-alive heartbeat so proxies/timeouts don't kill the
                # connection during quiet stretches.
                if time.monotonic() - last_heartbeat >= 15.0:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    last_heartbeat = time.monotonic()
                time.sleep(poll_interval)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # client went away — nothing to clean up
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _body_text(self) -> str:
        """Optional chat text from the POST body ({'text': ...})."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                body = json.loads(self.rfile.read(length))
                if isinstance(body, dict) and body.get("text"):
                    return str(body["text"]).strip()
        except (ValueError, json.JSONDecodeError):
            pass
        return ""

    def _body_path(self) -> str:
        """Optional scan path from the POST body ({'path': ...})."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                body = json.loads(self.rfile.read(length))
                if isinstance(body, dict) and body.get("path"):
                    return str(body["path"])
        except (ValueError, json.JSONDecodeError):
            pass
        return "."

    def _body_request_id(self) -> str:
        """Request id from the POST body ({'request_id': ...})."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                body = json.loads(self.rfile.read(length))
                if isinstance(body, dict) and body.get("request_id"):
                    return str(body["request_id"]).strip()
        except (ValueError, json.JSONDecodeError):
            pass
        return ""

    def _validated_path(self) -> str:
        """Body path expanded + checked to be a real directory.

        Returns "." when no path was sent (scan the server's cwd).
        Raises ValueError for a provided path that isn't a directory.
        """
        raw = self._body_path()
        if raw == ".":
            return raw
        expanded = str(Path(raw).expanduser())
        if not Path(expanded).is_dir():
            raise ValueError(f"not a directory: {raw}")
        return expanded


def make_server(host: str = "127.0.0.1", port: int = 8899) -> ThreadingHTTPServer:
    """Build the dashboard server (caller runs ``serve_forever``)."""
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    # Handler threads must not delay Ctrl+C shutdown.
    server.daemon_threads = True
    return server


def serve(host: str = "127.0.0.1", port: int = 8899) -> None:
    """Start the dashboard server; blocks until interrupted."""
    import webbrowser

    server = make_server(host, port)
    url = f"http://{host}:{server.server_address[1]}/"
    # flush=True: the URL must be visible immediately even when stdout is
    # redirected (scripting, `friday4 web | tee`), not just on a TTY.
    print(f"  {_DIM}Open {_RESET}{_CYAN}{url}{_RESET}", flush=True)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    print(f"  {_DIM}Press Ctrl+C to stop.{_RESET}\n", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[96m"
