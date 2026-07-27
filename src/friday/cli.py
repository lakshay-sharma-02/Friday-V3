"""Friday V3 command-line interface."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from a `.env` (cwd, else package root) into the
    environment without overriding already-set vars. No dependency — the spec
    forbids adding one. Silent on any error so the CLI never breaks on config."""
    for path in (Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip("'\""))
        return


from .ask import Exchange, ask
from .architecture import analyze_and_store
from .cli_knowledge import cmd_knowledge
from .cli_understanding import cmd_understanding
from .cli_initiative import cmd_initiatives
from .cli_insight import cmd_insights
from .cli_identity import cmd_identity
from .cli_portfolio import cmd_portfolio_dispatch
from .cli_profile import cmd_profile
from .cli_strategy import cmd_strategy
from .cli_planning import cmd_plan
from .cli_graph import cmd_graph
from .cli_worker import cmd_worker
from .cli_resolver import cmd_resolve, cmd_resolver
from .cli_scheduler import cmd_schedule, cmd_scheduler
from .cli_review import cmd_review
from .cli_execute import cmd_execute
from .cli_runtime import (
    cmd_runtime,
    cmd_runtime_dispatch,
    cmd_runtime_show,
    cmd_runtime_export,
)
from .cli_capability import cmd_capability
from .cli_watch import cmd_watch
from .cli_suggest import cmd_suggest
from .cli_repair import cmd_repair
from .cli_integration import cmd_integrate
from .cli_daemon import cmd_daemon
from .cli_patterns import cmd_patterns
from .cli_actions import cmd_actions
from .cli_skills import cmd_skills
from .cli_autonomy import cmd_autonomy
from .cli_email import cmd_email
from .cli_slack import cmd_slack
from .cli_discord import cmd_discord
from .cli_telegram import cmd_telegram
from .cli_nl import cmd_do
from .project_session import (
    ProjectSession,
    format_suggestions,
    list_templates,
    get_template,
    scaffold_from_template,
)
from .utils import _strip_code_fences
from .context import ContextEngine


from .db import connect
from .doctor import cmd_doctor
from .ingest import ingest_paths
from .observe import format_report, observe, observe_via_engine
from .observation import default_registry, format_run
from .summary import generate_summary
from .cross_project import (
    run_correlation,
    structural_pass,
    semantic_pass,
    scan_project_docs,
    format_correlations,
)


def cmd_ingest(args: argparse.Namespace) -> int:
    paths = [Path(p).expanduser() for p in args.paths]
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        print(f"error: path(s) not found: {', '.join(missing)}", file=sys.stderr)
        return 2
    conn = connect()
    report = ingest_paths(paths, conn)
    conn.close()
    print(
        f"Ingested {report.repos_stored} of {report.repos_found} repositories "
        f"({report.llm_summaries} with LLM README summaries)."
    )
    return 0


def cmd_correlate(args: argparse.Namespace) -> int:
    """Run or inspect cross-project correlations."""
    from .db import get_repositories

    conn = connect()
    try:
        if args.scan_docs:
            n = scan_project_docs(conn)
            print(f"Scanned project docs: {n} new/updated.")
            return 0

        if args.detail:
            repo_a, repo_b = args.detail
            pairs = structural_pass(conn)
            matching = [p for p in pairs
                        if p["repo_a_name"] == repo_a and p["repo_b_name"] == repo_b
                        or p["repo_a_name"] == repo_b and p["repo_b_name"] == repo_a]
            if matching:
                p = matching[0]
                print(f"Correlation: {p['repo_a_name']} ↔ {p['repo_b_name']}")
                print(f"  Structural score: {p['structural_score']}")
                print(f"  Volatility (recency): {p['volatility']}")
                print(f"  Adjusted score: {p['adjusted_score']}")
                for e in p.get("evidence", []):
                    print(f"  • {e}")
            else:
                print(f"No correlation data for {repo_a} ↔ {repo_b}.")
                print("Run `friday correlate` first to compute.")
            return 0

        # Full pipeline.
        print("Running cross-project correlation...")
        results = run_correlation(conn)
        if results:
            print(format_correlations(results))
            print(f"\n{len(results)} high-confidence correlation(s) promoted to Insights.")
            print("View them: friday insights")
        else:
            print("No high-confidence correlations found.")
            print("Repos may need more activity or project docs to produce signal.")
        return 0
    finally:
        conn.close()


def cmd_summary(args: argparse.Namespace) -> int:
    """Show the workspace knowledge summary."""
    from .presentation.cli_format import header, gray

    conn = connect()
    text = generate_summary(conn)
    conn.close()

    if not text.strip():
        print(header("Workspace", "no projects tracked"))
        print(gray("\n  No projects discovered yet. Run `friday ingest` first."))
        return 0

    # Extract project count from first line.
    lines = text.splitlines()
    count = "0"
    if lines and "Projects discovered:" in lines[0]:
        count = lines[0].split(":")[-1].strip()

    print(header("Workspace", f"{count} projects"))
    print()
    # Print the raw rendered summary after the header.
    # The summary module handles formatting of individual project sections.
    for line in lines[1:]:
        print(line)

    return 0


def cmd_create_file(args: argparse.Namespace) -> int:
    """Create files directly — now with multi-file, path support, and proactive suggestions.

    Enhanced features over the original:
    - Path-aware: keeps directory components in filename
    - Multi-file: generates complete project structures from one prompt
    - Session-aware: attaches to active project session if one exists
    - Existing project: reads existing context when working in a non-empty dir
    - Proactive suggestions: suggests enhancements after creation

    Used by ``friday do create a Flask app named ~/projects/myapp``.
    """
    from .presentation.cli_format import header, green, gray, yellow, error as perror

    filename = getattr(args, "filename", "output.py")
    description = getattr(args, "description", "")
    output_path = getattr(args, "path", None)
    multi = getattr(args, "multi", False)

    # Determine the target directory and filename.
    cwd = Path(output_path).expanduser().resolve() if output_path else Path.cwd()
    target = Path(filename)
    if target.is_absolute():
        cwd = target.parent
        filename = target.name
    elif len(target.parts) > 1:
        # Relative path like "src/utils.py" -> cwd/src/utils.py
        cwd = (cwd / target.parent).resolve()
        filename = target.name

    # Check for existing project context.
    session = ProjectSession.active()
    existing_context = ""
    if session and str(session.root_path) == str(cwd):
        existing_context = session.get_file_tree()
        existing_context += "\n" + session.get_conversation_context(max_turns=5)
    elif cwd.exists() and list(cwd.iterdir()):
        # Non-empty directory without a session — scan existing files.
        existing_files = list(cwd.glob("*"))[:20]
        if existing_files:
            existing_context = "Existing files in this directory:\n"
            for f in existing_files:
                existing_context += f"  {'📁' if f.is_dir() else '📄'} {f.name}\n"

    # Check if we should do multi-file generation (project-like request).
    is_project = multi or any(kw in description.lower() for kw in
                              ["app", "project", "website", "server", "api", "cli",
                               "tool", "microservice", "service", "script"])

    if is_project or len(target.parts) > 1:
        print(header("scaffold", filename if not is_project else description[:60]))
        print()
        return _generate_multi_file(cwd, filename, description, existing_context, session)

    return _generate_single_file(cwd, filename, description, existing_context, session)



