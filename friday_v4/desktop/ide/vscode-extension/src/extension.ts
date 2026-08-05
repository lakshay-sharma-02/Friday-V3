/**
 * Friday V4 — VS Code extension.
 *
 * Friday lives inside your editor: ask Friday anything, diagnose the
 * current file, open it in the editor, and browse the apps Friday has
 * learned. Every command shells out to the `friday4` CLI (the
 * documented, gated surface) — the extension adds no server, no new
 * attack surface, and works whether or not the Friday daemon is up.
 *
 * Safety: Friday's own permission gate decides what runs. The
 * extension never forces; an action that needs confirmation is reported
 * honestly with the terminal command to confirm it.
 */

import * as vscode from "vscode";
import { execFile } from "child_process";

// ---------------------------------------------------------------------------
// CLI plumbing
// ---------------------------------------------------------------------------

function binaryPath(): string {
  const cfg = vscode.workspace.getConfiguration("friday");
  return cfg.get<string>("binaryPath", "friday4");
}

function workspaceDir(): string {
  const cfg = vscode.workspace.getConfiguration("friday");
  const explicit = cfg.get<string>("workspacePath", "");
  if (explicit) {
    return explicit;
  }
  return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || process.cwd();
}

/** Run the friday4 CLI; resolves with combined stdout+stderr. */
function runFriday(args: string[], cwd?: string): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile(
      binaryPath(),
      args,
      { cwd: cwd || workspaceDir(), timeout: 60_000, maxBuffer: 4 * 1024 * 1024 },
      (error, stdout, stderr) => {
        const out = `${stdout || ""}${stderr || ""}`.trim();
        if (error && !out) {
          reject(new Error(`friday4 failed (${error.message})`));
          return;
        }
        resolve(out);
      }
    );
  });
}

interface TalkJson {
  intent?: string;
  action?: string;
  response?: string;
  status?: string | null;
}

/** `friday4 talk "<phrase>" --json` → parsed result (never throws). */
async function talkJson(phrase: string): Promise<TalkJson> {
  try {
    const raw = await runFriday(["talk", phrase, "--json"]);
    return JSON.parse(raw) as TalkJson;
  } catch {
    // Fall back to the human text when the CLI predates --json or the
    // binary is missing — the message is still useful.
    try {
      const raw = await runFriday(["talk", phrase]);
      return { action: "chat", response: raw };
    } catch (err) {
      return { action: "failed", response: String(err) };
    }
  }
}

function showResult(title: string, json: TalkJson, channel: vscode.OutputChannel) {
  channel.appendLine(`── ${title} ──`);
  channel.appendLine(`${json.response || "(no response)"}`);
  channel.appendLine("");
  channel.show(true);
  if (json.action === "denied") {
    vscode.window.showWarningMessage(
      `Friday needs your confirmation for that — run it in a terminal to approve.`
    );
  } else if (json.action === "failed") {
    vscode.window.showErrorMessage(`Friday: ${json.response || "failed"}`);
  }
}

// ---------------------------------------------------------------------------
// Learned-apps tree
// ---------------------------------------------------------------------------

class AliasTreeItem extends vscode.TreeItem {
  constructor(
    public readonly name: string,
    public readonly binary: string
  ) {
    super(name, vscode.TreeItemCollapsibleState.None);
    this.description = binary;
    this.tooltip = `'${name}' → ${binary}`;
    this.iconPath = new vscode.ThemeIcon("apps");
  }
}

class AliasProvider implements vscode.TreeDataProvider<AliasTreeItem> {
  private _onDidChangeTreeData = new vscode.EventEmitter<AliasTreeItem | undefined>();
  readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

  refresh(): void {
    this._onDidChangeTreeData.fire(undefined);
  }

  dispose(): void {
    this._onDidChangeTreeData.dispose();
  }

  getTreeItem(element: AliasTreeItem): vscode.TreeItem {
    return element;
  }

  getChildren(): Thenable<AliasTreeItem[]> {
    return this.load();
  }

  private async load(): Promise<AliasTreeItem[]> {
    try {
      // --json is the machine contract (the human table is ANSI-colored).
      const raw = await runFriday(["desktop", "aliases", "--json"]);
      const data = JSON.parse(raw) as Record<string, string>;
      return Object.entries(data).map(
        ([name, binary]) => new AliasTreeItem(name, binary)
      );
    } catch {
      return [];
    }
  }
}

// ---------------------------------------------------------------------------
// activation
// ---------------------------------------------------------------------------

export function activate(context: vscode.ExtensionContext) {
  const channel = vscode.window.createOutputChannel("Friday V4");
  context.subscriptions.push(channel);

  const statusBar = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Right,
    100
  );
  statusBar.text = "$(sparkle) Friday";
  statusBar.tooltip = "Ask Friday (Ctrl+Alt+T)";
  statusBar.command = "friday.talk";
  statusBar.show();
  context.subscriptions.push(statusBar);

  const aliases = new AliasProvider();
  vscode.window.registerTreeDataProvider("friday.aliases", aliases);

  const talk = vscode.commands.registerCommand("friday.talk", async () => {
    const phrase = await vscode.window.showInputBox({
      prompt: "Ask Friday — natural language",
      placeHolder: "e.g. open main.py in the editor, what's wrong with auth.py, run the tests",
      ignoreFocusOut: true,
    });
    if (!phrase || !phrase.trim()) {
      return;
    }
    statusBar.text = "$(sync~spin) Friday…";
    const result = await talkJson(phrase.trim());
    statusBar.text = "$(sparkle) Friday";
    showResult(`friday4 talk "${phrase.trim()}"`, result, channel);
  });

  const diagnose = vscode.commands.registerCommand("friday.diagnose", async () => {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
      vscode.window.showInformationMessage("Open a file first.");
      return;
    }
    const rel = vscode.workspace.asRelativePath(editor.document.uri);
    statusBar.text = "$(sync~spin) Friday…";
    const result = await talkJson(`what's wrong with ${rel}`);
    statusBar.text = "$(sparkle) Friday";
    showResult(`Diagnose ${rel}`, result, channel);
  });

  const openCurrent = vscode.commands.registerCommand("friday.openCurrent", async () => {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
      vscode.window.showInformationMessage("Open a file first.");
      return;
    }
    const rel = vscode.workspace.asRelativePath(editor.document.uri);
    const line = editor.selection.active.line + 1;
    statusBar.text = "$(sync~spin) Friday…";
    const result = await talkJson(`jump to line ${line} of ${rel}`);
    statusBar.text = "$(sparkle) Friday";
    showResult(`Reveal ${rel}:${line}`, result, channel);
  });

  const statusCmd = vscode.commands.registerCommand("friday.status", async () => {
    try {
      const raw = await runFriday(["status"]);
      channel.appendLine(raw);
      channel.show(true);
    } catch (err) {
      vscode.window.showErrorMessage(String(err));
    }
  });

  const refresh = vscode.commands.registerCommand("friday.aliases.refresh", () => {
    aliases.refresh();
  });

  context.subscriptions.push(
    talk,
    diagnose,
    openCurrent,
    statusCmd,
    refresh,
    aliases
  );
}

export function deactivate(): void {
  // nothing to tear down — the CLI owns all state
}
