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
from .cli_dashboard import cmd_dashboard
from .cli_patterns import cmd_patterns
from .cli_actions import cmd_actions
from .cli_skills import cmd_skills
from .cli_autonomy import cmd_autonomy
from .cli_status import cmd_status, cmd_focus
from .cli_protocol import cmd_protocol
from .cli_wait import cmd_wait
from .cli_agent import cmd_agent
from .cli_presentation import cmd_hud, cmd_viz, cmd_web, cmd_report
from .autonomous_planner import (
    get_pending_plans,
    get_plan_history,
    ActionPlan,
    dispatch_plan,
)
from .cli_email import cmd_email
from .cli_slack import cmd_slack
from .cli_discord import cmd_discord
from .cli_telegram import cmd_telegram
from .cli_calendar import cmd_calendar
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


def cmd_feed(args: argparse.Namespace) -> int:
    """Show the ambient event feed — what Friday has noticed.

    Displays recent events from the ambient feed in reverse chronological order.
    Events are color-coded by priority and category.
    """
    from .presentation.cli_format import header, green, yellow, red, gray, blue, cyan

    conn = connect()
    try:
        from .ambient import get_feed, get_unread_count, dismiss_event, dismiss_all, summarize_recent

        if args.action == "dismiss-all":
            n = dismiss_all(conn)
            print(f"Dismissed {n} event(s).")
            return 0

        if args.action == "dismiss":
            dismiss_event(conn, args.id)
            print(f"Dismissed event #{args.id}.")
            return 0

        if args.action == "prune":
            from .ambient import prune_feed
            n = prune_feed(conn)
            print(f"Pruned {n} old event(s) from the feed.")
            return 0

        if args.action == "summary":
            summary = summarize_recent(conn, hours=args.hours)
            print(header("Feed Summary", f"{summary['total_events']} events, {summary['unread']} unread"))
            print()
            print(f"  Recent events:   {summary['total_events']}")
            print(f"  High priority:   {summary['high_priority']}")
            print(f"  Unread:          {summary['unread']}")
            print()
            for cat, cnt in sorted(summary['by_category'].items()):
                print(f"    {cat}: {cnt}")
            if summary['latest_event']:
                print()
                print(gray(f"  Latest: {summary['latest_event']['title']}"))
            return 0

        # Default: show feed
        include_dismissed = args.all
        events = get_feed(
            conn,
            limit=args.limit,
            category=args.category,
            min_priority=args.min_priority,
            include_dismissed=include_dismissed,
        )
        unread = get_unread_count(conn)

        count_str = f"{len(events)} events"
        if unread:
            count_str += f", {unread} unread"
        if args.category:
            count_str += f", category={args.category}"
        if args.min_priority:
            count_str += f", min-priority={args.min_priority}"

        print(header("Ambient Feed", count_str))
        print()

        if not events:
            print(gray("  No events yet. Run `friday daemon start` to begin observing."))
            print(gray("  Events appear after each daemon cycle completes."))
            return 0

        for ev in events:
            # Color by priority
            pri_mark = "  "
            if ev.priority >= 3:
                pri_mark = red("●")
            elif ev.priority == 2:
                pri_mark = yellow("●")
            elif ev.priority == 1:
                pri_mark = blue("●")
            else:
                pri_mark = gray("○")

            # Category badge
            cat_colors = {
                "workspace": green,
                "intelligence": blue,
                "quality": yellow,
                "execution": cyan,
                "system": gray,
            }
            cat_fn = cat_colors.get(ev.category, gray)
            badge = cat_fn(f"[{ev.category[:5]}]")

            # Timestamp (just time portion)
            time_str = ev.timestamp[11:19] if len(ev.timestamp) >= 19 else ev.timestamp

            # Dismissed marker
            dismissed = gray(" [dismissed]") if ev.dismissed else ""

            print(f"  {pri_mark} {badge} {gray(time_str)} {ev.title}{dismissed}")
            if args.detail and ev.detail:
                print(f"        {ev.detail[:120]}")
            if ev.actionable:
                print(f"        {green('→')} {gray(ev.action_command)}")

        print()
        if unread:
            print(gray(f"  {unread} unread event(s). Use `friday feed --all` to see dismissed."))
        print(gray(f"  Commands: friday feed summary, friday feed dismiss-all"))

    finally:
        conn.close()
    return 0


