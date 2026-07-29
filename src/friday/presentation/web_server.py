"""Web Interface — lightweight web server for Friday's dashboard.

Provides:
  - `friday web` → starts server on localhost:8321
  - Real-time updates via Server-Sent Events (SSE)
  - Dashboard overview, ambient feed, telemetry, watchers, missions views
  - PWA manifest + service worker for mobile companion
  - Zero required deps — uses stdlib http.server

Design:
  - Separate process (not embedded in daemon) — run via `friday web`
  - All data comes from DB — READ-ONLY (no mutations through web in v1)
  - SSE endpoint pushes new ambient events periodically
  - Minimal JS — mostly server-rendered HTML
"""

from __future__ import annotations

import html
import json
import os
import signal
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional, Callable


# ──────────────────────────────────────────────────────────────────────────
# Server configuration
# ──────────────────────────────────────────────────────────────────────────

DEFAULT_PORT = 8321
HOST = "127.0.0.1"


# ──────────────────────────────────────────────────────────────────────────
# HTML templates
# ──────────────────────────────────────────────────────────────────────────

_HTML_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="theme-color" content="#0d1117">
<title>Friday Dashboard</title>
<style>
  :root {
    --bg: #0d1117; --text: #e6edf3; --dim: #8b949e;
    --primary: #58a6ff; --success: #3fb950; --warning: #d29922;
    --error: #f85149; --border: #30363d; --card-bg: #161b22;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;
         background: var(--bg); color: var(--text); line-height: 1.5;
         padding: 20px; max-width: 1200px; margin: 0 auto; }
  h1 { font-size: 1.5em; margin-bottom: 16px; color: var(--primary); }
  h2 { font-size: 1.1em; margin-bottom: 8px; color: var(--text); }
  .status-bar { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }
  .stat { background: var(--card-bg); border: 1px solid var(--border);
          border-radius: 6px; padding: 12px 16px; flex: 1; min-width: 140px; }
  .stat .value { font-size: 1.4em; font-weight: 600; color: var(--primary); }
  .stat .label { font-size: 0.85em; color: var(--dim); }
  .stat.green .value { color: var(--success); }
  .stat.red .value { color: var(--error); }
  .stat.yellow .value { color: var(--warning); }
  .feed { background: var(--card-bg); border: 1px solid var(--border);
          border-radius: 6px; padding: 16px; }
  .event { padding: 8px 0; border-bottom: 1px solid var(--border); }
  .event:last-child { border-bottom: none; }
  .event .time { color: var(--dim); font-size: 0.85em; }
  .event .title { font-weight: 500; }
  .event .detail { color: var(--dim); font-size: 0.9em; }
  .pri-high { border-left: 3px solid var(--error); padding-left: 8px; }
  .pri-med { border-left: 3px solid var(--warning); padding-left: 8px; }
  .pri-low { border-left: 3px solid var(--dim); padding-left: 8px; }
  .badge { display: inline-block; padding: 1px 6px; border-radius: 10px;
           font-size: 0.75em; font-weight: 600; margin-right: 4px; }
  .badge-workspace { background: #1a3a1a; color: var(--success); }
  .badge-intelligence { background: #1a2a3a; color: var(--primary); }
  .badge-quality { background: #3a2a1a; color: var(--warning); }
  .badge-execution { background: #1a3a3a; color: #56d4dd; }
  .badge-system { background: #2a2a2a; color: var(--dim); }
  .nav { display: flex; gap: 8px; margin-bottom: 20px; flex-wrap: wrap; }
  .nav a { color: var(--dim); text-decoration: none; padding: 6px 12px;
           border-radius: 6px; border: 1px solid var(--border); }
  .nav a:hover { background: var(--card-bg); color: var(--text); }
  .nav a.active { background: var(--primary); color: #fff; border-color: var(--primary); }
  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; padding: 8px; color: var(--dim); font-weight: 500;
       border-bottom: 2px solid var(--border); font-size: 0.85em; }
  td { padding: 8px; border-bottom: 1px solid var(--border); }
  .live-indicator { display: inline-block; width: 8px; height: 8px;
                    border-radius: 50%; background: var(--success);
                    animation: pulse 2s infinite; margin-right: 6px; }
  @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.4; } 100% { opacity: 1; } }
  .footer { margin-top: 20px; color: var(--dim); font-size: 0.85em;
            text-align: center; border-top: 1px solid var(--border); padding-top: 12px; }
  @media (max-width: 600px) {
    body { padding: 10px; }
    .stat { min-width: calc(50% - 6px); }
  }
</style>
<link rel="manifest" href="/manifest.json">
</head>
<body>
<div class="nav">
  <a href="/" class="active">Dashboard</a>
  <a href="/feed">Feed</a>
  <a href="/telemetry">Telemetry</a>
  <a href="/watchers">Watchers</a>
  <a href="/missions">Missions</a>
</div>
"""

_HTML_TAIL = """
<div class="footer">
  <span class="live-indicator"></span> Friday Dashboard &mdash; Live updates via SSE
</div>
<script>
  if (typeof EventSource !== 'undefined') {
    const evtSource = new EventSource('/events');
    evtSource.onmessage = function(e) {
      const data = JSON.parse(e.data);
      if (data.type === 'refresh') {
        const path = window.location.pathname;
        fetch(path)
          .then(r => r.text())
          .then(html => {
            const parser = new DOMParser();
            const doc = parser.parseFromString(html, 'text/html');
            const content = doc.querySelector('#content');
            const oldContent = document.querySelector('#content');
            if (content && oldContent) oldContent.innerHTML = content.innerHTML;
          });
      }
    };
    evtSource.onerror = function() {
      // Reconnect on error — EventSource does this automatically.
    };
  }
</script>
</body>
</html>
"""


# ──────────────────────────────────────────────────────────────────────────
# Page renderers
# ──────────────────────────────────────────────────────────────────────────


def _render_dashboard() -> str:
    """Render the main dashboard page."""
    from ..db import connect
    conn = connect()
    
    try:
        # Repo count.
        repo_count = conn.execute("SELECT COUNT(*) AS cnt FROM repositories").fetchone()["cnt"]
        
        # Daemon status.
        daemon_state = "stopped"
        try:
            from ..daemon import get_status
            st = get_status()
            daemon_state = st.get("state", "stopped")
        except Exception:
            pass
        
        # Events.
        event_count = conn.execute("SELECT COUNT(*) AS cnt FROM ambient_feed WHERE dismissed=0").fetchone()["cnt"]
        unread = conn.execute("SELECT COUNT(*) AS cnt FROM ambient_feed WHERE dismissed=0 AND read=0").fetchone()["cnt"]
        
        # Watchers.
        watcher_count = conn.execute("SELECT COUNT(*) AS cnt FROM watch_history WHERE outcome='running'").fetchone()["cnt"]
        
        # Missions.
        mission_count = conn.execute("SELECT COUNT(*) AS cnt FROM missions WHERE status='running'").fetchone()["cnt"]
        
        # Recent events.
        events = conn.execute(
            "SELECT id, timestamp, event_type, title, detail, priority, category "
            "FROM ambient_feed WHERE dismissed=0 ORDER BY timestamp DESC LIMIT 20"
        ).fetchall()
        
        # Kill switch.
        kill_active = False
        try:
            from ..autonomy import is_kill_switch_active
            kill_active = is_kill_switch_active(conn)
        except Exception:
            pass
        
        parts = [_HTML_HEAD]
        parts.append('<div id="content">')
        
        # Status bar.
        parts.append('<div class="status-bar">')
        parts.append(f'<div class="stat"><div class="value">{repo_count}</div><div class="label">Repositories</div></div>')
        state_class = {"running": "green", "stopped": "", "crashed": "red"}.get(daemon_state, "")
        parts.append(f'<div class="stat {state_class}"><div class="value">{daemon_state}</div><div class="label">Daemon</div></div>')
        parts.append(f'<div class="stat"><div class="value">{event_count}</div><div class="label">Events ({unread} unread)</div></div>')
        parts.append(f'<div class="stat"><div class="value">{watcher_count}</div><div class="label">Active Watchers</div></div>')
        parts.append(f'<div class="stat"><div class="value">{mission_count}</div><div class="label">Active Missions</div></div>')
        if kill_active:
            parts.append('<div class="stat red"><div class="value">⚠</div><div class="label">Kill Switch ACTIVE</div></div>')
        parts.append('</div>')
        
        # Recent events feed.
        parts.append('<h2>Recent Events</h2>')
        parts.append('<div class="feed">')
        if not events:
            parts.append('<div class="event"><span class="detail">No events yet.</span></div>')
        for ev in events:
            pri_class = "pri-high" if ev["priority"] >= 3 else "pri-med" if ev["priority"] >= 2 else "pri-low"
            cat_class = f"badge-{ev['category'] or 'system'}"
            time_str = ev["timestamp"][11:19] if ev["timestamp"] else "?"
            parts.append(f'<div class="event {pri_class}">')
            parts.append(f'  <span class="badge {cat_class}">{ev["category"] or "?"}</span>')
            parts.append(f'  <span class="time">{html.escape(time_str)}</span>')
            parts.append(f'  <span class="title">{html.escape(ev["title"] or "")}</span>')
            if ev["detail"]:
                parts.append(f'  <div class="detail">{html.escape(ev["detail"][:200])}</div>')
            parts.append('</div>')
        parts.append('</div>')
        
        parts.append('</div>')  # #content
        parts.append(_HTML_TAIL)
        return "\n".join(parts)
    finally:
        conn.close()


def _render_feed() -> str:
    """Render the ambient feed page."""
    from ..db import connect
    conn = connect()
    
    try:
        events = conn.execute(
            "SELECT id, timestamp, event_type, title, detail, priority, category, "
            "dismissed, actionable, action_command "
            "FROM ambient_feed ORDER BY timestamp DESC LIMIT 100"
        ).fetchall()
        
        parts = [_HTML_HEAD]
        parts.append('<div id="content">')
        parts.append('<h2>Ambient Feed</h2>')
        parts.append('<div class="feed">')
        
        if not events:
            parts.append('<div class="event"><span class="detail">No events yet.</span></div>')
        
        for ev in events:
            pri_class = "pri-high" if ev["priority"] >= 3 else "pri-med" if ev["priority"] >= 2 else "pri-low"
            cat_class = f"badge-{ev['category'] or 'system'}"
            time_str = ev["timestamp"] if ev["timestamp"] else "?"
            dismissed = " [dismissed]" if ev["dismissed"] else ""
            parts.append(f'<div class="event {pri_class}">')
            parts.append(f'  <span class="badge {cat_class}">{ev["category"] or "?"}</span>')
            parts.append(f'  <span class="time">{html.escape(time_str)}</span>')
            parts.append(f'  <span class="title">{html.escape(ev["title"] or "")}{dismissed}</span>')
            if ev["detail"]:
                parts.append(f'  <div class="detail">{html.escape(ev["detail"][:200])}</div>')
            if ev["actionable"] and ev["action_command"]:
                parts.append(f'  <div class="detail">→ {html.escape(ev["action_command"])}</div>')
            parts.append('</div>')
        
        parts.append('</div>')
        parts.append('</div>')
        parts.append(_HTML_TAIL)
        return "\n".join(parts)
    finally:
        conn.close()


def _render_telemetry() -> str:
    """Render the telemetry page."""
    from ..db import connect
    conn = connect()
    
    try:
        # Cycle history.
        cycles = conn.execute(
            "SELECT started_at, finished_at, outcome, repos_scanned "
            "FROM watch_history ORDER BY started_at DESC LIMIT 20"
        ).fetchall()
        
        # Action counts by type.
        actions = conn.execute(
            "SELECT action_type, COUNT(*) AS cnt FROM actions GROUP BY action_type ORDER BY cnt DESC LIMIT 10"
        ).fetchall()
        
        parts = [_HTML_HEAD]
        parts.append('<div id="content">')
        
        # Daemon cycles.
        parts.append('<h2>Daemon Cycles</h2>')
        parts.append('<table><tr><th>Started</th><th>Duration</th><th>Outcome</th><th>Repos</th></tr>')
        for c in cycles:
            dur = ""
            if c["started_at"] and c["finished_at"]:
                try:
                    from datetime import datetime
                    s = datetime.fromisoformat(c["started_at"])
                    f = datetime.fromisoformat(c["finished_at"])
                    secs = int((f - s).total_seconds())
                    dur = f"{secs}s"
                except Exception:
                    pass
            outcome_class = {"succeeded": "green", "failed": "red"}.get(c["outcome"], "")
            parts.append(f'<tr><td>{html.escape(c["started_at"][:19] or "?")}</td>'
                         f'<td>{dur}</td>'
                         f'<td class="{outcome_class}">{html.escape(c["outcome"] or "?")}</td>'
                         f'<td>{c.get("repos_scanned", 0)}</td></tr>')
        parts.append('</table>')
        
        # Action counts.
        parts.append('<h2>Actions by Type</h2>')
        parts.append('<table><tr><th>Action Type</th><th>Count</th></tr>')
        for a in actions:
            parts.append(f'<tr><td>{html.escape(a["action_type"] or "?")}</td><td>{a["cnt"]}</td></tr>')
        parts.append('</table>')
        
        parts.append('</div>')
        parts.append(_HTML_TAIL)
        return "\n".join(parts)
    finally:
        conn.close()


def _render_watchers() -> str:
    """Render the watchers page."""
    from ..db import connect
    conn = connect()
    
    try:
        watchers = conn.execute(
            "SELECT id, started_at, outcome, repos_scanned "
            "FROM watch_history ORDER BY started_at DESC LIMIT 30"
        ).fetchall()
        
        parts = [_HTML_HEAD]
        parts.append('<div id="content">')
        parts.append('<h2>Watch History</h2>')
        parts.append('<table><tr><th>ID</th><th>Started</th><th>Outcome</th><th>Repos Scanned</th></tr>')
        for w in watchers:
            outcome_class = {"succeeded": "green", "failed": "red", "running": "yellow"}.get(w["outcome"], "")
            wid = str(w["id"])[:12]
            parts.append(f'<tr><td>{html.escape(wid)}</td>'
                         f'<td>{html.escape(w["started_at"][:19] or "?")}</td>'
                         f'<td class="{outcome_class}">{html.escape(w["outcome"] or "?")}</td>'
                         f'<td>{w.get("repos_scanned", 0)}</td></tr>')
        parts.append('</table>')
        parts.append('</div>')
        parts.append(_HTML_TAIL)
        return "\n".join(parts)
    finally:
        conn.close()


def _render_missions() -> str:
    """Render the missions page."""
    from ..db import connect
    conn = connect()
    
    try:
        missions = conn.execute(
            "SELECT id, goal, status, created_at, mission_type "
            "FROM missions ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
        
        parts = [_HTML_HEAD]
        parts.append('<div id="content">')
        parts.append('<h2>Missions</h2>')
        
        if not missions:
            parts.append('<p class="detail">No missions yet.</p>')
        else:
            parts.append('<table><tr><th>ID</th><th>Goal</th><th>Status</th><th>Type</th><th>Created</th></tr>')
            for m in missions:
                status_class = {"succeeded": "green", "failed": "red", "running": "yellow"}.get(m["status"], "")
                mid = str(m["id"])[:12]
                parts.append(f'<tr><td>{html.escape(mid)}</td>'
                             f'<td>{html.escape(m["goal"][:60] or "")}</td>'
                             f'<td class="{status_class}">{html.escape(m["status"] or "?")}</td>'
                             f'<td>{html.escape(m["mission_type"] or "?")}</td>'
                             f'<td>{html.escape(m["created_at"][:19] or "?")}</td></tr>')
            parts.append('</table>')
        
        parts.append('</div>')
        parts.append(_HTML_TAIL)
        return "\n".join(parts)
    finally:
        conn.close()


def _render_manifest() -> str:
    """Render the PWA manifest."""
    return json.dumps({
        "name": "Friday Dashboard",
        "short_name": "Friday",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0d1117",
        "theme_color": "#0d1117",
        "description": "Friday — AI Operating Partner Dashboard",
        "icons": [{"src": "/icon.svg", "sizes": "192x192", "type": "image/svg+xml"}],
    })


def _render_service_worker() -> str:
    """Render the service worker for PWA offline caching."""
    return """self.addEventListener('install', (e) => {
  e.waitUntil(self.skipWaiting());
});
self.addEventListener('activate', (e) => {
  e.waitUntil(self.clients.claim());
});
self.addEventListener('fetch', (e) => {
  // Network-first strategy
  e.respondWith(
    fetch(e.request).catch(() => new Response('Offline', { status: 503 }))
  );
});
"""


# ──────────────────────────────────────────────────────────────────────────
# SSE endpoint
# ──────────────────────────────────────────────────────────────────────────


def _sse_events() -> str:
    """Generate SSE events. Returns the event stream text."""
    events = []
    events.append(f"data: {json.dumps({'type': 'refresh'})}\n\n")
    try:
        from ..db import connect
        conn = connect()
        count = conn.execute("SELECT COUNT(*) AS cnt FROM ambient_feed WHERE dismissed=0").fetchone()["cnt"]
        conn.close()
        events.append(f"data: {json.dumps({'type': 'event_count', 'count': count})}\n\n")
    except Exception:
        pass
    return "".join(events)


# ──────────────────────────────────────────────────────────────────────────
# Route table
# ──────────────────────────────────────────────────────────────────────────

_ROUTES: dict[str, Callable[[], tuple[int, str, str]]] = {
    "/": lambda: (200, "text/html", _render_dashboard()),
    "/feed": lambda: (200, "text/html", _render_feed()),
    "/telemetry": lambda: (200, "text/html", _render_telemetry()),
    "/watchers": lambda: (200, "text/html", _render_watchers()),
    "/missions": lambda: (200, "text/html", _render_missions()),
    "/manifest.json": lambda: (200, "application/json", _render_manifest()),
    "/sw.js": lambda: (200, "application/javascript", _render_service_worker()),
}


# ──────────────────────────────────────────────────────────────────────────
# HTTP Handler
# ──────────────────────────────────────────────────────────────────────────


class FridayWebHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the Friday web dashboard."""

    def do_GET(self) -> None:
        path = self.path.split("?")[0]

        # SSE endpoint.
        if path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            
            # Push initial data then keep connection alive.
            try:
                for _ in range(30):  # Keep alive for ~30s
                    data = _sse_events()
                    self.wfile.write(data.encode("utf-8"))
                    self.wfile.flush()
                    time.sleep(2)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        # Static files.
        if path == "/icon.svg":
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.end_headers()
            self.wfile.write(b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192"><rect width="192" height="192" fill="#0d1117"/><text x="96" y="128" font-size="96" text-anchor="middle" fill="#58a6ff">F</text></svg>')
            return

        # Route lookup.
        handler = _ROUTES.get(path)
        if handler:
            try:
                status, content_type, body = handler()
            except Exception as exc:
                status, content_type, body = 500, "text/html", f"<h1>Internal Error</h1><pre>{exc}</pre>"
        else:
            status, content_type, body = 404, "text/html", "<h1>Not Found</h1>"

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache" if "text/html" in content_type else "max-age=3600")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default logging to stderr."""
        pass


# ──────────────────────────────────────────────────────────────────────────
# Server lifecycle
# ──────────────────────────────────────────────────────────────────────────


_server: Optional[HTTPServer] = None


def start_server(port: int = DEFAULT_PORT, open_browser: bool = False) -> None:
    """Start the Friday web server.

    Args:
        port: Port to listen on (default: 8321).
        open_browser: Whether to open the browser automatically.
    """
    global _server
    
    _server = HTTPServer((HOST, port), FridayWebHandler)
    url = f"http://{HOST}:{port}"
    
    print(f"  Friday Web Dashboard: {url}")
    print(f"  Press Ctrl+C to stop.")
    print()
    
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    
    # Handle SIGINT gracefully.
    def _handle_sig(sig, frame):
        print("\n  Shutting down...")
        _server.shutdown()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, _handle_sig)
    
    try:
        _server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down...")
        _server.shutdown()


def stop_server() -> None:
    """Stop the web server if running."""
    global _server
    if _server:
        _server.shutdown()
        _server = None
