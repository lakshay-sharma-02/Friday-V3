"""Architecture Visualization — ASCII, Mermaid, and image renderers for Friday.

Provides:
  - `friday viz arch` → project architecture diagram
  - `friday viz deps` → dependency graph
  - `friday viz timeline` → activity timeline
  - `friday viz impact <symbol>` → impact tree

Each visualization is deterministic (no LLM), generates structured intermediate
data (nodes + edges), then renders through format-specific renderers.

Supported output formats:
  - tree:   ASCII tree via Rich Tree or indented text
  - mermaid: Mermaid.js markup for GitHub/Mermaid viewers
  - image:  SVG via graphviz if available
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# ──────────────────────────────────────────────────────────────────────────
# Data models
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class VizNode:
    """A node in a visualization graph."""

    id: str
    label: str
    kind: str = "default"      # "project" | "module" | "file" | "symbol" | "phase"
    detail: str = ""
    children: list[VizNode] = field(default_factory=list)


@dataclass
class VizEdge:
    """A directed edge between two nodes."""

    source: str
    target: str
    label: str = ""
    kind: str = "depends"      # "depends" | "calls" | "imports" | "contains"


@dataclass
class VizGraph:
    """Intermediate representation of a visualization graph."""

    title: str
    nodes: list[VizNode] = field(default_factory=list)
    edges: list[VizEdge] = field(default_factory=list)

    def find_node(self, node_id: str) -> Optional[VizNode]:
        """Find a node by ID (breadth-first)."""
        queue = list(self.nodes)
        while queue:
            node = queue.pop(0)
            if node.id == node_id:
                return node
            queue.extend(node.children)
        return None


# ──────────────────────────────────────────────────────────────────────────
# Architecture graph builders
# ──────────────────────────────────────────────────────────────────────────


def _build_project_tree(repo_path: Optional[str] = None) -> VizGraph:
    """Build a tree of project structure from workspace data or filesystem."""
    graph = VizGraph(title="Project Architecture")
    
    if repo_path:
        # Build from a specific directory.
        root = Path(repo_path).expanduser().resolve()
        if not root.is_dir():
            graph.nodes.append(VizNode(id="error", label=f"Not found: {repo_path}", kind="error"))
            return graph
        
        graph.nodes.append(VizNode(id=root.name, label=root.name, kind="project", detail=str(root)))
        _walk_directory(root, root, graph)
    else:
        # Build from workspace DB.
        try:
            from ..db import connect
            conn = connect()
            repos = conn.execute(
                "SELECT name, path, description FROM repositories ORDER BY name"
            ).fetchall()
            conn.close()
            
            for r in repos:
                node = VizNode(
                    id=r["name"],
                    label=r["name"],
                    kind="project",
                    detail=r.get("description", "") or r.get("path", ""),
                )
                # Try to read structure from DB.
                try:
                    conn2 = connect()
                    components = conn2.execute(
                        "SELECT * FROM architect_components WHERE repo_id = "
                        "(SELECT id FROM repositories WHERE name = ?) LIMIT 20",
                        (r["name"],),
                    ).fetchall()
                    conn2.close()
                    if components:
                        for c in components:
                            node.children.append(VizNode(
                                id=f"{r['name']}/{c['name']}",
                                label=c["name"],
                                kind="module",
                                detail=c.get("description", ""),
                            ))
                    else:
                        # Fall back to filesystem.
                        p = Path(r.get("path", "."))
                        if p.is_dir():
                            _walk_directory(p, p, graph, parent_id=r["name"])
                except Exception:
                    pass
                graph.nodes.append(node)
        except Exception:
            # Fallback: scan current directory.
            root = Path.cwd()
            graph.nodes.append(VizNode(id=root.name, label=root.name, kind="project", detail=str(root)))
            _walk_directory(root, root, graph)
    
    return graph


def _walk_directory(root: Path, current: Path, graph: VizGraph,
                    parent_id: Optional[str] = None, depth: int = 0) -> None:
    """Walk a directory and add nodes to the graph."""
    if depth > 3:
        return  # Limit depth.
    
    try:
        entries = sorted(current.iterdir(), key=lambda p: (p.is_file(), p.name))
    except PermissionError:
        return
    
    # Collect important source files and directories.
    source_exts = {".py", ".js", ".ts", ".rs", ".go", ".java", ".kt",
                   ".swift", ".c", ".cpp", ".h", ".hpp", ".rb", ".php",
                   ".jsx", ".tsx", ".vue", ".svelte", ".css", ".scss"}
    
    dirs = []
    files = []
    for entry in entries:
        if entry.name.startswith(".") or entry.name.startswith("__pycache__"):
            continue
        if entry.name == "node_modules" or entry.name == ".git":
            continue
        if entry.is_dir():
            dirs.append(entry)
        elif entry.suffix in source_exts:
            files.append(entry)
    
    # Add directories as module nodes.
    for d in dirs[:8]:  # Limit to 8 subdirectories.
        node_id = f"{parent_id}/{d.name}" if parent_id else d.name
        kind = "module"
        # Check if it's a typical project directory.
        if d.name in ("src", "lib", "app", "components", "pages", "api"):
            kind = "module"
        node = VizNode(id=node_id, label=d.name, kind=kind)
        graph.find_node(parent_id) if parent_id else None
        if parent_id:
            parent = graph.find_node(parent_id)
            if parent:
                parent.children.append(node)
        else:
            graph.nodes.append(node)
        _walk_directory(root, d, graph, parent_id=node_id, depth=depth + 1)
    
    # Add source files as file nodes (only to immediate parent).
    for f in files[:12]:  # Limit to 12 files per directory.
        node_id = f"{parent_id}/{f.name}" if parent_id else f.name
        node = VizNode(id=node_id, label=f.name, kind="file", detail=f.suffix)
        if parent_id:
            parent = graph.find_node(parent_id)
            if parent:
                parent.children.append(node)


def _build_dep_graph() -> VizGraph:
    """Build a dependency graph between projects from workspace data."""
    graph = VizGraph(title="Project Dependencies")
    
    try:
        from ..db import connect
        conn = connect()
        repos = conn.execute("SELECT id, name FROM repositories ORDER BY name").fetchall()
        
        # Add nodes.
        for r in repos:
            graph.nodes.append(VizNode(id=r["name"], label=r["name"], kind="project"))
        
        # Add dependency edges from knowledge_relationships or similar.
        try:
            deps = conn.execute(
                """SELECT DISTINCT a.name AS source, b.name AS target
                   FROM repositories a
                   JOIN knowledge_relationships kr ON kr.source_type = 'repo'
                   JOIN repositories b ON b.name = kr.target_id
                   WHERE kr.relationship_type IN ('depends', 'imports', 'uses', 'references')
                   AND kr.source_id = a.name"""
            ).fetchall()
            for d in deps:
                graph.edges.append(VizEdge(
                    source=d["source"],
                    target=d["target"],
                    kind="depends",
                ))
        except Exception:
            pass
        
        conn.close()
    except Exception:
        pass
    
    return graph


def _build_timeline_graph() -> VizGraph:
    """Build a timeline visualization from today's activity."""
    graph = VizGraph(title="Activity Timeline")
    
    try:
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        
        from ..db import connect
        conn = connect()
        
        # Get today's daemon cycles.
        cycles = conn.execute(
            """SELECT started_at, finished_at, outcome, repos_scanned
               FROM watch_history
               WHERE date(started_at) = ?
               ORDER BY started_at DESC
               LIMIT 10""",
            (today,),
        ).fetchall()
        
        for c in cycles:
            time_str = c["started_at"][11:19] if c["started_at"] else "?"
            label = f"Cycle {time_str}"
            detail = f"{c['outcome']} — {c.get('repos_scanned', 0)} repos"
            node = VizNode(
                id=f"cycle_{c['started_at']}",
                label=label,
                kind="phase",
                detail=detail,
            )
            graph.nodes.append(node)
        
        # Get today's actions.
        actions = conn.execute(
            """SELECT created_at, action_type, target
               FROM actions
               WHERE date(created_at) = ?
               ORDER BY created_at DESC
               LIMIT 15""",
            (today,),
        ).fetchall()
        
        for a in actions:
            time_str = a["created_at"][11:19] if a["created_at"] else "?"
            label = f"{a['action_type']}: {a['target'][:40]}"
            node = VizNode(
                id=f"action_{a['created_at']}",
                label=label,
                kind="file",
            )
            graph.nodes.append(node)
        
        conn.close()
    except Exception:
        pass
    
    return graph