def _parse_multi_file_output(raw: str) -> list[tuple[str, str]]:
    """Parse multi-file LLM output into (relative_path, content) pairs.

    Expected format:
        ## path/to/file.py
        content...
        ## path/to/file2.py
        content...

    Or markdown code fences:
        ```python path/to/file.py
        content...
        ```
        ```python path/to/file2.py
        content...
        ```
    """
    files: list[tuple[str, str]] = []

    # Try ## header format first.
    blocks = re.split(r"^##\s+(.+)$", raw, flags=re.MULTILINE)
    if len(blocks) >= 3:
        for i in range(1, len(blocks), 2):
            if i + 1 < len(blocks):
                path = blocks[i].strip()
                content = blocks[i + 1].strip()
                # Strip ``` fences from content.
                content = _strip_code_fences(content)
                if path and content:
                    files.append((path, content))

    # Try markdown fence format if ## didn't work.
    if not files:
        fence_pattern = re.compile(
            r"```(?:\w+)?\s*(.+?)\n(.*?)```", re.DOTALL
        )
        for m in fence_pattern.finditer(raw):
            path = m.group(1).strip()
            content = m.group(2).strip()
            if path and content and not path.startswith("{"):
                files.append((path, content))

    return files


def _generate_single_file(
    cwd: Path, filename: str, description: str,
    existing_context: str, session: ProjectSession | None,
) -> int:
    """Generate a single file via LLM and write it."""
    from .presentation.cli_format import green, gray, error as perror
    from .services.llm import _call as _llm_call

    path = cwd / filename
    print(gray(f"  Generating {filename}..."))

    system = (
        "You generate code. Output ONLY the file content. "
        "No explanations, no markdown fences, no extra text. "
        "Just the raw code."
    )
    user = f"Write {filename} that does: {description}"
    if existing_context:
        user += f"\n\nProject context:\n{existing_context}"

    content = _llm_call(system, user)
    if content:
        content = _strip_code_fences(content)

    if not content:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext in ("py",):
            content = f'"""{description}"""\n\n\ndef main():\n    pass\n\n\nif __name__ == "__main__":\n    main()\n'
        else:
            content = f"# {description}\n"

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        print(perror(f"Failed to write {filename}: {exc}"))
        return 1

    print(green(f"  Created {filename}"))

    # Track in active session if applicable.
    if session and str(session.root_path) == str(cwd):
        session.add_file(filename, content, created=True)
        session.add_exchange(f"create {filename}", "created", f"wrote {filename} ({len(content.splitlines())} lines)")
        session.save_active()

    # Show preview.
    lines_content = content.splitlines()
    preview = lines_content[:5]
    print()
    print(gray("  Preview:"))
    for line in preview:
        print(f"    {line}")
    if len(lines_content) > 5:
        print(gray(f"    ... ({len(lines_content)} lines total)"))

    # Proactive suggestions.
    _show_enhancements(cwd, filename, content, session)

    return 0


def _generate_multi_file(
    cwd: Path, filename: str, description: str,
    existing_context: str, session: ProjectSession | None,
) -> int:
    """Generate a multi-file project structure via LLM."""
    from .presentation.cli_format import green, gray, yellow, error as perror
    from .services.llm import _call as _llm_call

    print(gray(f"  Designing project structure for: {description[:60]}..."))

    # Determine project type from existing context or description.
    project_type = None
    desc_lower = description.lower()
    for ptype in ("flask", "fastapi", "django", "react", "vue", "cli",
                  "python", "node", "express", "next", "rust", "go"):
        if ptype in desc_lower:
            project_type = ptype
            break

    system = (
        "You are a senior software architect scaffolding a project. "
        "Output a COMPLETE, production-quality project with multiple files.\n\n"
        "Use this format EXACTLY (separate each file with ## path):\n\n"
        "## path/to/file1.py\n"
        "content of file 1\n\n"
        "## path/to/file2.py\n"
        "content of file 2\n\n"
        "IMPORTANT:\n"
        "- Every file must have COMPLETE, working code — no TODOs or placeholders\n"
        "- Include a README.md with setup/usage instructions\n"
        "- Include requirements.txt or package.json with dependencies\n"
        "- Make it impressive: error handling, type hints, docstrings, proper structure\n"
        "- Output ONLY the files in ## format — no extra commentary"
    )
    user = f"Scaffold a complete project: {description}"
    if existing_context:
        user += f"\n\nWorking directory context:\n{existing_context}"

    raw = _llm_call(system, user)
    if not raw:
        print(yellow("  LLM unavailable — falling back to single file."))
        return _generate_single_file(cwd, filename, description, existing_context, session)

    files = _parse_multi_file_output(raw)
    if not files:
        print(yellow("  Couldn't parse multi-file output — creating as single file."))
        content = _strip_code_fences(raw)
        if content:
            files = [(filename, content)]
        else:
            files = [(filename, f"# {description}\n")]

    # Start or reuse a project session.
    if not session or str(session.root_path) != str(cwd):
        session = ProjectSession.start(str(cwd), project_type=project_type)
        print(gray(f"  Started project session at {cwd}"))
    elif session.project_type is None and project_type:
        session.project_type = project_type

    written = []
    for rel_path, content in files:
        full_path = (cwd / rel_path).resolve()
        try:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
            session.add_file(rel_path, content, created=True)
            written.append(rel_path)
            print(green(f"  ✓ {rel_path}"))
        except OSError as exc:
            print(perror(f"  ✗ {rel_path}: {exc}"))

    session.add_exchange(
        description, "scaffolded",
        f"created {len(written)} files in {cwd.name}",
    )
    session.save_active()

    print()
    print(gray(f"  Created {len(written)} files in {cwd}"))
    print()

    # Proactive enhancement suggestions.
    _show_enhancements(cwd, "", description, session)

    return 0


def _show_enhancements(
    cwd: Path, filename: str, content: str,
    session: ProjectSession | None,
) -> None:
    """Show proactive enhancement suggestions after file/project creation."""
    from .presentation.cli_format import gray, yellow
    from .services.llm import _call as _llm_call, _enabled

    if not _enabled():
        return

    # Brief pause to indicate thinking.
    print(gray("  Thinking about what else could make this better..."))

    if session:
        suggestions = session.suggest_enhancements(_llm_call)
    else:
        # No session — do one-shot suggestions for this single file.
        temp_session = ProjectSession(str(cwd))
        temp_session.add_file(filename, content, created=True)
        suggestions = temp_session.suggest_enhancements(_llm_call)

    if suggestions:
        print()
        print(yellow("  💡 Want me to add any of these?"))
        for s in suggestions[:5]:
            mark = {"high": "🔥", "medium": "💡", "low": "📝"}.get(s.priority, "💡")
            print(f"    {mark} {s.title}")
            if s.description:
                print(f"       {s.description}")
        print()
        print(gray("  Just say: friday do 'add <suggestion>'"))


