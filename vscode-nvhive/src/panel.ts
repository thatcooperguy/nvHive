import * as vscode from "vscode";

export class NvHivePanel {
  private panel: vscode.WebviewPanel | undefined;
  private status: "idle" | "running" | "done" = "idle";
  private steps: string[] = [];
  private result = "";

  constructor(private readonly extensionUri: vscode.Uri) {}

  public show() {
    if (this.panel) {
      this.panel.reveal();
      return;
    }
    this.panel = vscode.window.createWebviewPanel(
      "nvhive.panel",
      "nvHive Agent",
      vscode.ViewColumn.Beside,
      { enableScripts: false }
    );
    this.panel.onDidDispose(() => (this.panel = undefined));
    this.refresh();
  }

  public setRunning(task: string) {
    this.status = "running";
    this.steps = [`Started: ${task}`];
    this.result = "";
    this.refresh();
  }

  public addStep(step: string) {
    this.steps.push(step);
    this.refresh();
  }

  public setDone(result: string) {
    this.status = "done";
    this.result = result;
    this.refresh();
  }

  public setIdle() {
    this.status = "idle";
    this.refresh();
  }

  private refresh() {
    if (!this.panel) return;
    const stepsHtml = this.steps.map((s) => `<li>${esc(s)}</li>`).join("");
    this.panel.webview.html = `<!DOCTYPE html>
<html><head><style>
  body { font-family: var(--vscode-font-family, sans-serif); padding: 12px; color: #ccc; background: transparent; }
  h2 { margin: 0 0 8px; font-size: 14px; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; }
  .idle   { background: #555; }
  .running { background: #2a6; color: #fff; }
  .done   { background: #26a; color: #fff; }
  ul { padding-left: 18px; font-size: 13px; }
  pre { white-space: pre-wrap; font-size: 12px; background: #1e1e1e; padding: 8px; border-radius: 4px; }
</style></head><body>
  <h2>Agent <span class="badge ${this.status}">${this.status}</span></h2>
  ${stepsHtml ? `<ul>${stepsHtml}</ul>` : "<p>No active task.</p>"}
  ${this.result ? `<h2>Result</h2><pre>${esc(this.result)}</pre>` : ""}
</body></html>`;
  }
}

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