def cmd_learn(args: argparse.Namespace) -> int:
    """Run conversation learning — extract operator identity and preferences
    from unprocessed conversation_log entries using the LLM.

    ``friday learn``               Process unprocessed entries
    ``friday learn --dry-run``      Show what would be extracted without saving
    ``friday learn --force``        Re-process already-processed entries too
    """
    from .presentation.cli_format import header, green, gray, yellow

    dry_run = getattr(args, "dry_run", False)
    force = getattr(args, "force", False)

    conn = connect()
    try:
        if force:
            # Reset processed flag for all entries so they get re-scanned.
            conn.execute("UPDATE conversation_log SET processed = 0")
            conn.commit()

        from .conversation_learner import process_conversations

        label = "dry-run" if dry_run else "learn"
        print(header("Conversation Learning", label))
        print()

        result = process_conversations(conn, dry_run=dry_run)

        scanned = result.get("scanned", 0)
        processed = result.get("processed", 0)
        extracted = result.get("extracted", {})
        persisted = result.get("persisted", [])

        if scanned == 0:
            print(gray("  No unprocessed conversation entries found."))
            print(gray("  Exchanges are logged when you chat via Telegram, Slack,"))
            print(gray("  Discord, or the CLI."))
            return 0

        print(f"  Scanned:    {scanned} exchange(s)")
        print(f"  Processed:  {processed} exchange(s)")

        if extracted:
            print()
            print(green("  Extracted:"))
            for key, value in extracted.items():
                label_key = key.lstrip("_")
                print(f"    {label_key}: {value}")

        if persisted:
            print()
            print(green(f"  Persisted: {', '.join(persisted)}"))

        if not extracted:
            print()
            print(yellow("  No operator information could be extracted."))
            print(yellow("  This is normal if the conversations don't contain"))
            print(yellow("  identity or preference statements."))

    finally:
        conn.close()
    return 0


def cmd_notif(args: argparse.Namespace) -> int:
    """Manage notification actions — next, list, clear.

    ``friday notif next``    Execute the last actionable notification's command
    """
    from .presentation.cli_format import header, green, gray

    action = getattr(args, "action", "status")

    if action == "next":
        from .notification import run_pending_action
        result = run_pending_action()
        print(result)
        return 0

    if action == "clear":
        from .notification import _clear_all_actions
        _clear_all_actions()
        print("✅ Pending notification actions cleared.")
        return 0

    print(f"Unknown notif action: {action}")
    print(gray("  Usage: friday notif next, friday notif clear"))
    return 1


def cmd_history(args: argparse.Namespace) -> int:
    """Show the conversation history — what Friday and the operator have said.

    ``friday history``                  Show last 20 exchanges
    ``friday history --channel telegram`` Only Telegram exchanges
    ``friday history --limit 100``       Show last 100 exchanges
    ``friday history --unprocessed``      Show only unprocessed exchanges
    """
    from .presentation.cli_format import header, green, gray, yellow, blue, cyan

    conn = connect()
    try:
        from .db import get_conversation_history

        limit = getattr(args, "limit", 20)
        channel = getattr(args, "channel", None)
        unprocessed = getattr(args, "unprocessed", False)

        events = get_conversation_history(
            conn,
            limit=limit,
            channel=channel,
            unprocessed_only=unprocessed,
        )

        count_str = f"{len(events)} exchanges"
        if channel:
            count_str += f", channel={channel}"
        if unprocessed:
            count_str += " (unprocessed only)"

        print(header("Conversation History", count_str))
        print()

        if not events:
            print(gray("  No conversations yet."))
            print(gray("  Use `friday ask 'hello'` or chat on Telegram/Slack/Discord."))
            return 0

        for ev in events:
            # Channel badge
            channel_colors = {
                "telegram": blue,
                "slack": green,
                "discord": cyan,
                "cli": gray,
            }
            ch_fn = channel_colors.get(ev.channel, gray)
            badge = ch_fn(f"[{ev.channel[:8]}]")

            # Timestamp (just time portion)
            time_str = ev.conversation_at[11:19] if len(ev.conversation_at) >= 19 else ev.conversation_at

            # Routing badge
            route_badge = ""
            if ev.routing:
                route_badge = gray(f" {ev.routing}")

            # Processed marker
            processed = "" if ev.processed else yellow(" [new]")

            print(f"  {badge} {gray(time_str)}{route_badge}{processed}")
            print(f"    You:     {ev.user_message[:120]}")
            print(f"    Friday:  {ev.friday_reply[:120]}")
            print()

        print(gray(f"  Total: {len(events)} exchange(s)"))

    finally:
        conn.close()
    return 0


