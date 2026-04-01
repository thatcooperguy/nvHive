# NVHive — Privacy Policy

**Effective Date:** 2026-03-31
**Version:** 1.0

## Summary

NVHive is a local-first tool. We don't run servers, don't collect telemetry, and don't see your data. Your queries go directly from your device to the AI provider you choose — we're not in the middle.

## What NVHive Stores (on YOUR device only)

| Data | Location | Purpose |
|---|---|---|
| Config | ~/.hive/config.yaml | Your settings and preferences |
| API keys | OS keychain | Authenticate with AI providers |
| Conversations | ~/.hive/council.db | Chat history (local SQLite) |
| Memory | ~/.hive/memory/ | Persistent context across sessions |
| Knowledge base | ~/.hive/knowledge/ | Ingested documents for RAG |
| Model weights | ~/nvh/models/ | Local AI models (Nemotron, etc.) |

**All data stays on your device.** NVHive has no server, no cloud backend, no analytics endpoint.

## What Leaves Your Device

| When | What is sent | Where | Your control |
|---|---|---|---|
| Cloud AI query | Your prompt text | The AI provider you selected | Choose provider, or use `nvh safe` for local-only |
| Provider signup | Your email | The provider's signup page | You enter it directly on their site |
| Web search | Search query | DuckDuckGo/Brave/Google | Configurable engine choice |
| URL fetch | The URL | The target website | Only when you explicitly request it |
| `nvh update` | Git pull request | GitHub | Only when you run update |

## What NEVER Leaves Your Device

- Your API keys (stored in OS keychain, sent only to the respective provider)
- Your conversation history
- Your memory/preferences
- Your knowledge base documents
- Your configuration
- Any data processed in safe mode (`nvh safe`)

## Safe Mode

`nvh safe "your question"` guarantees:
- Query processed by local Ollama model only
- Zero network requests made
- No logging, no caching, no persistence
- Nothing leaves your machine

## Third-Party AI Providers

When you use a cloud AI provider through NVHive:
- Your prompts are sent directly to that provider's API
- The provider's own privacy policy governs how they handle your data
- Some providers may use your data for model training (check their policies)
- NVHive does not add, modify, or store your prompts beyond local caching

### Provider Privacy Policies
- OpenAI: https://openai.com/privacy
- Anthropic: https://www.anthropic.com/privacy
- Google: https://policies.google.com/privacy
- Groq: https://groq.com/privacy-policy
- Others: Check each provider's website

## NVIDIA

- NVHive uses NVIDIA's NVML library for GPU detection (local only)
- Nemotron models are downloaded from Ollama's registry and run locally
- GPU diagnostic data (`nvh debug --nvidia-report`) stays on your device unless you choose to share it
- NVHive does not send any data to NVIDIA

## Children

NVHive is a developer tool intended for users aged 13+. We do not knowingly collect information from children under 13.

## Your Rights

Since all data is stored locally on your device:
- **Access**: Read your data at ~/.hive/ and ~/nvh/
- **Delete**: `rm -rf ~/.hive ~/nvh` removes everything
- **Portability**: Copy ~/.hive/ to another machine
- **Control**: You choose which providers to use and what data to share

## Changes

This policy may be updated. Check the repository for the latest version.

---

**NOTE: This document is a template. Consult qualified legal counsel for your jurisdiction before commercial deployment.**
