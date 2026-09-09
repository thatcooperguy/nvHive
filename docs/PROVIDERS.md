# Providers

nvHive routes across a local Ollama, your own Triton server, and the cloud
providers below through one `nvh` command, choosing by task type, cost,
latency, health and privacy. Every cloud provider except Ollama and Triton is
one `ProviderSpec` row in `nvh/providers/specs.py` served by a single
`OpenAICompatibleProvider` over LiteLLM. The table below is generated from
that file by `scripts/gen_providers_doc.py` — the same rows feed `nvh keys`,
the free-tier ladder, the rate-limit messages and `nvh advisor info`.

## Provider table

<!-- BEGIN GENERATED: providers -->
| Provider | `config.yaml` name | Key variable | Free tier | Default model |
|---|---|---|---|---|
| Ollama (local) | `ollama` | — | Unlimited, free, runs on your GPU | `ollama/gemma3:4b` (VRAM-tiered) |
| LLM7 | `llm7` | `LLM7_API_KEY` (optional) | Anonymous access: 30 RPM, no signup. Token: 120 RPM | `gpt-oss` |
| Groq | `groq` | `GROQ_API_KEY` | Free tier: 30 req/min, 14.4K tok/min | `groq/openai/gpt-oss-120b` |
| Google Gemini | `google` | `GOOGLE_API_KEY` (`GEMINI_API_KEY`) | Free tier: 15 req/min, 1M tokens/day | `gemini/gemini-3.7-flash` |
| Mistral | `mistral` | `MISTRAL_API_KEY` | Free Experiment plan: 2 RPM, 1B tokens/month | `mistral/mistral-large-latest` |
| Cohere | `cohere` | `COHERE_API_KEY` (`CO_API_KEY`) | Trial API key included on signup | `command-a-03-2025` |
| NVIDIA NIM | `nvidia` | `NVIDIA_API_KEY` (`NIM_API_KEY`) | 1000+ free API credits, 40 RPM, NVIDIA Developer Program | `nvidia_nim/meta/llama-3.3-70b-instruct` |
| SiliconFlow | `siliconflow` | `SILICONFLOW_API_KEY` | Permanently free models at 1000 RPM | `Qwen/Qwen2.5-7B-Instruct` |
| Fireworks AI | `fireworks` | `FIREWORKS_API_KEY` (`FIREWORKS_AI_API_KEY`) | Free tier available | `fireworks_ai/accounts/fireworks/models/gpt-oss-120b` |
| Cerebras | `cerebras` | `CEREBRAS_API_KEY` | Free tier: 30 req/min | `cerebras/gpt-oss-120b` |
| SambaNova | `sambanova` | `SAMBANOVA_API_KEY` | Free tier available | `sambanova/Meta-Llama-3.3-70B-Instruct` |
| Hugging Face | `huggingface` | `HUGGINGFACE_API_KEY` (`HF_TOKEN`) | Free Inference API | `huggingface/openai/gpt-oss-120b` |
| AI21 Labs | `ai21` | `AI21_API_KEY` | Free tier available | `ai21_chat/jamba-large-1.7` |
| OpenAI | `openai` | `OPENAI_API_KEY` | paid | `gpt-5.6-terra` |
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` | paid | `claude-sonnet-5` |
| Grok (xAI) | `grok` | `XAI_API_KEY` | paid | `xai/grok-4.6` |
| DeepSeek | `deepseek` | `DEEPSEEK_API_KEY` | paid — Very cheap: $0.07/M input tokens | `deepseek/deepseek-v4-pro` |
| Perplexity | `perplexity` | `PERPLEXITY_API_KEY` (`PERPLEXITYAI_API_KEY`) | paid — Search-augmented responses with citations | `perplexity/preset/low` |
| Together AI | `together` | `TOGETHER_API_KEY` (`TOGETHERAI_API_KEY`) | paid — Requires $5 minimum purchase | `together_ai/openai/gpt-oss-120b` |
| OpenRouter | `openrouter` | `OPENROUTER_API_KEY` | paid — Routes to best available provider | `openrouter/openai/gpt-oss-120b` |
| Triton | `triton` | `TRITON_URL` | your own inference server | — |
| Mock | `mock` | — | tests only, no network | `mock/default` |
<!-- END GENERATED: providers -->

Rate limits and quotas belong to the providers and change without notice;
`nvh keys` prints each signup page, `nvh advisor info <name>` shows the
current strengths, weaknesses and quota notes nvHive ships with. Perplexity
is served through its Agent API (the Responses shape); Sonar Chat
Completions retires 2026-09-27. Default and fallback model IDs were verified
against LiteLLM's model database for 0.41.1 and are reverified each release;
`nvh status --deep` warns when an enabled provider's default has since been
retired, and `nvh config migrate` rewrites it in your `config.yaml`. GitHub
Models was retired by GitHub on 2026-07-30 and is no longer a provider; a
leftover `github:` stanza is skipped with a warning until `nvh config migrate`
removes it.

## Adding and testing providers

```bash
nvh setup                      # free-tier wizard: Ollama, LLM7, then keyed free tiers
nvh advisor add groq           # paste one key (also: nvh advisor login groq)
nvh advisor test               # connectivity + key validity for every enabled advisor
nvh advisor list               # what is enabled and healthy
nvh advisor remove groq        # scrub the key from keyring and .env, disable the stanza
nvh ask "question" -p groq     # bypass the router for one query
```

Keys resolve in this order: the `api_key` value in `config.yaml` (usually a
`${VAR}` reference), then `COUNCIL_<NAME>_API_KEY` and `<NAME>_API_KEY` in the
environment, then the provider's own variables from its spec, then the OS
keyring when `NVH_USE_KEYRING=1`. The CLI and the API server both load
`$NVH_HOME/config/.env` at startup, so keys saved by the Wizard or `nvh setup`
work everywhere without exporting anything.

The dashboard's **AI Connections** page does the same with a **Test
Connection** button per card. See [CONFIGURATION.md](CONFIGURATION.md) for
the `advisors:` stanza fields and [GETTING_STARTED.md](GETTING_STARTED.md)
for the rootless key story.

## Free-tier routing

With no paid keys configured the router works down a fixed preference list,
derived by `nvh/core/free_tier.py` from each provider's `free_tier_rank` in
`nvh/providers/specs.py`:

<!-- BEGIN GENERATED: free-tier-ladder -->
1. **Ollama (local)** (`ollama`) — Unlimited, free, runs on your GPU; a running local daemon
2. **Groq** (`groq`) — Free tier: 30 req/min, 14.4K tok/min; `GROQ_API_KEY`
3. **Google Gemini** (`google`) — Free tier: 15 req/min, 1M tokens/day; `GOOGLE_API_KEY`
4. **Mistral** (`mistral`) — Free Experiment plan: 2 RPM, 1B tokens/month; `MISTRAL_API_KEY`
5. **Cohere** (`cohere`) — Trial API key included on signup; `COHERE_API_KEY`
6. **NVIDIA NIM** (`nvidia`) — 1000+ free API credits, 40 RPM, NVIDIA Developer Program; `NVIDIA_API_KEY`
7. **SiliconFlow** (`siliconflow`) — Permanently free models at 1000 RPM; `SILICONFLOW_API_KEY`
8. **LLM7** (`llm7`) — Anonymous access: 30 RPM, no signup. Token: 120 RPM; no key needed
9. **Fireworks AI** (`fireworks`) — Free tier available; `FIREWORKS_API_KEY`
10. **Cerebras** (`cerebras`) — Free tier: 30 req/min; `CEREBRAS_API_KEY`
11. **SambaNova** (`sambanova`) — Free tier available; `SAMBANOVA_API_KEY`
12. **Hugging Face** (`huggingface`) — Free Inference API; `HUGGINGFACE_API_KEY`
13. **AI21 Labs** (`ai21`) — Free tier available; `AI21_API_KEY`
<!-- END GENERATED: free-tier-ladder -->

LLM7 is the only cloud provider enabled by default because it needs no
account. Once you add paid keys the router scores capability, cost, latency
and health (`routing.weights` in `config.yaml`) and `nvh ask --fast` or
`--strategy cheapest` still prefer the free tiers.

## Local and NVIDIA-hosted

- **Ollama** is discovered at `OLLAMA_BASE_URL` (default
  `http://localhost:11434`); the rootless binary under `$NVH_HOME/bin` is
  managed by `nvh services` and `nvh models`. See [MODELS.md](MODELS.md).
  Every request to a loopback daemon carries this machine's VRAM tier
  `num_ctx`, capped at the model's own context; with no visible GPU or a
  non-loopback `OLLAMA_BASE_URL` nothing is sent and Ollama's default
  applies, unless `NVH_OLLAMA_NUM_CTX` is set — a positive integer
  overrides the tier (still capped) and `0` sends none
  ([CONFIGURATION.md](CONFIGURATION.md#environment-variables)).
- **NVIDIA NIM** IDs carry the `nvidia_nim/` prefix so LiteLLM routes them to
  `integrate.api.nvidia.com`; the adapter adds it to any ID you pass.
- **Triton** talks to a TensorRT-LLM / Triton Inference Server at `TRITON_URL`
  for on-prem deployments; `nvh nvidia` shows all three in one dashboard and
  `--prefer-nvidia` biases routing toward them.

## Adding a provider to nvHive

One `ProviderSpec` row plus a catalog entry — see
[CONTRIBUTING.md](../CONTRIBUTING.md#how-to-add-a-provider).

Back to [README](../README.md)