def cmd_reason(args: argparse.Namespace) -> int:
    """Run the ensemble reasoner directly — fire 2-3 models in parallel for
    calibrated confidence on any question.

    ``friday reason "which project is most mature?"``
    ``friday reason "what should I work on next?" --verbose``
    """
    from .presentation.cli_format import header, green, yellow, red, gray, blue
    from .reasoning import EnsembleReasoner

    question = args.question
    verbose = getattr(args, "verbose", False)

    # Build a system prompt for the ensemble.
    # The ensemble fires 2-3 models in parallel and measures their agreement.
    system = (
        "You are Friday, an AI operating partner. Answer the question concisely "
        "and directly. Base your answer on general knowledge — no workspace "
        "evidence is provided. Be specific and actionable."
    )

    print(header("Ensemble Reasoner", f"\"{question[:60]}...\"" if len(question) > 60 else f"\"{question}\""))
    print()
    print(gray("  Consulting 2-3 models in parallel..."))
    print()

    er = EnsembleReasoner(timeout_per_model=30)
    result = er.reason(system, question)

    if not result.text:
        print(red("  No model responded. Check your API keys and model configuration."))
        print(gray("  Set GROQ_API_KEY, OPENROUTER_API_KEY, or GEMINI_API_KEY to enable models."))
        return 1

    # Print the answer.
    print(result.text)
    print()

    # Confidence badge.
    conf_pct = f"{result.confidence:.0%}"
    if result.agreement == "high":
        badge = green(f"● {conf_pct} confidence (high agreement)")
    elif result.agreement == "medium":
        badge = yellow(f"● {conf_pct} confidence (medium agreement)")
    else:
        badge = red(f"● {conf_pct} confidence (low agreement — models disagree)")

    label = er.confidence_label(result.confidence)
    print(f"  {badge}")
    print(f"  {gray(label)}")
    print()

    if verbose and result.response_count > 0:
        print(header("Details", f"{result.response_count} model(s) responded"))
        print(gray(f"  Agreement score: {result.agreement_score:.0%}"))
        print(gray(f"  Primary model: {result.primary_model}"))
        print()
        for name, text in result.all_responses.items():
            m = "◀ PRIMARY" if name == result.primary_model else ""
            print(header(name, m))
            print(text[:300])
            print()

    return 0