def cmd_project(args: argparse.Namespace) -> int:
    """Manage persistent project sessions — start, continue, show status, end.

    ``friday project start <path> [--type <type>]``    Start a new session
    ``friday project status``                            Show active session
    ``friday project continue``                          Resume active session info
    ``friday project end``                               End active session
    ``friday project add <file> <description>``          Create file in active session
    ``friday project suggest``                           Show enhancement suggestions
    ``friday project edit <file> <description>``         Modify existing file
    ``friday project template <name> [--output <path>]`` Scaffold from a template
    ``friday project list-templates``                    Show available templates
    ``friday project chat``                              Interactive REPL session
    """
    from .presentation.cli_format import header, green, gray, yellow, error as perror
    from .services.llm import _call as _llm_call, _enabled as _llm_enabled

    action = getattr(args, "action", "status")

    if action == "start":
        path = getattr(args, "path", None) or getattr(args, "target", ".")
        project_type = getattr(args, "project_type", None)
        root = Path(path).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        session = ProjectSession.start(str(root), project_type=project_type)
        print(header("project", f"started at {root}"))
        print(green(f"  Project session started"))
        print(f"  Path:        {root}")
        print(f"  Type:        {session.project_type or 'auto-detect'}")
        print(f"  Session ID:  {session.session_id}")
        print()
        print(gray("  You can now use 'friday do' commands and they'll"))
        print(gray("  automatically attach to this session:"))
        print(gray("    friday do create a Flask app in this project"))
        print(gray("    friday do add authentication to this project"))
        print(gray("    friday do add a database model"))
        return 0

    if action == "status":
        session = ProjectSession.active()
        if not session:
            print(header("project", "no active session"))
            print()
            print("  No active project session.")
            print()
            print("  Start one:")
            print(gray("    friday project start ~/projects/myapp"))
            print(gray("    friday do create a Flask app in ~/projects/myapp"))
            return 0
        print(header("project", session.root_path.name))
        print(f"  Path:        {session.root_path}")
        print(f"  Type:        {session.project_type or 'unknown'}")
        print(f"  Files:       {session.file_count} ({len(session.created_files)} created)")
        print(f"  Exchanges:   {len(session.conversation)}")
        print()
        print(session.get_file_tree())
        if session.suggestions:
            print()
            print(gray("  Pending suggestions:"))
            print(format_suggestions(session.suggestions))
        if session.conversation:
            print()
            print(gray("  Recent activity:"))
            for ex in session.conversation[-3:]:
                print(f"    • {ex.action_taken}: {ex.result[:80]}")
        return 0

    if action == "continue":
        session = ProjectSession.active()
        if not session:
            print("No active session to continue.")
            print("Start one: friday project start <path>")
            return 1
        print(header("project", f"continuing {session.root_path.name}"))
        print(green(f"  Resumed session at {session.root_path}"))
        print(gray(f"  {session.file_count} files tracked, {len(session.conversation)} prior exchanges"))
        return 0

    if action == "end":
        if ProjectSession.end_active():
            print(green("  Project session ended."))
        else:
            print("  No active session to end.")
        return 0

    if action == "add":
        session = ProjectSession.active()
        if not session:
            print("No active session. Start one: friday project start <path>")
            return 1
        target = getattr(args, "target", "")
        desc = getattr(args, "description", "")
        if not target:
            print("Specify what to create, e.g.: friday project add main.py 'a CLI entry point'")
            return 1
        ns = argparse.Namespace(
            filename=target,
            description=desc or target,
            path=str(session.root_path),
            multi=False,
        )
        return cmd_create_file(ns)

    if action == "edit":
        session = ProjectSession.active()
        if not session:
            print("No active session. Start one: friday project start <path>")
            return 1
        target = getattr(args, "target", "")
        desc = getattr(args, "description", "")
        if not target:
            print("Specify what to edit, e.g.: friday project edit main.py 'add error handling'")
            return 1

        existing = session.read_file(target)
        if existing is None:
            print(yellow(f"  File '{target}' doesn't exist yet — will create."))
        else:
            print(gray(f"  Reading existing {target} ({len(existing.splitlines())} lines)..."))

        system = (
            "You modify or create a file. Output ONLY the COMPLETE new file content. "
            "No explanations, no markdown fences, no extra text. "
            "Replace the entire file with the improved version."
        )
        existing_ctx = f"\n\nExisting content:\n{existing}" if existing else ""
        user = f"{desc}\n\nFile: {target}{existing_ctx}"

        content = _llm_call(system, user)
        if content:
            content = _strip_code_fences(content)

        if not content:
            print(perror(f"Failed to generate content for {target}"))
            return 1

        session.modify_file(target, content)
        session.add_exchange(f"edit {target}: {desc}", "modified", f"updated {target}")
        session.save_active()

        print(green(f"  ✓ Updated {target}"))
        print()
        lines_c = content.splitlines()
        for line in lines_c[:5]:
            print(f"    {line}")
        if len(lines_c) > 5:
            print(gray(f"    ... ({len(lines_c)} lines total)"))

        return 0

    if action == "suggest":
        session = ProjectSession.active()
        if not session:
            print("No active session.")
            return 1
        suggestions = session.suggest_enhancements(_llm_call)
        if suggestions:
            print(header("project", "enhancement suggestions"))
            print(format_suggestions(suggestions))
        else:
            print("No suggestions available.")
        return 0

    # --- Project Templates ---
    if action == "list-templates":
        templates = list_templates()
        if not templates:
            print("No templates available.")
            return 0
        print(header("project", "available templates"))
        print()
        for t in templates:
            print(t.describe())
            print()
        print(gray("  Use: friday project template <name> --output <path>"))
        print(gray("  Or:  friday do scaffold a flask app in ~/projects/myapp"))
        return 0

    if action in ("template", "scaffold"):
        template_name = getattr(args, "target", "")
        if not template_name:
            print("Specify a template name.")
            print("Available: " + ", ".join(t.name for t in list_templates()))
            return 1

        tmpl = get_template(template_name)
        if not tmpl:
            print(f"Unknown template: {template_name}")
            print("Available: " + ", ".join(t.name for t in list_templates()))
            return 1

        output_path = getattr(args, "path", None) or getattr(args, "description", tmpl.default_output)
        root = Path(output_path).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)

        session = ProjectSession.start(str(root), project_type=tmpl.project_type)
        print(header("project", f"scaffolding {tmpl.name} at {root}"))
        print()

        if _llm_enabled():
            print(gray(f"  Generating {tmpl.name} project..."))
            created = scaffold_from_template(session, tmpl, _llm_call, root.name)
        else:
            print(yellow("  LLM unavailable — using static content only."))
            created = scaffold_from_template(session, tmpl, lambda s, u: "", root.name)

        session.add_exchange(
            f"scaffold {tmpl.name}", "scaffolded",
            f"created {created} files from {tmpl.name} template",
        )
        session.save_active()

        print(green(f"  ✓ Created {created} files in {root}"))
        print()
        print(session.get_file_tree())
        print()
        print(gray("  Continue building: friday project chat"))

        # Proactive suggestions for what else to add.
        if _llm_enabled():
            _show_enhancements(root, "", f"{tmpl.name} app via template", session)

        return 0

    # --- Interactive Chat Mode ---
    if action == "chat":
        session = ProjectSession.active()
        if not session:
            print("No active session. Start one first: friday project start <path>")
            print("Or scaffold one: friday project template flask --output ~/projects/myapp")
            return 1

        print(header("project chat", session.root_path.name))
        print(green(f"  Interactive project session at {session.root_path}"))
        print(gray(f"  {session.file_count} files, {len(session.conversation)} prior exchanges"))
        print(gray("  Type 'exit' or 'end' to quit. Type 'help' for commands."))
        print()

        while True:
            try:
                q = input("project> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not q:
                continue
            if q.lower() in ("exit", "quit", "end", "done"):
                break
            if q.lower() in ("help", "?"):
                print()
                print(gray("  Commands:"))
                print(gray("    create <desc>     — Create a file or scaffold a project"))
                print(gray("    edit <file> <desc> — Modify an existing file"))
                print(gray("    status            — Show project status"))
                print(gray("    suggest           — Get enhancement ideas"))
                print(gray("    template <name>   — Scaffold from a template"))
                print(gray("    list-templates    — Show available templates"))
                print(gray("    add <file> <desc> — Add a new file"))
                print(gray("    exit/end          — End session and quit"))
                print()
                continue
            if q.lower() in ("status", "s"):
                ns = argparse.Namespace(action="status", target=None, description=None,
                                        path=None, project_type=None)
                cmd_project(ns)
                continue
            if q.lower() in ("suggest", "ideas"):
                ns = argparse.Namespace(action="suggest", target=None, description=None,
                                        path=None, project_type=None)
                cmd_project(ns)
                continue
            if q.lower() == "list-templates":
                ns = argparse.Namespace(action="list-templates", target=None, description=None,
                                        path=None, project_type=None)
                cmd_project(ns)
                continue
            if q.lower().startswith("template "):
                name = q[9:].strip()
                ns = argparse.Namespace(action="template", target=name, description=None,
                                        path=str(session.root_path), project_type=None)
                cmd_project(ns)
                continue

            # Route through the NL dispatcher, but keep session context.
            from .cli_nl import classify_intent
            try:
                handler, ns = classify_intent(q)
            except Exception as exc:
                print(yellow(f"  Couldn't process: {exc}"))
                continue

            # If it's a project command, run it directly (keeps session).
            if hasattr(ns, "action"):
                handler(ns)
            elif handler.__name__ in ("cmd_create_file",):
                # Ensure create/file commands use the session path.
                if hasattr(ns, "path") and not ns.path:
                    ns.path = str(session.root_path)
                handler(ns)
            else:
                handler(ns)

            session.save_active()

        print(green(f"\n  Project session saved. {session.file_count} files total."))
        return 0

    print(f"Unknown project action: {action}")
    return 1


def cmd_ask(args: argparse.Namespace) -> int:
    conn = connect()
    answer = ask(args.question, conn, verbose=args.verbose)
    conn.close()
    if args.verbose:
        print("Question:")
        print(args.question)
        print("\nEvidence:")
        if answer.evidence.blocks:
            print("\n".join(f"- {b}" for b in answer.evidence.blocks))
        else:
            print("(no retrieved evidence)")
        cov = answer.evidence.raw.get("coverage_report")
        if cov:
            from .evidence_scope import format_coverage_report
            print("\n" + format_coverage_report(cov))
        audit = answer.evidence.raw.get("retrieval_audit")
        if audit:
            print("Retrieval audit:")
            print(f"  Objective: {audit['objective']}")
            print(f"  Providers requested: {', '.join(audit['providers_requested'])}")
            print(f"  Providers returned:  {', '.join(audit['providers_returned'])}")
            print(f"  Knowledge used:      {'yes' if audit['knowledge_used'] else 'no'}")
            print(f"  Confidence:          {audit['confidence']}")
            if answer.evidence.raw.get("widened"):
                print("  Coverage widened:    yes (adaptive expansion, once)")
        print(f"\n[synthesized via LLM: {answer.used_llm}]\n")
    print(answer.text)
    # Always show confidence — no --verbose needed for this signal.
    audit = answer.evidence.raw.get("retrieval_audit")
    confidence = audit.get("confidence") if audit else None
    if confidence:
        print(f"\n[Confidence: {confidence}]")
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    """Bounded conversational loop (M6.5D): remembers only the last exchange.

    Thin wrapper over ask(prev=...) — no new architecture, no persistence.
    """
    conn = connect()
    prev: Exchange | None = None
    print("Friday chat — type 'exit' to quit. I only remember the last thing we said.")
    while True:
        try:
            q = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not q:
            continue
        if q.lower() in ("exit", "quit"):
            break
        ans = ask(q, conn, prev=prev, verbose=args.verbose)
        prev = Exchange(q, ans)
        if args.verbose:
            print("[evidence]", "; ".join(ans.evidence.blocks) or "(none)")
        print(ans.text)
    conn.close()
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    """Extract and persist architectural knowledge for one repository."""
    from .discovery import Repo

    path = Path(args.repository).expanduser().resolve()
    git = path / ".git"
    if not (git.is_dir() or git.is_file()):
        print(f"error: not a git repository: {path}", file=sys.stderr)
        return 2
    conn = connect()
    profile = analyze_and_store(conn, Repo(path=path))
    conn.close()
    print(f"Analyzed {profile.path}")
    print(f"  Architecture: {profile.architecture}")
    print(f"  Components:   {', '.join(c.name for c in profile.components) or '(none detected)'}")
    print(f"  Entry points: {', '.join(f'{e.kind} ({e.detail})' for e in profile.entry_points) or '(none detected)'}")
    if profile.circular:
        print(f"  Circular deps: {len(profile.circular)}")
    return 0


def cmd_observe(args: argparse.Namespace) -> int:
    """Refresh the workspace knowledge stack from current repository state.

    `friday observe`                  -> refresh the whole workspace.
    `friday observe <repo>`           -> refresh only one repository.
    `friday observe --changed`        -> refresh only changed repositories.
    `friday observe --summary`        -> refresh + print the summary report.

    Composes the EXISTING pipeline (ingest -> knowledge -> understanding ->
    initiative -> insight). No new subsystem; change detection skips untouched
    repositories and every layer's build() is idempotent.
    """
    from .observe import refresh

    conn = connect()
    only_changed = bool(getattr(args, "changed", False))
    summary = bool(getattr(args, "summary", False))
    repo = getattr(args, "repo", None)
    repos = [repo] if repo else None

    rep = refresh(conn, repos=repos, only_changed=only_changed)
    conn.close()

    print(rep.to_text())
    if not summary and rep.changed_repos:
        print("Changed repositories:")
        for name in rep.changed_repos:
            print(f"  - {name}")
    return 0


def cmd_observers(args: argparse.Namespace) -> int:
    """List every registered observer and its health/summary."""
    conn = connect()
    reg = default_registry()
    print(f"Registered observers ({len(reg)}):\n")
    for obs in reg.all():
        h = obs.health(conn)
        state = h.status.value
        mark = "ok" if h.healthy else "!"
        print(f"  [{mark}] {obs.name}  ({state})")
        if h.detail and not h.healthy:
            print(f"       {h.detail}")
        try:
            print(f"       {obs.summarize(conn)}")
        except Exception as exc:
            print(f"       (summary unavailable: {exc})")
    conn.close()
    return 0


def _context_engine():
    conn = connect()
    return conn, ContextEngine(conn)


def cmd_context_build(args: argparse.Namespace) -> int:
    """WRITE: build engineering sessions from stored observations and persist."""
    conn, eng = _context_engine()
    result = eng.build()
    conn.close()
    print(result.to_text(), end="")
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    """Dispatch friday context [build|today]."""
    if getattr(args, "action", None) == "build":
        return cmd_context_build(args)
    conn, eng = _context_engine()
    sessions = eng.sessions()
    if not sessions:
        conn.close()
        print("Engineering context has not been built.\n")
        print("Run:\n")
        print("  friday context build\n")
        return 0
    if eng.is_stale():
        print("Engineering context is out of date.")
        print("Latest observations are newer than the current context.\n")
        print("Run:\n")
        print("  friday context build\n")
        print()
    from datetime import datetime, timezone
    if getattr(args, "action", None) == "today":
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        summ = eng.summary(day)
    else:
        summ = eng.summary()
    conn.close()

    # Format context summary
    print(f"Engineering Context — {summ.day}\n")
    print(f"Sessions: {summ.session_count}")
    print(f"Active time: {summ.estimated_active_min:.1f} min")
    print(f"Context switches: {summ.context_switches}")
    if summ.most_active_repo:
        print(f"Most active: {summ.most_active_repo}")
    if summ.current_focus:
        print(f"Current focus: {summ.current_focus}")
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    """READ-ONLY: list all engineering sessions (newest first)."""
    conn, eng = _context_engine()
    sessions = eng.sessions()
    conn.close()

    if not sessions:
        print("No sessions found.\n")
        print("Run:\n")
        print("  friday context build\n")
        return 0

    for s in sessions:
        print(f"{s.start_time[:16]} | {s.duration_min:>5.0f}m | {s.activity.value:20s} | {s.primary_repo or 'multiple'}")

    print(f"\nTotal: {len(sessions)} sessions")
    return 0


def cmd_timeline(args: argparse.Namespace) -> int:
    """READ-ONLY: show the chronological engineering timeline."""
    conn, eng = _context_engine()
    timeline = eng.timeline()
    conn.close()

    if not timeline:
        print("No timeline entries.\n")
        print("Run:\n")
        print("  friday context build\n")
        return 0

    for entry in timeline:
        if entry.kind == "session":
            print(f"[{entry.start_time[:16]}] {entry.duration_min:>5.0f}m | {entry.label} | {entry.detail or ''}")
        else:
            print(f"[{entry.start_time[:16]}] {entry.duration_min:>5.0f}m | {entry.label}")

    return 0


def cmd_observer(args: argparse.Namespace) -> int:
    """Show one observer's health, summary, and live facts."""
    conn = connect()
    reg = default_registry()
    if args.name not in reg:
        print(f"error: no such observer: {args.name}", file=sys.stderr)
        print(f"available: {', '.join(reg.names())}", file=sys.stderr)
        conn.close()
        return 2
    obs = reg.get(args.name)
    h = obs.health(conn)
    print(f"Observer: {obs.name}")
    print(f"Health:   {h.status.value}" + (f" — {h.detail}" if h.detail else "") + (f"  [{h.method}]" if h.method else ""))
    if h.healthy:
        print(f"Summary:  {obs.summarize(conn)}")
        if not args.summary_only:
            from .observation import ObservationEngine, ObserverRegistry
            reg_single = ObserverRegistry()
            reg_single.register(obs)
            run = ObservationEngine(reg_single, conn).run()
            print("\n" + format_run(run), end="")
    conn.close()
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    """Audit every repository for exactly why its evidence is weak (Part D)."""
    from .evidence_scope import audit_evidence_completeness, format_completeness_audit

    conn = connect()
    rows = audit_evidence_completeness(conn)
    conn.close()
    print(format_completeness_audit(rows))
    weak = sum(1 for r in rows if not r["complete"])
    print(f"\n{weak} of {len(rows)} repositories have weak evidence.")
    return 0


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    parser = argparse.ArgumentParser(
        prog="friday",
        description="Friday V3 — workspace understanding operating partner.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="Scan directories and store knowledge.")
    p_ingest.add_argument("paths", nargs="+", help="One or more root folders.")
    p_ingest.set_defaults(func=cmd_ingest)

    p_summary = sub.add_parser("summary", help="Print the workspace knowledge summary.")
    p_summary.set_defaults(func=cmd_summary)

    p_ask = sub.add_parser("ask", help="Ask a question about your projects.")
    p_ask.add_argument("question", help="Natural-language question (quote it).")
    p_ask.add_argument(
        "--verbose",
        action="store_true",
        help="Show the retrieved evidence block behind the answer.",
    )
    p_ask.set_defaults(func=cmd_ask)

    p_chat = sub.add_parser(
        "chat", help="Conversational loop that remembers only the last exchange."
    )
    p_chat.add_argument(
        "--verbose",
        action="store_true",
        help="Show the retrieved evidence block behind each answer.",
    )
    p_chat.set_defaults(func=cmd_chat)

    p_analyze = sub.add_parser(
        "analyze", help="Extract and persist repository architecture knowledge."
    )
    p_analyze.add_argument("repository", help="Path to a git repository.")
    p_analyze.set_defaults(func=cmd_analyze)

    p_observe = sub.add_parser(
        "observe",
        help="Refresh the workspace knowledge stack from current repos.",
    )
    p_observe.add_argument(
        "repo", nargs="?", default=None,
        help="Refresh only this repository (path or name); omit for the whole workspace.",
    )
    p_observe.add_argument(
        "--changed", action="store_true",
        help="Refresh ONLY repositories whose observable state changed.",
    )
    p_observe.add_argument(
        "--summary", action="store_true",
        help="Print the summary report (repos scanned/changed, knowledge, "
             "understanding, identity, portfolio, insights, elapsed).",
    )
    p_observe.set_defaults(func=cmd_observe)

    p_audit = sub.add_parser(
        "audit", help="Show exactly why each repository contributes weak evidence."
    )
    p_audit.set_defaults(func=cmd_audit)

    p_observers = sub.add_parser(
        "observers", help="List all registered observers and their health."
    )
    p_observers.set_defaults(func=cmd_observers)

    p_context = sub.add_parser(
        "context", help="Show engineering context. Add 'build' to (re)build it."
    )
    p_context.add_argument(
        "action", nargs="?", default=None,
        choices=["build", "today"],
        help="'build' to derive+persist sessions (WRITE); 'today' for today only; omit to show current context.",
    )
    p_context.set_defaults(func=cmd_context)

    p_sessions = sub.add_parser(
        "sessions", help="List all engineering sessions (newest first)."
    )
    p_sessions.set_defaults(func=cmd_sessions)

    p_timeline = sub.add_parser(
        "timeline", help="Show the chronological engineering timeline."
    )
    p_timeline.set_defaults(func=cmd_timeline)

    p_observer = sub.add_parser(
        "observer", help="Show one observer's health, summary, and live facts."
    )
    p_observer.add_argument("name", help="Observer name (see `friday observers`).")
    p_observer.add_argument(
        "--summary-only", action="store_true",
        help="Print health + summary only; skip a fresh observation run.",
    )
    p_observer.set_defaults(func=cmd_observer)

    p_knowledge = sub.add_parser(
        "knowledge", help="Accumulated engineering knowledge (WRITE: 'build')."
    )
    p_knowledge.add_argument(
        "action", nargs="?", default=None,
        help="Action: 'build' (WRITE), 'list', 'explain', 'history', 'evolution', 'verify'; omit to list.",
    )
    p_knowledge.add_argument(
        "knowledge_id", nargs="?", default=None,
        help="Knowledge ID for 'explain' action (can also use --id)."
    )
    p_knowledge.add_argument(
        "--id", help="Knowledge ID for 'explain' action."
    )
    p_knowledge.add_argument(
        "--verbose", action="store_true",
        help="Show full evidence IDs when explaining."
    )
    p_knowledge.set_defaults(func=cmd_knowledge)

    p_understanding = sub.add_parser(
        "understanding", help="Derive and show engineering understanding (WRITE: 'build')."
    )
    p_understanding.add_argument(
        "action", nargs="?", default=None,
        choices=["build", "explain", "evolution", "list"],
        help="'build' (WRITE), 'explain <id>', 'evolution'; omit to list.",
    )
    p_understanding.add_argument(
        "understanding_id", nargs="?", default=None,
        help="Understanding ID for 'explain' (can also use --id)."
    )
    p_understanding.add_argument(
        "--id", help="Understanding ID for 'explain' action."
    )
    p_understanding.set_defaults(func=cmd_understanding)

    p_initiatives = sub.add_parser(
        "initiatives", help="Derive and show engineering initiatives (WRITE: 'build')."
    )
    p_initiatives.add_argument(
        "action", nargs="?", default=None,
        choices=["build", "explain", "timeline", "list"],
        help="'build' (WRITE), 'explain <id>', 'timeline'; omit to list.",
    )
    p_initiatives.add_argument(
        "initiative_id", nargs="?", default=None,
        help="Initiative ID for 'explain' (can also use --id)."
    )
    p_initiatives.add_argument(
        "--id", help="Initiative ID for 'explain' action."
    )
    p_initiatives.set_defaults(func=cmd_initiatives)

    p_insights = sub.add_parser(
        "insights", help="Derive and show engineering insights (WRITE: 'build')."
    )
    p_insights.add_argument(
        "action", nargs="?", default=None,
        choices=["build", "explain", "evolution", "list"],
        help="'build' (WRITE), 'explain <id>', 'evolution'; omit to list.",
    )
    p_insights.add_argument(
        "insight_id", nargs="?", default=None,
        help="Insight ID for 'explain' (can also use --id)."
    )
    p_insights.add_argument(
        "--id", help="Insight ID for 'explain' action."
    )
    p_insights.set_defaults(func=cmd_insights)

    p_profile = sub.add_parser(
        "profile", help="Operator identity: preferences, patterns, and derived traits."
    )
    p_profile.add_argument(
        "action", nargs="?", default="show",
        choices=["show", "set", "unset", "history", "derive", "stats"],
        help="'show' (default), 'set <key> <value>', 'unset <key>', 'history', "
             "'derive' (force re-derive), or 'stats'.",
    )
    p_profile.add_argument(
        "key", nargs="?", default=None,
        help="Preference key (for 'set' / 'unset').",
    )
    p_profile.add_argument(
        "value", nargs="?", default=None,
        help="Preference value (for 'set').",
    )
    p_profile.set_defaults(func=cmd_profile)

    p_portfolio = sub.add_parser(
        "portfolio", help="Workspace reasoning: themes, overlap, value, recommendations."
    )
    p_portfolio.add_argument(
        "token", nargs="?",
        choices=["themes", "overlap", "ranking", "recommendations", "integrations"],
        help="Aspect to view; omit for the workspace overview.",
    )
    p_portfolio.set_defaults(func=cmd_portfolio_dispatch)

    p_strategy = sub.add_parser(
        "strategy", help="Strategic judgment: impact, platform, learning, opportunity."
    )
    p_strategy.add_argument(
        "token", nargs="?",
        choices=["impact", "platform", "learning", "opportunity", "priority", "merge", "converge"],
        help="Judgment axis; omit for the converging thesis.",
    )
    p_strategy.set_defaults(func=cmd_strategy)

    p_plan = sub.add_parser(
        "plan", help="Generate / show an engineering plan (WRITE: '<goal>')."
    )
    p_plan.add_argument(
        "goal", nargs="?",
        help="Goal to plan for (e.g. \"Implement OAuth\"). Omit with an action to list.",
    )
    p_plan.add_argument(
        "action", nargs="?", default=None,
        help="'explain <id>', 'history', or 'list'; omit with a goal to generate.",
    )
    p_plan.add_argument(
        "plan_id", nargs="?", default=None,
        help="Plan ID for 'explain' (can also use --id)."
    )
    p_plan.add_argument("--id", help="Plan ID for 'explain' action.")
    p_plan.set_defaults(func=cmd_plan)

    p_plans = sub.add_parser(
        "plans", help="List derived engineering plans (alias of 'plan list')."
    )
    p_plans.set_defaults(func=cmd_plan, action="list", goal=[])

    p_graph = sub.add_parser(
        "graph", help="Compile a goal's Plan into a Task Graph (WRITE: '<goal>')."
    )
    p_graph.add_argument(
        "goal", nargs="?",
        help="Goal, or 'generate <initiative-id>', or 'review [approve|reject <id>]'.",
    )
    p_graph.add_argument(
        "action", nargs="?",
        help="'explain <id>', 'export <id>', 'list', 'generate <id>', or 'review [approve|reject <id>]'.",
    )
    p_graph.add_argument(
        "graph_id", nargs="*", default=None,
        help="Graph ID for 'explain'/'export'/'review' (can also use --id)."
    )
    p_graph.add_argument("--id", help="Graph ID for 'explain'/'export' action.")
    p_graph.set_defaults(func=cmd_graph)

    p_graphs = sub.add_parser(
        "graphs", help="List compiled task graphs (alias of 'graph list')."
    )
    p_graphs.set_defaults(func=cmd_graph, action="list", goal=[])

    p_workers = sub.add_parser(
        "workers", help="List all registered workers (capability profiles)."
    )
    p_workers.set_defaults(func=cmd_worker, action=None, name=None)

    p_worker = sub.add_parser(
        "worker", help="Show / register / export workers (catalog only)."
    )
    p_worker.add_argument(
        "token", nargs="?", default=None,
        help="Worker name to show, or 'register' / 'export' action.",
    )
    p_worker.add_argument(
        "sub", nargs="?", default=None,
        help="Sub-action for 'register': 'builtin' registers all built-in workers.",
    )
    p_worker.add_argument(
        "--file", default=None,
        help="Manifest JSON file for the 'register' action.",
    )
    p_worker.set_defaults(func=cmd_worker)

    p_resolve = sub.add_parser(
        "resolve", help='Resolve a goal into task->worker assignments.'
    )
    p_resolve.add_argument(
        "goal", nargs="*",
        help='Goal to resolve (e.g. "Implement OAuth").',
    )
    p_resolve.set_defaults(func=cmd_resolve)

    p_resolver = sub.add_parser(
        "resolver", help="List / explain / export resolver assignments."
    )
    p_resolver.add_argument(
        "token", nargs="?", default=None,
        help="'explain <id>' or 'export'; omit to list.",
    )
    p_resolver.add_argument(
        "assignment_id", nargs="?", default=None,
        help="Assignment ID for 'explain' (can also use --id).",
    )
    p_resolver.add_argument("--id", help="Assignment ID for 'explain' action.")
    p_resolver.set_defaults(func=cmd_resolver)

    p_schedule = sub.add_parser(
        "schedule", help='Schedule a goal into an execution ordering.'
    )
    p_schedule.add_argument(
        "goal", nargs="*",
        help='Goal to schedule (e.g. "Implement OAuth").',
    )
    p_schedule.set_defaults(func=cmd_schedule)

    p_scheduler = sub.add_parser(
        "scheduler", help="List / explain / export execution schedules."
    )
    p_scheduler.add_argument(
        "token", nargs="?", default=None,
        help="'explain <id>' or 'export'; omit to list.",
    )
    p_scheduler.add_argument(
        "schedule_id", nargs="?", default=None,
        help="Schedule/graph ID for 'explain' (can also use --id).",
    )
    p_scheduler.add_argument("--id", help="Schedule/graph ID for 'explain' action.")
    p_scheduler.set_defaults(func=cmd_scheduler)

    p_execute = sub.add_parser(
        "execute", help="Plan, resolve, schedule, and run a goal end-to-end.")
    p_execute.add_argument(
        "goal", nargs="+", help="The goal to execute, e.g. 'Improve the README'.")
    p_execute.add_argument(
        "--workspace", default=".", help="Working directory for execution.")
    p_execute.add_argument(
        "--yes", action="store_true",
        help="Skip confirmation prompt (for scripted/CI use).")
    p_execute.add_argument(
        "--dry-run", action="store_true",
        help="Print the execution plan without running it.")
    p_execute.set_defaults(func=cmd_execute)

    p_runtime = sub.add_parser(
        "runtime", help="Execute a goal (Plan->Graph->Resolve->Schedule->Run)."
    )
    p_runtime.add_argument(
        "goal", nargs="*",
        help='Goal to execute (e.g. "Implement OAuth").',
    )
    p_runtime.add_argument(
        "--yes", action="store_true",
        help="Skip confirmation prompt (for scripted/CI use).")
    p_runtime.add_argument(
        "--dry-run", action="store_true",
        help="Print the execution plan without running it.")
    p_runtime.set_defaults(func=cmd_runtime)

    p_runtime_session = sub.add_parser(
        "runtime_session", help="List execution sessions."
    )
    p_runtime_session.set_defaults(func=cmd_runtime_dispatch)

    p_runtime_show = sub.add_parser(
        "runtime_show", help="Show an execution session's timeline."
    )
    p_runtime_show.add_argument("session_id", nargs="?", default=None)
    p_runtime_show.set_defaults(func=cmd_runtime_show)

    p_runtime_export = sub.add_parser(
        "runtime_export", help="Export all execution sessions as JSON."
    )
    p_runtime_export.set_defaults(func=cmd_runtime_export)

    p_review = sub.add_parser(
        "review",
        help="Review engineering work: workspace, project, plan, graph, runtime, portfolio.",
    )
    p_review.add_argument(
        "token", nargs="?",
        help="Subcommand: 'plan', 'graph', 'runtime', 'portfolio'; or a project name; omit for the workspace review.",
    )
    p_review.add_argument(
        "rest", nargs="*",
        help="Argument for the subcommand: goal for 'plan', id for 'graph'/'runtime', project name for bare review.",
    )
    p_review.add_argument("--id", help="Graph or session ID (alternative to position).")
    p_review.set_defaults(func=cmd_review)

    p_capability = sub.add_parser(
        "capability", help="Capability discovery, registry, health, benchmark, genesis.")
    p_capability.add_argument(
        "token", nargs="?", default="list",
        choices=["discover", "list", "info", "benchmark", "propose", "review"])
    p_capability.add_argument(
        "--worker", help="Worker name for 'info'.")
    p_capability.add_argument(
        "--capability", help="Capability name for 'propose' (explicit gap).")
    p_capability.add_argument(
        "--goal", help="Goal for 'propose' (context for explicit gap).")
    p_capability.add_argument(
        "action", nargs="?", default=None,
        help="For 'review': 'approve <id>' or 'reject <id>', or just an ID to inspect.")
    p_capability.add_argument(
        "target", nargs="?", default=None,
        help="Target proposal ID for 'review' action.")
    p_capability.set_defaults(func=cmd_capability)

    p_watch = sub.add_parser(
        "watch", help="Ambient workspace observation loop (systemd timer).")
    p_watch.add_argument(
        "--install", action="store_true",
        help="Install the systemd timer and start it.")
    p_watch.add_argument(
        "--uninstall", action="store_true",
        help="Stop and remove the systemd timer.")
    p_watch.add_argument(
        "--status", action="store_true",
        help="Show timer and recent watch cycle status.")
    p_watch.add_argument(
        "--run-once", action="store_true",
        help="Run one watch cycle immediately (also used by the timer).")
    p_watch.add_argument(
        "--quiet", action="store_true",
        help="Suppress output when --run-once.")
    p_watch.set_defaults(func=cmd_watch)

    p_suggest = sub.add_parser(
        "suggest",
        help="Surface cross-project integration opportunities from existing workspace evidence.")
    p_suggest.add_argument(
        "--graph", default=None,
        help="Generate a Task Graph from a suggestion by its id (run `friday suggest` to see ids).")
    p_suggest.set_defaults(func=cmd_suggest)

    p_integrate = sub.add_parser(
        "integrate",
        help="Analyse 2+ repositories for integration opportunities and generate a Task Graph.")
    p_integrate.add_argument(
        "repos", nargs="+",
        help="Two or more repository names (e.g. vivaha aether).")
    p_integrate.set_defaults(func=cmd_integrate)

    p_repair = sub.add_parser(
        "repair",
        help="Detect and propose repairs for failed task executions (Law 16).")
    p_repair.add_argument(
        "action", nargs="?", default="pending",
        choices=["pending", "approve", "reject"],
        help="'pending' (default), 'approve <id>', or 'reject <id>'.",
    )
    p_repair.add_argument(
        "rest", nargs="*",
        help="Proposal ID for approve/reject/pending detail.",
    )
    p_repair.set_defaults(func=cmd_repair)

    p_daemon = sub.add_parser(
        "daemon",
        help="Ambient observation daemon (always-on mode).")
    p_daemon.add_argument(
        "action", nargs="?", default="status",
        choices=["start", "stop", "restart", "status", "logs"],
        help="'start', 'stop', 'restart', 'status' (default), or 'logs'.")
    p_daemon.add_argument(
        "--interval", type=int, default=900,
        help="Seconds between observation cycles (default: 900 = 15 min).")
    p_daemon.add_argument(
        "--no-notify", action="store_true",
        help="Suppress desktop notifications.")
    p_daemon.add_argument(
        "--lines", type=int, default=50,
        help="Number of log lines to show with 'logs' action (default: 50).")
    p_daemon.set_defaults(func=cmd_daemon)

    p_patterns = sub.add_parser(
        "patterns",
        help="Mine and show repeated action patterns from the actions log (Pillar B Stage 2).")
    p_patterns.add_argument(
        "action", nargs="?", default=None,
        choices=["mine", "clear", "label", "form"],
        help="'mine' to run sequence mining, 'label' to run LLM intent labeling, "
             "'form' to create skills from intents, "
             "'clear' to delete all patterns, omit to show.")
    p_patterns.add_argument(
        "--force", action="store_true",
        help="Force re-formation even if skill already exists (form action).")
    p_patterns.add_argument(
        "--min-count", type=int, default=0,
        help="Minimum pattern count to show (default: 0 = all).")
    p_patterns.add_argument(
        "--limit", type=int, default=50,
        help="Max patterns to show (default: 50).")
    p_patterns.set_defaults(func=cmd_patterns)

    p_actions = sub.add_parser(
        "actions",
        help="Show recent action events logged by Friday (Pillar B Stage 1).")
    p_actions.add_argument(
        "n", nargs="?", type=int, default=50,
        help="Number of recent actions to show (default: 50).")
    p_actions.add_argument(
        "--source", type=str, default=None,
        help="Filter by source (friday, hyprland, browser, etc.).")
    p_actions.set_defaults(func=cmd_actions)

    p_skills = sub.add_parser(
        "skills",
        help="List or invoke formed skills (Pillar B Stage 4).")
    p_skills.add_argument(
        "action", nargs="?", default="list",
        choices=["list", "run", "drift"],
        help="'list' (default), 'run <name>' to invoke, 'drift' to analyze degradation.")
    p_skills.add_argument(
        "name", nargs="?", default=None,
        help="Worker name for the 'run' action.")
    p_skills.add_argument(
        "--on-failure", type=str, default=None,
        choices=["abort", "skip", "retry_alt"],
        help="Step failure strategy: abort (stop on first failure), "
             "skip (log and continue), retry_alt (try next exemplar). "
             "Default: abort (auto-downgrades to skip after 3+ failures).")
    p_skills.set_defaults(func=cmd_skills)

    p_autonomy = sub.add_parser(
        "autonomy",
        help="Graduated autonomy controls: kill switch, per-action permissions, confidence escalation (Gap #7).")
    p_autonomy.add_argument(
        "subcommand", nargs="?", default="status",
        choices=["status", "enable", "disable", "kill", "resume", "set", "reset"],
        help="'status' (default), 'enable', 'disable', 'kill' (emergency stop), "
             "'resume' (release kill switch), 'set <action> <level>', "
             "or 'reset <action>'.")
    p_autonomy.add_argument(
        "action_type", nargs="?", default=None,
        help="Action type for 'set' or 'reset' (e.g. 'workspace', 'exec').")
    p_autonomy.add_argument(
        "level", nargs="?", default=None,
        help="Permission level for 'set': auto, confirm, double.")
    p_autonomy.set_defaults(func=cmd_autonomy)

    # --- Email communication layer ---
    p_email = sub.add_parser(
        "email",
        help="Email communication: config, inbox, send (requires IMAP/SMTP credentials).")
    p_email.add_argument(
        "action", nargs="?", default=None,
        choices=["config", "inbox", "send", "setup"],
        help="'config' to show configuration, 'inbox' to list recent emails, "
             "'send <to> <subject>' to send an email, 'setup' for instructions.")
    p_email.add_argument(
        "to", nargs="?", default=None,
        help="Recipient email address for 'send'.")
    p_email.add_argument(
        "subject", nargs="?", default=None,
        help="Email subject for 'send'.")
    p_email.add_argument(
        "--limit", type=int, default=20,
        help="Max emails to show for 'inbox' (default: 20).")
    p_email.set_defaults(func=cmd_email)

    # --- Slack communication layer ---
    p_slack = sub.add_parser(
        "slack",
        help="Slack communication: config, channels, send (requires bot token).")
    p_slack.add_argument(
        "action", nargs="?", default=None,
        choices=["config", "channels", "send", "setup"],
        help="'config' to show configuration, 'channels' to list, "
             "'send <channel> <text>' to post, 'setup' for instructions.")
    p_slack.add_argument(
        "channel", nargs="?", default=None,
        help="Channel name/ID for 'send'.")
    p_slack.add_argument(
        "text", nargs="*", default=None,
        help="Message text for 'send'.")
    p_slack.add_argument(
        "--limit", type=int, default=20,
        help="Max channels to show (default: 20).")
    p_slack.set_defaults(func=cmd_slack)

    # --- Discord communication layer ---
    p_discord = sub.add_parser(
        "discord",
        help="Discord communication: config, guilds, channels, send (requires bot token).")
    p_discord.add_argument(
        "action", nargs="?", default=None,
        choices=["config", "guilds", "channels", "send", "setup"],
        help="'config', 'guilds', 'channels <guild_id>', "
             "'send <channel> <text>', or 'setup'.")
    p_discord.add_argument(
        "guild_id", nargs="?", default=None,
        help="Guild ID for 'channels' action.")
    p_discord.add_argument(
        "channel", nargs="?", default=None,
        help="Channel ID for 'send' action.")
    p_discord.add_argument(
        "content", nargs="*", default=None,
        help="Message content for 'send' action.")
    p_discord.set_defaults(func=cmd_discord)

    # --- Telegram communication layer ---
    p_telegram = sub.add_parser(
        "telegram",
        help="Telegram communication: config, me, send (requires bot token from @BotFather).")
    p_telegram.add_argument(
        "action", nargs="?", default=None,
        choices=["config", "me", "send", "setup"],
        help="'config', 'me' (bot info), 'send <chat_id> <text>', or 'setup'.")
    p_telegram.add_argument(
        "chat_id", nargs="?", default=None,
        help="Chat ID for 'send' action.")
    p_telegram.add_argument(
        "text", nargs="*", default=None,
        help="Message text for 'send' action.")
    p_telegram.set_defaults(func=cmd_telegram)

    # --- Friday Identity (persistent persona) ---
    p_identity = sub.add_parser(
        "identity",
        help="Friday Identity: persistent persona you can chat with through any channel.")
    p_identity.add_argument(
        "action", nargs="?", default=None,
        choices=["chat", "telegram"],
        help="'chat' for interactive CLI session, 'telegram start|stop' to poll Telegram, "
             "omit to show identity status.")
    p_identity.add_argument(
        "sub", nargs="?", default=None,
        help="'start' or 'stop' for 'telegram' action.")
    p_identity.set_defaults(func=cmd_identity)

    p_project = sub.add_parser(
        "project",
        help="Persistent project sessions: start, chat, template, edit, add, suggest."
    )
    p_project.add_argument(
        "action", nargs="?", default="status",
        choices=["start", "status", "continue", "end", "chat",
                 "add", "edit", "suggest",
                 "template", "scaffold", "list-templates"],
        help="'start <path>' to begin, 'status' to view, 'chat' for interactive mode, "
             "'template <name>' to scaffold from a preset, "
             "'list-templates' to see available presets, "
             "'end' to finish, 'add <file> <desc>' to create, "
             "'edit <file> <desc>' to modify, or 'suggest' for enhancements.",
    )
    p_project.add_argument(
        "target", nargs="?", default=None,
        help="For 'start': project path. For 'template': template name. For 'add'/'edit': file name.",
    )
    p_project.add_argument(
        "description", nargs="?", default=None,
        help="For 'start'/'template': output path. For 'add'/'edit': description.",
    )
    p_project.add_argument(
        "--type", dest="project_type", default=None,
        help="Project type hint for 'start' (e.g. flask, react, python, cli).",
    )
    p_project.set_defaults(func=cmd_project)

    p_do = sub.add_parser(
        "do",
        help="Natural language command — say what you want and Friday figures out the rest."
    )
    p_do.add_argument(
        "text", nargs=argparse.REMAINDER,
        help="What you want Friday to do — in plain English.",
    )
    p_do.set_defaults(func=cmd_do)

    p_doctor = sub.add_parser(
        "doctor", help="Check system health (DB, deps, workers, README, watch)."
    )
    p_doctor.set_defaults(func=cmd_doctor)

    from .cli_synthesize import add_subparser as add_synthesize
    add_synthesize(sub)

    from .cli_meta import add_subparser as add_meta
    add_meta(sub)

    p_correlate = sub.add_parser(
        "correlate",
        help="Cross-project correlation: find structural and semantic similarities between repos.")
    p_correlate.add_argument(
        "--detail", nargs=2, metavar=("REPO_A", "REPO_B"), default=None,
        help="Show detailed correlation between two specific repos.")
    p_correlate.add_argument(
        "--scan-docs", action="store_true",
        help="Scan repos for project docs (PRDs, design docs) without running correlation.")
    p_correlate.set_defaults(func=cmd_correlate)

    # If the first argument isn't a known subcommand, route through
    # the NL dispatcher so `friday <anything>` works naturally.
    # ``argv`` may be None (called from the installed script or ``__main__``),
    # so resolve it to ``sys.argv[1:]`` for the fallback check.
    _argv = argv if argv is not None else sys.argv[1:] if len(sys.argv) > 1 else []
    if _argv and _argv[0] not in sub.choices and not _argv[0].startswith("-"):
        return cmd_do(argparse.Namespace(text=_argv))

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
