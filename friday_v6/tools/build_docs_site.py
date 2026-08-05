"""Wave 12 docs-site generator — pure-stdlib markdown → static HTML.

Reads every ``*.md`` in ``docs/`` and writes a self-contained static
site to ``site/`` (index + one page per doc) with zero dependencies:
a small markdown renderer covers the constructs used by the V4 docs
(headings, paragraphs, lists, fenced code blocks, tables, links,
blockquotes, bold/inline code). Everything else degrades to paragraphs.

Usage:
    python tools/build_docs_site.py [--out site] [--docs docs]

Deterministic and idempotent: same docs in → same HTML out.
"""

from __future__ import annotations

import argparse
import html
import re
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PROJECT = _HERE.parent


# ─────────────────────────────────────────────────────────────────────
# Minimal markdown → HTML (subset used by the V4 docs)
# ─────────────────────────────────────────────────────────────────────


def _inline(text: str) -> str:
    """Inline formatting: code, bold, links. Escapes HTML first."""
    text = html.escape(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(
        r"\[([^\]]+)\]\(([^)\s]+)\)",
        lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
    return text


def _render_fenced(code: str) -> str:
    lang = code.splitlines()[0].strip() if code.strip() else ""
    if lang.startswith("```"):
        lang = lang[3:].strip()
    body = html.escape("\n".join(code.splitlines()[1:]))
    return f'<pre><code class="language-{html.escape(lang)}">{body}</code></pre>'


def _render_table(rows: list[str]) -> str:
    # rows[1] is a separator row (e.g. |---|---|) when present; skip it.
    has_separator = len(rows) > 2 and re.match(
        r"^\s*\|[\s:\-|]+\|\s*$", rows[1])
    header = rows[0]
    body = rows[2:] if has_separator else rows[1:]
    th = "".join(f"<th>{_inline(c.strip())}</th>" for c in header)
    trs = []
    for row in body:
        cells = row.split("|")
        if not cells:
            continue
        if len(cells) == 2 and not cells[0].strip() and not cells[1].strip():
            continue
        trs.append("<tr>" + "".join(
            f"<td>{_inline(c.strip())}</td>" for c in cells) + "</tr>")
    return f"<table><thead><tr>{th}</tr></thead><tbody>{''.join(trs)}</tbody></table>"


def md_to_html(md: str) -> str:
    """Render a markdown document body to HTML."""
    out: list[str] = []
    lines = md.splitlines()
    i = 0
    in_code = False
    code_buf: list[str] = []
    list_buf: list[str] = []
    table_buf: list[str] = []

    def flush_list() -> None:
        if list_buf:
            items = []
            for l in list_buf:
                item = l.strip()
                # Strip the bullet marker ('- x' / '* x') without regex
                # escaping pitfalls.
                if item[:2] in ("- ", "* "):
                    item = item[2:].strip()
                items.append(f"<li>{_inline(item)}</li>")
            out.append("<ul>" + "".join(items) + "</ul>")
            list_buf.clear()

    def flush_table() -> None:
        if len(table_buf) >= 1:
            out.append(_render_table(table_buf))
            table_buf.clear()

    while i < len(lines):
        line = lines[i]
        if in_code:
            if line.strip().startswith("```"):
                out.append(_render_fenced("\n".join(code_buf)))
                code_buf.clear()
                in_code = False
            else:
                code_buf.append(line)
            i += 1
            continue
        if line.strip().startswith("```"):
            flush_list(); flush_table()
            in_code = True
            code_buf = [line]
            i += 1
            continue
        if re.match(r"^\s*[-*]\s+", line):
            flush_table()
            list_buf.append(line)
            i += 1
            continue
        if line.strip().startswith("|"):
            flush_list()
            table_buf.append(line)
            i += 1
            continue
        flush_list(); flush_table()
        if line.strip().startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            text = _inline(line.strip("# ").strip())
            out.append(f"<h{level}>{text}</h{level}>")
        elif line.strip().startswith(">"):
            out.append(f"<blockquote>{_inline(line.strip('> ').strip())}"
                       "</blockquote>")
        elif line.strip() == "---":
            out.append("<hr>")
        elif line.strip():
            out.append(f"<p>{_inline(line.strip())}</p>")
        i += 1
    flush_list(); flush_table()
    if code_buf:
        out.append(_render_fenced("\n".join(code_buf)))
    return "\n".join(out)


# ─────────────────────────────────────────────────────────────────────
# Site assembly
# ─────────────────────────────────────────────────────────────────────

_PAGE_TMPL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Friday V4 Docs</title>
<style>
  :root {{ --bg:#0a0e17; --panel:#111a2c; --line:#1e2b44; --text:#dfe6f3;
           --dim:#7c8aa5; --cyan:#38d4f5; --green:#3ddc97; --code:#0d1524; }}
  * {{ box-sizing:border-box; }}
  body {{ background:var(--bg); color:var(--text); margin:0;
         font-family:ui-sans-serif,system-ui,sans-serif; line-height:1.65; }}
  .wrap {{ max-width:880px; margin:0 auto; padding:32px 24px 80px; }}
  header {{ border-bottom:1px solid var(--line); padding-bottom:14px;
           margin-bottom:26px; display:flex; align-items:baseline; gap:16px;
           flex-wrap:wrap; }}
  .logo {{ color:var(--cyan); font-weight:700; letter-spacing:2px;
          font-size:20px; text-decoration:none; }}
  nav a {{ color:var(--dim); font-size:13px; margin-right:12px;
          text-decoration:none; }}
  nav a:hover {{ color:var(--cyan); }}
  h1 {{ font-size:28px; }} h2 {{ font-size:20px; margin-top:34px;
       color:var(--cyan); }} h3 {{ font-size:16px; margin-top:24px; }}
  code {{ background:var(--code); border:1px solid var(--line);
         padding:2px 6px; border-radius:6px; font-size:.9em; }}
  pre {{ background:var(--code); border:1px solid var(--line);
        padding:14px; border-radius:10px; overflow-x:auto; }}
  pre code {{ border:none; padding:0; }}
  table {{ border-collapse:collapse; width:100%; margin:14px 0; }}
  th,td {{ border:1px solid var(--line); padding:8px 10px; text-align:left;
          font-size:14px; }}
  th {{ background:var(--panel); color:var(--cyan); }}
  a {{ color:var(--green); }}
  blockquote {{ border-left:3px solid var(--cyan); margin:14px 0;
               padding:6px 14px; background:var(--panel);
               border-radius:0 8px 8px 0; color:var(--dim); }}
  hr {{ border:none; border-top:1px solid var(--line); margin:24px 0; }}
  footer {{ margin-top:48px; color:var(--dim); font-size:12px;
           text-align:center; }}
</style>
</head>
<body>
<div class="wrap">
<header>
  <a class="logo" href="index.html">◆ FRIDAY V4</a>
  <nav><a href="index.html">Docs</a></nav>
</header>
{body}
<footer>Friday V4 — local-first documentation site (generated by tools/build_docs_site.py)</footer>
</div>
</body>
</html>
"""


def build_site(docs_dir: Path, out_dir: Path) -> list[Path]:
    """Generate the site; returns the written file paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    docs = sorted(docs_dir.glob("*.md"))
    written: list[Path] = []

    # Index page with a doc list.
    items = "".join(
        f'<li><a href="{p.stem}.html">{p.stem.replace("_", " ").title()}'
        f"</a></li>" for p in docs)
    index_body = f"<h1>Friday V4 — Documentation</h1><ul>{items}</ul>"
    index_path = out_dir / "index.html"
    index_path.write_text(_PAGE_TMPL.format(
        title="Docs", body=index_body))
    written.append(index_path)

    for doc in docs:
        md = doc.read_text(encoding="utf-8")
        title = doc.stem.replace("_", " ").title()
        body = f"<h1>{html.escape(title)}</h1>\n" + md_to_html(md)
        page = out_dir / f"{doc.stem}.html"
        page.write_text(_PAGE_TMPL.format(title=title, body=body))
        written.append(page)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build_docs_site")
    parser.add_argument("--docs", type=Path, default=_PROJECT / "docs")
    parser.add_argument("--out", type=Path, default=_PROJECT / "site")
    args = parser.parse_args(argv)

    written = build_site(args.docs, args.out)
    print(f"✓ wrote {len(written)} pages to {args.out}")
    for p in written:
        print(f"  {p.relative_to(_PROJECT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