def cmd_act(args: argparse.Namespace) -> int:
    """Inspect and manage autonomous action plans.

    ``friday act``                     List pending plans
    ``friday act history``              Show completed/rejected plan history
    ``friday act approve <plan_id>``    Approve and execute a pending plan
    ``friday act reject <plan_id>``     Reject a pending plan
    ``friday act run``                  Execute all auto-approvable pending plans
    """
    from .presentation.cli_format import header, green, yellow, red, gray, blue

    action = getattr(args, "action", "list")
    conn = connect()
    try:
        if action == "history":
            plans = get_plan_history(conn, limit=30)
            if not plans:
                print(header("Autonomous Actions", "history empty"))
                print(gray("  No autonomous action history yet."))
                print(gray("  Plans are created automatically after each daemon cycle."))
                return 0
            print(header("Autonomous Action History", f"{len(plans)} total"))
            print()
            for p in plans:
                status_color = {
                    "pending": yellow,
                    "approved": blue,
                    "rejected": red,
                    "succeeded": green,
                    "failed": red,
                }.get(p.status, gray)
                status_mark = {
                    "pending": "○",
                    "approved": "◷",
                    "rejected": "✗",
                    "succeeded": "✓",
                    "failed": "✗",
                }.get(p.status, "?")
                print(f"  {status_color(status_mark)} {gray(p.created_at[:16])} "
                      f"{status_color(p.status.upper()):>10s} "
                      f"[{p.source}] {p.source_summary[:60]}")
            return 0

        if action == "approve":
            plan_id = getattr(args, "plan_id", "")
            if not plan_id:
                print("Specify a plan ID: friday act approve <plan_id>")
                return 1
            plans = get_pending_plans(conn)
            plan = next((p for p in plans if p.plan_id == plan_id), None)
            if not plan:
                print(f"No pending plan with ID '{plan_id}'.")
                return 1
            from .db import now_iso
            conn.execute(
                "UPDATE autonomous_actions SET status='approved', updated_at=? WHERE plan_id=?",
                (now_iso(), plan_id))
            conn.commit()
            print(green(f"Approved plan {plan_id[:16]}... Executing..."))
            result = dispatch_plan(plan, conn)
            if result:
                if result.get("success"):
                    print(green(f"  ✓ Succeeded ({result.get('duration_ms', 0)}ms)"))
                else:
                    print(red(f"  ✗ Failed: {result.get('error', 'unknown')}"))
            return 0

        if action == "reject":
            plan_id = getattr(args, "plan_id", "")
            if not plan_id:
                print("Specify a plan ID: friday act reject <plan_id>")
                return 1
            from .db import now_iso
            conn.execute(
                "UPDATE autonomous_actions SET status='rejected', updated_at=? WHERE plan_id=?",
                (now_iso(), plan_id))
            conn.commit()
            print(yellow(f"Rejected plan {plan_id[:16]}."))
            return 0

        if action == "run":
            plans = get_pending_plans(conn)
            auto_plans = [p for p in plans if not p.requires_confirm]
            if not auto_plans:
                print(gray("No auto-approvable pending plans."))
                print(gray("Use `friday act` to see all pending plans."))
                return 0
            print(header("Autonomous Dispatch", f"{len(auto_plans)} plan(s)"))
            for plan in auto_plans:
                print(f"  {gray(plan.plan_id[:12])} [{plan.source}] {plan.source_summary[:60]}")
                conn.execute(
                    "UPDATE autonomous_actions SET status='approved', updated_at=? WHERE plan_id=?",
                    (now_iso(), plan.plan_id))
                conn.commit()
                result = dispatch_plan(plan, conn)
                if result:
                    if result.get("success"):
                        print(f"    {green('✓')} Succeeded ({result.get('duration_ms', 0)}ms)")
                    else:
                        print(f"    {red('✗')} Failed: {result.get('error', 'unknown')}")
            return 0

        # Default: list pending plans
        source = getattr(args, "source", None)
        plans = get_pending_plans(conn, source=source)
        if not plans:
            print(header("Autonomous Actions", "none pending"))
            print(gray("  No pending autonomous actions."))
            print(gray("  Plans are created automatically after each daemon full cycle."))
            print(gray("  Run `friday daemon status` to check cycle status."))
            return 0

        # Group by source for readability.
        by_source: dict[str, list[ActionPlan]] = {}
        for p in plans:
            by_source.setdefault(p.source, []).append(p)

        total_count = len(plans)
        auto_count = sum(1 for p in plans if not p.requires_confirm)
        confirm_count = sum(1 for p in plans if p.requires_confirm)
        print(header("Autonomous Actions",
                      f"{total_count} pending ({auto_count} auto, {confirm_count} need confirm)"))
        print()
        for source_name, src_plans in by_source.items():
            print(f"  [{blue(source_name.upper())}]")
            for p in src_plans:
                auto_tag = gray(" [auto]") if not p.requires_confirm else yellow(" [needs confirm]")
                print(f"    {gray(p.plan_id[:12])} {p.source_summary[:65]}{auto_tag}")
                print(f"      → {p.action_type} ({p.auto_level}) — {p.motivation[:80]}")
            print()

        print(gray(f"  Commands:"))
        print(gray(f"    friday act approve <plan_id>   — Approve and execute"))
        print(gray(f"    friday act reject <plan_id>    — Reject"))
        print(gray(f"    friday act run                 — Execute all auto plans"))
        print(gray(f"    friday act history             — Show history"))

    finally:
        conn.close()
    return 0


def cmd_abort(args: argparse.Namespace) -> int:
    """Emergency kill switch — stop all execution immediately.

    ``friday abort``              Pull the kill switch (block all executors)
    ``friday abort --resume``      Release the kill switch
    ``friday abort --status``      Check whether kill switch is active

    This is the fastest way to stop Friday in an emergency. The kill switch
    sets a persistent flag checked before EVERY executor dispatch. Already-
    running processes complete or timeout — no new work starts.

    Equivalent to ``friday autonomy kill`` / ``friday autonomy resume``.
    """
    from .autonomy import is_kill_switch_active, set_kill_switch
    from .presentation.cli_format import red, green, gray, yellow

    conn = connect()
    try:
        if args.resume:
            set_kill_switch(False, conn)
            print(green("  🟢 Kill switch released. Normal operation resumed."))
            return 0

        if args.status:
            active = is_kill_switch_active(conn)
            if active:
                print(red("  🛑 KILL SWITCH IS ACTIVE."))
                print(gray("  All executor dispatch is blocked."))
                print(gray("  Run: friday abort --resume"))
            else:
                print(green("  ✅ Kill switch is NOT active."))
                print(gray("  Normal operation."))
            return 0

        # Default: activate kill switch.
        set_kill_switch(True, conn)
        print(red("  🛑 EMERGENCY KILL SWITCH ACTIVATED."))
        print(gray("  All executor dispatch is blocked."))
        print(gray("  Already-running processes will complete or time out."))
        print()
        print(gray("  To release:  friday abort --resume"))
        print(gray("  To check:    friday abort --status"))
        return 0
    finally:
        conn.close()