def _build_impact_graph(symbol: str) -> VizGraph:
    """Build an impact tree for a symbol from workspace data."""
    graph = VizGraph(title=f"Impact Analysis: {symbol}")
    
    try:
        from ..impact import analyze_symbol
        from ..db import connect
        conn = connect()
        result = analyze_symbol(symbol, conn, depth=3)
        conn.close()
        
        if result and result.affected:
            root = VizNode(id=symbol, label=symbol, kind="symbol")
            for aff in result.affected[:20]:
                node = VizNode(
                    id=f"{symbol}/{aff}",
                    label=aff,
                    kind="file" if "." in aff else "module",
                )
                root.children.append(node)
            graph.nodes.append(root)
        else:
            graph.nodes.append(VizNode(
                id=symbol,
                label=symbol,
                kind="symbol",
                detail="No impact data found",
            ))
    except Exception:
        graph.nodes.append(VizNode(id=symbol, label=symbol, kind="symbol", detail="Impact analysis unavailable"))
    
    return graph


# ──────────────────────────────────────────────────────────────────────────
# Renderers
# ──────────────────────────────────────────────────────────────────────────


def render_tree(graph: VizGraph, indent: str = "  ") -> str:
    """Render a VizGraph as an indented ASCII tree."""
    lines: list[str] = []
    lines.append(f"# {graph.title}")
    lines.append("")

    def _render_node(node: VizNode, depth: int = 0) -> None:
        prefix = indent * depth
        icon = _kind_icon(node.kind)
        detail = f"  — {node.detail}" if node.detail else ""
        lines.append(f"{prefix}{icon} {node.label}{detail}")
        for child in node.children:
            _render_node(child, depth + 1)

    for node in graph.nodes:
        _render_node(node)
        if graph.edges:
            lines.append("")

    # Add edges section.
    if graph.edges:
        lines.append("")
        lines.append("Dependencies:")
        for edge in graph.edges:
            arrow = "→"
            label = f"  [{edge.label}]" if edge.label else ""
            lines.append(f"  {edge.source} {arrow} {edge.target}{label}")

    return "\n".join(lines)


