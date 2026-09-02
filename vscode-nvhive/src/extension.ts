import { execSync } from "child_process";
import * as vscode from "vscode";
import { NvHivePanel } from "./panel";

let statusBar: vscode.StatusBarItem;
let panel: NvHivePanel;

export function activate(ctx: vscode.ExtensionContext) {
  panel = new NvHivePanel(ctx.extensionUri);

  // Status bar
  statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  statusBar.text = "$(sync~spin) nvHive";
  statusBar.show();
  ctx.subscriptions.push(statusBar);
  checkConnection();

  // Commands
  ctx.subscriptions.push(
    vscode.commands.registerCommand("nvhive.agent", cmdAgent),
    vscode.commands.registerCommand("nvhive.review", cmdReview),
    vscode.commands.registerCommand("nvhive.testgen", cmdTestgen),
    vscode.commands.registerCommand("nvhive.explain", cmdExplain),
    vscode.commands.registerCommand("nvhive.council", cmdCouncil)
  );
}

export function deactivate() {}

// ── helpers ──────────────────────────────────────────────────────────

function apiUrl(): string {
  return vscode.workspace.getConfiguration("nvhive").get<string>("apiUrl", "http://localhost:8000");
}

async function apiFetch(path: string, body: Record<string, unknown>): Promise<any> {
  const url = `${apiUrl()}${path}`;
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`API ${res.status}: ${await res.text()}`);
  // Server wraps every payload as {status, data}; unwrap so callers see the payload.
  const json: any = await res.json();
  return json.data ?? json;
}

function queryText(data: any): string {
  return data.content ?? JSON.stringify(data, null, 2);
}

async function checkConnection() {
  try {
    const res = await fetch(`${apiUrl()}/v1/health`);
    if (res.ok) {
      statusBar.text = "$(check) nvHive";
      statusBar.tooltip = "nvHive API connected";
    } else {
      throw new Error();
    }
  } catch {
    statusBar.text = "$(error) nvHive";
    statusBar.tooltip = "nvHive API unreachable -- run `nvh serve`";
  }
}

// ── commands ─────────────────────────────────────────────────────────

async function cmdAgent() {
  const task = await vscode.window.showInputBox({ prompt: "Describe the task for nvHive" });
  if (!task) return;
  panel.show();
  panel.setRunning(task);
  try {
    const data = await apiFetch("/v1/query", { prompt: task });
    panel.setDone(queryText(data));
    vscode.window.showInformationMessage("nvHive: task complete");
  } catch (e: any) {
    panel.setDone(`Error: ${e.message}`);
    vscode.window.showErrorMessage(`nvHive: ${e.message}`);
  }
}

async function cmdReview() {
  panel.show();
  panel.setRunning("Code review (staged diff)");
  try {
    const diff = await getGitDiff();
    if (!diff) { panel.setDone("No staged changes."); return; }
    const data = await apiFetch("/v1/query", { prompt: `Review this diff:\n${diff}` });
    panel.setDone(queryText(data));
  } catch (e: any) {
    panel.setDone(`Error: ${e.message}`);
    vscode.window.showErrorMessage(`nvHive review: ${e.message}`);
  }
}

async function cmdTestgen() {
  const editor = vscode.window.activeTextEditor;
  if (!editor) { vscode.window.showWarningMessage("Open a file first."); return; }
  const code = editor.document.getText();
  const lang = editor.document.languageId;
  panel.show();
  panel.setRunning(`Generate tests for ${editor.document.fileName}`);
  try {
    const data = await apiFetch("/v1/query", { prompt: `Generate tests for this ${lang} code:\n${code}` });
    const result = queryText(data);
    panel.setDone(result);
    // Open result in a new untitled doc
    const doc = await vscode.workspace.openTextDocument({ content: result, language: lang });
    await vscode.window.showTextDocument(doc, vscode.ViewColumn.Beside);
  } catch (e: any) {
    panel.setDone(`Error: ${e.message}`);
    vscode.window.showErrorMessage(`nvHive testgen: ${e.message}`);
  }
}

async function cmdExplain() {
  const editor = vscode.window.activeTextEditor;
  if (!editor) { vscode.window.showWarningMessage("Open a file first."); return; }
  const selection = editor.document.getText(editor.selection);
  if (!selection) { vscode.window.showWarningMessage("Select some code first."); return; }
  panel.show();
  panel.setRunning("Explain selected code");
  try {
    const data = await apiFetch("/v1/query", { prompt: `Explain this code:\n${selection}` });
    panel.setDone(queryText(data));
  } catch (e: any) {
    panel.setDone(`Error: ${e.message}`);
  }
}

async function cmdCouncil() {
  const question = await vscode.window.showInputBox({ prompt: "Question for the nvHive council" });
  if (!question) return;
  panel.show();
  panel.setRunning(`Council: ${question}`);
  try {
    const data = await apiFetch("/v1/council", { prompt: question });
    panel.setDone(data.synthesis?.content ?? JSON.stringify(data, null, 2));
  } catch (e: any) {
    panel.setDone(`Error: ${e.message}`);
    vscode.window.showErrorMessage(`nvHive council: ${e.message}`);
  }
}

async function getGitDiff(): Promise<string> {
  const ws = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  if (!ws) return "";
  try {
    return execSync("git diff --cached", { cwd: ws, encoding: "utf-8" });
  } catch {
    return "";
  }
}