def cmd_working_context(args: argparse.Namespace) -> int:
    """Show Friday's working memory — what I'm doing right now.

    ``friday context-wm``               Show all current context
    ``friday context-wm clear``          Clear all working memory
    ``friday context-wm count``           Count active entries
    ``friday context-wm category status`` Show only status entries
    ``friday context-wm source system``   Show only system-originated entries
    """
    from .presentation.cli_format import header, green, yellow, red, gray, blue, cyan
    from .memory import WorkingMemory

    action = getattr(args, "action", "show")
    filter_val = getattr(args, "filter", None)

    conn = connect()
    try:
        wm = WorkingMemory(conn)

        if action == "clear":
            n = wm.clear_all()
            print(green(f"Cleared {n} working memory entries."))
            return 0

        if action == "count":
            n = wm.count()
            print(header("Working Memory", f"{n} active entries"))
            print()
            print(f"  Active (non-expired) entries: {n}")
            print(f"  Max entries before eviction: {wm.MAX_ENTRIES}")
            try:
                total_row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM working_memory").fetchone()
                total = total_row["cnt"] if total_row else 0
                if total > n:
                    print(f"  Total (including expired):     {total} (will be cleared next cycle)")
            except Exception:
                pass
            return 0

        if action == "category":
            if not filter_val:
                print("Specify a category: friday context-wm category <name>")
                return 1
            entries = wm.get_contexts_by_category(filter_val, limit=20)
            print(header("Working Memory", f"category={filter_val}, {len(entries)} entry/ies"))
            print()
            if not entries:
                print(gray(f"  No entries with category '{filter_val}'."))
                return 0
            for e in entries:
                pri_label = {0: "low", 1: "normal", 2: "medium",
                             3: "high", 4: "critical", 5: "blocking"}.get(
                    e["priority"], str(e["priority"]))
                expires = e["expires_at"][:19] if e["expires_at"] else "?"
                print(f"  {green(e['context_key']):25s} {e['value'][:60]}")
                print(f"  {'':25s} {gray(f'priority={pri_label}, expires={expires}')}")
                print()
            return 0

        if action == "source":
            if not filter_val:
                print("Specify a source: friday context-wm source <name>")
                return 1
            entries = wm.get_contexts_by_source(filter_val, limit=20)
            print(header("Working Memory", f"source={filter_val}, {len(entries)} entry/ies"))
            print()
            if not entries:
                print(gray(f"  No entries from source '{filter_val}'."))
                return 0
            for e in entries:
                cat_tag = cyan(f"[{e['category']}]")
                print(f"  {cat_tag} {green(e['context_key']):25s} {e['value'][:60]}")
                print()
            return 0

        # Default: show all context
        ctx = wm.get_current_context(limit=20)
        count = wm.count()
        print(header("Working Memory", f"{count} active entry/ies"))
        print()
        if not ctx:
            print(gray("  No active working memory entries."))
            print(gray("  Working memory tracks what Friday is doing right now —"))
            print(gray("  daemon status, active plans, current task state."))
            print(gray("  Entries appear after the daemon runs its first cycle."))
            return 0
        print(ctx)
        print()
        print(gray(f"  Max entries: {wm.MAX_ENTRIES} | "))
        print(gray(f"  Auto-eviction: lowest priority entries removed when over limit"))
        print(gray(f"  Expiry: entries auto-delete after their TTL"))
        print()
        print(gray("  Filter commands:"))
        print(gray("    friday context-wm category <name>"))
        print(gray("    friday context-wm source <name>"))

    finally:
        conn.close()
    return 0


