# Workflows

Define multi-step pipelines in YAML to chain queries, council sessions, and other actions together.

## Example Pipeline

```yaml
name: Code Review Pipeline
steps:
  - name: security_scan
    action: ask
    prompt: "Analyze for security vulnerabilities:\n\n{{input}}"
    advisor: anthropic
    save_as: security

  - name: quality_review
    action: ask
    prompt: "Review for quality and best practices:\n\n{{input}}"
    advisor: openai
    save_as: quality

  - name: synthesis
    action: convene
    prompt: "Synthesize findings:\n\nSecurity: {{security}}\nQuality: {{quality}}"
    cabinet: code_review
    save_as: summary
```

## Running a Workflow

```bash
nvh workflow run code_review.yaml --input "$(cat main.py)"
```

---

Back to [README](../README.md)