def render_mermaid(graph: VizGraph) -> str:
    """Render a VizGraph as Mermaid.js flowchart markup."""
    lines: list[str] = []
    lines.append("```mermaid")
    lines.append("flowchart LR")
    lines.append(f"  title[{graph.title}]")

    # Add nodes.
    def _add_node(node: VizNode) -> str:
        node_id = node.id.replace(" ", "_").replace("/", "_").replace("-", "_")
        kind_class = _mermaid_class(node.kind)
        lines.append(f"  {node_id}[{node.label}]{kind_class}")
        for child in node.children:
            child_id = _add_node(child)
            lines.append(f"  {node_id} --> {child_id}")
        return node_id

    for node in graph.nodes:
        _add_node(node)

    # Add edges.
    for edge in graph.edges:
        src = edge.source.replace(" ", "_").replace("/", "_").replace("-", "_")
        tgt = edge.target.replace(" ", "_").replace("/", "_").replace("-", "_")
        label = f"|{edge.label}|" if edge.label else ""
        lines.append(f"  {src} -->{label} {tgt}")

    lines.append("```")
    return "\n".join(lines)


def render_image(graph: VizGraph, output_path: Path) -> bool:
    """Render a VizGraph as an SVG image via graphviz.

    Returns True if successful, False otherwise.
    """
    try:
        # Generate DOT format.
        dot_lines = ['digraph {']
        dot_lines.append(f'  label="{graph.title}";')
        dot_lines.append('  rankdir=LR;')

        def _add_dot_node(node: VizNode) -> str:
            nid = node.id.replace(" ", "_").replace("/", "_").replace("-", "_")
            shape = {"project": "folder", "module": "box", "file": "note",
                     "symbol": "ellipse", "phase": "cds"}.get(node.kind, "box")
            dot_lines.append(f'  "{nid}" [label="{node.label}", shape={shape}];')
            for child in node.children:
                cid = _add_dot_node(child)
                dot_lines.append(f'  "{nid}" -> "{cid}";')
            return nid

        for node in graph.nodes:
            _add_dot_node(node)

        for edge in graph.edges:
            src = edge.source.replace(" ", "_").replace("/", "_").replace("-", "_")
            tgt = edge.target.replace(" ", "_").replace("/", "_").replace("-", "_")
            label = f' [label="{edge.label}"]' if edge.label else ""
            dot_lines.append(f'  "{src}" -> "{tgt}"{label};')

        dot_lines.append("}")
        dot_source = "\n".join(dot_lines)

        # Try graphviz.
        proc = subprocess.run(
            ["dot", "-Tsvg"],
            input=dot_source,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode == 0 and proc.stdout:
            output_path.write_text(proc.stdout, encoding="utf-8")
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    except Exception:
        pass

    return False


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


def _kind_icon(kind: str) -> str:
    icons = {
        "project": "📁",
        "module": "📂",
        "file": "📄",
        "symbol": "🔧",
        "phase": "▶",
        "error": "❌",
    }
    return icons.get(kind, "•")


def _mermaid_class(kind: str) -> str:
    classes = {
        "project": ":::project",
        "module": ":::module",
        "file": ":::file",
        "symbol": ":::symbol",
        "phase": ":::phase",
    }
    return classes.get(kind, "")


def _build_graph(kind: str, target: Optional[str] = None) -> VizGraph:
    """Build a VizGraph for the given visualization kind."""
    builders = {
        "arch": lambda: _build_project_tree(target),
        "deps": lambda: _build_dep_graph(),
        "timeline": lambda: _build_timeline_graph(),
        "impact": lambda: _build_impact_graph(target or ""),
    }
    builder = builders.get(kind)
    if not builder:
        return VizGraph(title=f"Unknown visualization: {kind}", nodes=[
            VizNode(id="error", label=f"Unknown kind: {kind}", kind="error"),
        ])
    return builder()


# ──────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────


def visualize(kind: str, target: Optional[str] = None,
              fmt: str = "tree", output: Optional[Path] = None) -> str:
    """Generate a visualization.

    Args:
        kind: One of "arch", "deps", "timeline", "impact".
        target: Optional target (repo path for arch, symbol for impact).
        fmt: Output format: "tree", "mermaid", "image".
        output: Output path (required for "image" format).

    Returns:
        The rendered visualization as a string (for tree/mermaid formats).
        For image format, returns the path to the saved file.
    """
    graph = _build_graph(kind, target)

    if fmt == "mermaid":
        return render_mermaid(graph)

    if fmt == "image":
        if not output:
            output = Path(f"{graph.title.lower().replace(' ', '_')}.svg")
        success = render_image(graph, output)
        if success:
            return f"Saved to {output}"
        return "Image generation requires graphviz (dot). Install it and try again."

    # Default: tree format.
    return render_tree(graph)