def cmd_talk(args: argparse.Namespace) -> int:
    """Universal natural language entry point — just talk to Friday.

    Routes any text through IdentityEngine which handles questions,
    commands, chitchat, learning, and everything else. This is the same
    unified interface Telegram, Slack, and Discord use.

    Usage:
        friday talk "what's my name bud?"
        friday talk "deploy the fix"
        friday talk "hello"
        friday talk "remember my father's name is Raj"
        friday talk "what's happening in my projects?"

    Or just:
        friday "what's my name bud?"
        friday "deploy the fix"
    (Unknown commands auto-route here.)
    """
    text = " ".join(getattr(args, "text", getattr(args, "question", "")))
    text = text.strip()

    if not text:
        print("  Friday — Talk to me\n")
        print("  Just tell me what you want in plain English:")
        print()
        print("    friday talk 'what's my name bud?'")
        print("    friday talk 'deploy the fix'")
        print("    friday talk 'what's happening?'")
        print("    friday talk 'remember my father's name is Raj'")
        print()
        print("  Or use the short form (unknown commands auto-route):")
        print()
        print("    friday 'what's my name bud?'")
        print("    friday 'show me my projects'")
        print()
        return 0

    # ── "On it" pattern ─────────────────────────────────────────────
    # Print instant acknowledgment, then overwrite with the real response.
    # Uses carriage-return to overwrite the same line.
    import sys as _sys
    _sys.stdout.write("  On it...")
    _sys.stdout.flush()

    try:
        from .persona import IdentityEngine
        from .db import connect

        conn = connect()
        engine = IdentityEngine(conn=conn)
        reply = engine.process(text, channel_id="cli")
        conn.close()

        # Clear the "On it..." line and print the real response.
        _sys.stdout.write("\r" + " " * 20 + "\r")
        _sys.stdout.flush()

        if reply:
            print(reply)
            return 0

        # IdentityEngine returned nothing — try the ask pipeline directly.
        ns = argparse.Namespace(question=text, verbose=False)
        return cmd_ask(ns)

    except Exception as exc:
        _sys.stdout.write("\r" + " " * 20 + "\r")
        _sys.stdout.flush()
        print(f"Sorry, I hit an error: {exc}", file=sys.stderr)
        return 1


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
        choices=["show", "set", "unset", "history", "derive", "stats",
                 "depth", "relationship", "sentiment"],
        help="'show' (default), 'set <key> <value>', 'unset <key>', 'history', "
             "'derive' (force re-derive), 'stats', 'depth' (relationship depth), "
             "'relationship' (relationship graph), or 'sentiment' (sentiment trends).",
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
        "goal", nargs="*",
        help="The goal to execute, e.g. 'Improve the README'. "
             "Omit or use --pending to run all pending compiled graphs.")
    p_execute.add_argument(
        "--pending", action="store_true",
        help="Execute all pending compiled/approved graphs that haven't "
             "been scheduled yet (same logic as daemon _stage_execution_pipeline).")
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

    p_agent = sub.add_parser(
        "agent",
        help="Agent management: status, history, cancel agent sessions."
    )
    p_agent.add_argument(
        "subcommand", nargs="?", default="status",
        choices=["status", "history", "cancel"],
        help="'status' to show current session, 'history' for past runs, 'cancel' to stop."
    )
    p_agent.set_defaults(func=cmd_agent)

    # ── Presentation & Interface: hud ──────────────────────────────────
    p_hud = sub.add_parser(
        "hud",
        help="Heads-up display: persistent terminal status bar and popup notifications.")
    p_hud.add_argument(
        "action", nargs="?", default="status",
        choices=["on", "off", "compact", "full", "status"],
        help="'on'/'full' to enable, 'compact' for minimal, 'off' to disable, or omit for status.")
    p_hud.set_defaults(func=cmd_hud)

    # ── Presentation & Interface: viz ──────────────────────────────────
    p_viz = sub.add_parser(
        "viz",
        help="Visualizations: architecture, dependency graph, timeline, impact tree.")
    p_viz.add_argument(
        "kind", nargs="?", default="arch",
        choices=["arch", "deps", "timeline", "impact"],
        help="'arch' for architecture tree, 'deps' for dependencies, 'timeline' for activity, 'impact' for impact analysis.")
    p_viz.add_argument(
        "target", nargs="?", default=None,
        help="Target path (for arch) or symbol (for impact).")
    p_viz.add_argument(
        "--format", default="tree", choices=["tree", "mermaid", "image"],
        help="Output format: tree (ASCII), mermaid (Mermaid.js), image (SVG via graphviz).")
    p_viz.add_argument(
        "--output", default=None,
        help="Save output to file (required for image format).")
    p_viz.set_defaults(func=cmd_viz)

    # ── Presentation & Interface: web ──────────────────────────────────
    p_web = sub.add_parser(
        "web",
        help="Start the Friday dashboard web server.")
    p_web.add_argument(
        "--port", type=int, default=8321,
        help="Port to listen on (default: 8321).")
    p_web.add_argument(
        "--open", dest="open_browser", action="store_true",
        help="Open browser automatically.")
    p_web.set_defaults(func=cmd_web)

    # ── Presentation & Interface: report ───────────────────────────────
    p_report = sub.add_parser(
        "report",
        help="Generate rich reports: daily, weekly, impact analysis.")
    p_report.add_argument(
        "kind", nargs="?", default="daily",
        choices=["daily", "weekly", "impact"],
        help="'daily' for today's report, 'weekly' for weekly summary, 'impact' for impact analysis.")
    p_report.add_argument(
        "target", nargs="?", default=None,
        help="Symbol name (required for impact report).")
    p_report.add_argument(
        "--format", default="markdown", choices=["markdown", "html"],
        help="Output format: markdown or HTML.")
    p_report.add_argument(
        "--output", default=None,
        help="Save report to file.")
    p_report.set_defaults(func=cmd_report)

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
        "--lines", type=int, default=50,            help="Number of log lines to show with 'logs' action (default: 50).")
    p_daemon.set_defaults(func=cmd_daemon)

    p_notif = sub.add_parser(
        "notif",
        help="Manage notification actions — run the last actionable notification.")
    p_notif.add_argument(
        "action", nargs="?", default="status",
        choices=["next", "clear"],
        help="'next' to execute the last actionable notification's command, "
             "'clear' to dismiss all pending actions.")
    p_notif.set_defaults(func=cmd_notif)

    p_learn = sub.add_parser(
        "learn",
        help="Extract operator identity and preferences from conversation history.")
    p_learn.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be extracted without persisting.")
    p_learn.add_argument(
        "--force", action="store_true",
        help="Re-process already-processed entries.")
    p_learn.set_defaults(func=cmd_learn)

    p_history = sub.add_parser(
        "history",
        help="Show the conversation history — exchanges across all channels.")
    p_history.add_argument(
        "--limit", type=int, default=20,
        help="Number of recent exchanges to show (default: 20).")
    p_history.add_argument(
        "--channel", default=None,
        choices=["cli", "telegram", "slack", "discord"],
        help="Filter by channel.")
    p_history.add_argument(
        "--unprocessed", action="store_true",
        help="Show only exchanges not yet processed by LLM extraction.")
    p_history.set_defaults(func=cmd_history)

    p_dashboard = sub.add_parser(
        "dashboard",
        help="Unified command center — tabbed dashboard + inline command bar.")
    p_dashboard.add_argument(
        "--refresh", type=float, default=3.0,
        help="Seconds between auto-refresh (default: 3.0).")
    p_dashboard.add_argument(
        "--legacy", action="store_true",
        help="Launch the original ambient feed dashboard instead.")
    p_dashboard.set_defaults(func=cmd_dashboard)

    # ── Presence & Attention: status + focus ────────────────────────
    p_status = sub.add_parser(
        "status",
        help="Show current presence state and attention level — at desk, in meeting, deep focus, etc.")
    p_status.set_defaults(func=cmd_status)

    p_focus = sub.add_parser(
        "focus",
        help="Manage focus mode — auto DND for deep work. Use 'on <minutes>' or 'off'.")
    p_focus.add_argument(
        "action", nargs="?", default=None,
        help="'on' to enable (optionally with --minutes), 'off' to disable, or omit to check status.")
    p_focus.add_argument(
        "-m", "--minutes", type=int, default=90,
        help="Duration of focus mode in minutes (default: 90).")
    p_focus.set_defaults(func=cmd_focus)

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

    # Named protocols — user-defined multi-step macro procedures.
    from .cli_protocol import add_subparser as add_protocol_subparser
    add_protocol_subparser(sub)

    # Change impact analysis — what breaks if I modify this file?
    from .cli_impact import add_subparser as add_impact_subparser
    add_impact_subparser(sub)

    # Semantic code search — search codebase by meaning.
    from .cli_search import add_subparser as add_search_subparser
    add_search_subparser(sub)

    # Live Telemetry — real-time system metrics + processes + build.
    from .cli_telemetry import add_subparser as add_telemetry_subparser
    add_telemetry_subparser(sub)

    # Screen/Workspace Awareness — what's on your screen.
    from .cli_screen import add_subparser as add_screen_subparser
    add_screen_subparser(sub)

    # Persistent Missions — multi-cycle adaptive goals.
    from .cli_mission import add_subparser as add_mission_subparser
    add_mission_subparser(sub)

    # What-If Sandbox — simulate actions without side effects.
    from .cli_sandbox import add_subparser as add_sandbox_subparser
    add_sandbox_subparser(sub)

    # Undo / Rollback — undo the last mutating action.
    from .cli_undo import add_subparser as add_undo_subparser
    add_undo_subparser(sub)

    # Daily briefing — morning summary of workspace activity.
    from .cli_briefing import add_subparser as add_briefing_subparser
    add_briefing_subparser(sub)

    # Daily standup report + yesterday summary.
    from .cli_standup import add_standup_subparser, add_yesterday_subparser
    add_standup_subparser(sub)
    add_yesterday_subparser(sub)

    # Codebase narrative — git archaeology for project evolution.
    from .cli_narrative import add_subparser as add_narrative_subparser
    add_narrative_subparser(sub)

    # Persistent watchers — monitor conditions and notify when met.
    from .cli_wait import add_subparser as add_wait_subparser
    add_wait_subparser(sub)

    # Guided walkthroughs — step-by-step procedures.
    from .cli_guide import add_subparser as add_guide_subparser
    add_guide_subparser(sub)

    # Translation — translate text between languages + detect.
    from .cli_translate import add_subparser as add_translate_subparser
    add_translate_subparser(sub)

    # PR review assistant — analyze pull requests.
    from .cli_pr import add_subparser as add_pr_subparser
    add_pr_subparser(sub)

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

    # ── `friday abort` — emergency kill switch (discoverable shortcut) ──
    p_abort = sub.add_parser(
        "abort",
        help="🛑 EMERGENCY STOP: pull the kill switch and stop all execution.")
    p_abort.add_argument(
        "--resume", "-r", action="store_true",
        help="Release the kill switch and resume normal operation.")
    p_abort.add_argument(
        "--status", "-s", action="store_true",
        help="Check whether the kill switch is active.")
    p_abort.set_defaults(func=cmd_abort)

    p_act = sub.add_parser(
        "act",
        help="Autonomous actions — inspect, approve, or reject pending plans.")
    p_act.add_argument(
        "action", nargs="?", default="list",
        choices=["list", "history", "approve", "reject", "run"],
        help="'list' (default), 'history', 'approve <plan_id>', "
             "'reject <plan_id>', or 'run' (execute auto-approvable plans).")
    p_act.add_argument(
        "plan_id", nargs="?", default=None,
        help="Plan ID for 'approve' or 'reject'.")
    p_act.set_defaults(func=cmd_act)

    p_ctx_wm = sub.add_parser(
        "context-wm",
        help="Show Friday's working memory — what I'm doing right now.")
    p_ctx_wm.add_argument(
        "action", nargs="?", default="show",
        choices=["show", "clear", "count", "category", "source"],
        help="'show' (default), 'clear', 'count', 'category <name>', 'source <name>'.")
    p_ctx_wm.add_argument(
        "filter", nargs="?", default=None,
        help="Category or source name to filter by.")
    p_ctx_wm.set_defaults(func=cmd_working_context)

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

    # --- Calendar configuration ---
    p_calendar = sub.add_parser(
        "calendar",
        help="Calendar integration: configure Google Calendar OAuth, check status.")
    p_calendar.add_argument(
        "action", nargs="?", default="status",
        choices=["auth", "status"],
        help="'auth' for interactive OAuth setup, 'status' to show current provider.")
    p_calendar.set_defaults(func=cmd_calendar)

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

    p_reason = sub.add_parser(
        "reason",
        help="Ensemble-based reasoning — 3 models in parallel, agreement as confidence."
    )
    p_reason.add_argument(
        "question", nargs=argparse.REMAINDER,
        help="The question to reason about.",
    )
    p_reason.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show each model's response and detailed agreement metrics."
    )
    p_reason.set_defaults(func=cmd_reason)

    p_talk = sub.add_parser(
        "talk",
        help="Talk to Friday freely — questions, commands, chitchat, anything. The universal interface."
    )
    p_talk.add_argument(
        "text", nargs=argparse.REMAINDER,
        help="Anything you want to say to Friday — in plain English.",
    )
    p_talk.set_defaults(func=cmd_talk)

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

    p_feed = sub.add_parser(
        "feed",
        help="Ambient event feed — what Friday has noticed recently.")
    p_feed.add_argument(
        "action", nargs="?", default="list",
        choices=["list", "summary", "dismiss", "dismiss-all", "prune"],
        help="'list' (default), 'summary', 'dismiss <id>', 'dismiss-all', or 'prune'.")
    p_feed.add_argument(
        "id", nargs="?", type=int, default=None,
        help="Event ID for 'dismiss' action.")
    p_feed.add_argument(
        "--limit", type=int, default=50,
        help="Max events to show (default: 50).")
    p_feed.add_argument(
        "--category", type=str, default=None,
        help="Filter by category: workspace, intelligence, quality, execution, system.")
    p_feed.add_argument(
        "--min-priority", type=int, default=0,
        help="Minimum priority level: 0=all, 1=noteworthy, 2=important, 3=critical.")
    p_feed.add_argument(
        "--all", action="store_true",
        help="Include dismissed events.")
    p_feed.add_argument(
        "--detail", action="store_true",
        help="Show full detail for each event.")
    p_feed.add_argument(
        "--hours", type=int, default=24,
        help="Hours of history for summary (default: 24).")
    p_feed.set_defaults(func=cmd_feed)

    # If the first argument isn't a known subcommand, route through
    # the NL dispatcher so `friday <anything>` works naturally.
    # ``argv`` may be None (called from the installed script or ``__main__``),
    # so resolve it to ``sys.argv[1:]`` for the fallback check.
    _argv = argv if argv is not None else sys.argv[1:] if len(sys.argv) > 1 else []
    if _argv and _argv[0] not in sub.choices and not _argv[0].startswith("-"):
        return cmd_talk(argparse.Namespace(text=_argv))

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
