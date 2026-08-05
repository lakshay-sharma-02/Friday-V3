"use strict";
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
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.activate = activate;
exports.deactivate = deactivate;
const vscode = __importStar(require("vscode"));
const child_process_1 = require("child_process");
// ---------------------------------------------------------------------------
// CLI plumbing
// ---------------------------------------------------------------------------
function binaryPath() {
    const cfg = vscode.workspace.getConfiguration("friday");
    return cfg.get("binaryPath", "friday4");
}
function workspaceDir() {
    const cfg = vscode.workspace.getConfiguration("friday");
    const explicit = cfg.get("workspacePath", "");
    if (explicit) {
        return explicit;
    }
    return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || process.cwd();
}
/** Run the friday4 CLI; resolves with combined stdout+stderr. */
function runFriday(args, cwd) {
    return new Promise((resolve, reject) => {
        (0, child_process_1.execFile)(binaryPath(), args, { cwd: cwd || workspaceDir(), timeout: 60_000, maxBuffer: 4 * 1024 * 1024 }, (error, stdout, stderr) => {
            const out = `${stdout || ""}${stderr || ""}`.trim();
            if (error && !out) {
                reject(new Error(`friday4 failed (${error.message})`));
                return;
            }
            resolve(out);
        });
    });
}
/** `friday4 talk "<phrase>" --json` → parsed result (never throws). */
async function talkJson(phrase) {
    try {
        const raw = await runFriday(["talk", phrase, "--json"]);
        return JSON.parse(raw);
    }
    catch {
        // Fall back to the human text when the CLI predates --json or the
        // binary is missing — the message is still useful.
        try {
            const raw = await runFriday(["talk", phrase]);
            return { action: "chat", response: raw };
        }
        catch (err) {
            return { action: "failed", response: String(err) };
        }
    }
}
function showResult(title, json, channel) {
    channel.appendLine(`── ${title} ──`);
    channel.appendLine(`${json.response || "(no response)"}`);
    channel.appendLine("");
    channel.show(true);
    if (json.action === "denied") {
        vscode.window.showWarningMessage(`Friday needs your confirmation for that — run it in a terminal to approve.`);
    }
    else if (json.action === "failed") {
        vscode.window.showErrorMessage(`Friday: ${json.response || "failed"}`);
    }
}
// ---------------------------------------------------------------------------
// Learned-apps tree
// ---------------------------------------------------------------------------
class AliasTreeItem extends vscode.TreeItem {
    constructor(name, binary) {
        super(name, vscode.TreeItemCollapsibleState.None);
        this.name = name;
        this.binary = binary;
        this.description = binary;
        this.tooltip = `'${name}' → ${binary}`;
        this.iconPath = new vscode.ThemeIcon("apps");
    }
}
class AliasProvider {
    constructor() {
        this._onDidChangeTreeData = new vscode.EventEmitter();
        this.onDidChangeTreeData = this._onDidChangeTreeData.event;
    }
    refresh() {
        this._onDidChangeTreeData.fire(undefined);
    }
    dispose() {
        this._onDidChangeTreeData.dispose();
    }
    getTreeItem(element) {
        return element;
    }
    getChildren() {
        return this.load();
    }
    async load() {
        try {
            // --json is the machine contract (the human table is ANSI-colored).
            const raw = await runFriday(["desktop", "aliases", "--json"]);
            const data = JSON.parse(raw);
            return Object.entries(data).map(([name, binary]) => new AliasTreeItem(name, binary));
        }
        catch {
            return [];
        }
    }
}
// ---------------------------------------------------------------------------
// activation
// ---------------------------------------------------------------------------
function activate(context) {
    const channel = vscode.window.createOutputChannel("Friday V4");
    context.subscriptions.push(channel);
    const statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
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
        }
        catch (err) {
            vscode.window.showErrorMessage(String(err));
        }
    });
    const refresh = vscode.commands.registerCommand("friday.aliases.refresh", () => {
        aliases.refresh();
    });
    context.subscriptions.push(talk, diagnose, openCurrent, statusCmd, refresh, aliases);
}
function deactivate() {
    // nothing to tear down — the CLI owns all state
}
//# sourceMappingURL=extension.js.map