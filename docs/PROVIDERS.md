# Providers

nvHive routes across a local Ollama, your own Triton server, and the cloud
providers below through one `nvh` command, choosing by task type, cost,
latency, health and privacy. Every cloud provider except Ollama and Triton is
one `ProviderSpec` row in `nvh/providers/specs.py` served by a single
`OpenAICompatibleProvider` over LiteLLM; this table is hand-maintained from
that file until 0.43 generates it.

## Provider table

| Provider | `config.yaml` name | Key variable | Free tier | Default model |
|---|---|---|---|---|
| Ollama (local) | `ollama` | — | unlimited, runs on your GPU | `ollama/gemma3:4b` (VRAM-tiered) |
| LLM7 | `llm7` | `LLM7_API_KEY` (optional) | anonymous, no signup; a token raises the rate limit | `gpt-oss` |
| Groq | `groq` | `GROQ_API_KEY` | yes, rate-limited | `groq/openai/gpt-oss-120b` |
| Google Gemini | `google` | `GOOGLE_API_KEY` (`GEMINI_API_KEY`) | yes, rate-limited | `gemini/gemini-3.7-flash` |
| Mistral | `mistral` | `MISTRAL_API_KEY` | Experiment plan | `mistral/mistral-large-latest` |
| Cohere | `cohere` | `COHERE_API_KEY` (`CO_API_KEY`) | trial key | `command-a-03-2025` |
| NVIDIA NIM | `nvidia` | `NVIDIA_API_KEY` (`NIM_API_KEY`) | Developer Program credits | `nvidia_nim/meta/llama-3.3-70b-instruct` |
| SiliconFlow | `siliconflow` | `SILICONFLOW_API_KEY` | permanently free models | `Qwen/Qwen2.5-7B-Instruct` |
| Fireworks AI | `fireworks` | `FIREWORKS_API_KEY` | yes | `fireworks_ai/accounts/fireworks/models/gpt-oss-120b` |
| Cerebras | `cerebras` | `CEREBRAS_API_KEY` | yes, rate-limited | `cerebras/gpt-oss-120b` |
| SambaNova | `sambanova` | `SAMBANOVA_API_KEY` | yes | `sambanova/Meta-Llama-3.3-70B-Instruct` |
| Hugging Face | `huggingface` | `HUGGINGFACE_API_KEY` (`HF_TOKEN`) | Inference API | `huggingface/openai/gpt-oss-120b` |
| AI21 Labs | `ai21` | `AI21_API_KEY` | credit on signup | `ai21_chat/jamba-large-1.7` |
| OpenAI | `openai` | `OPENAI_API_KEY` | paid | `gpt-5.6-terra` |
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` | paid | `claude-sonnet-5` |
| DeepSeek | `deepseek` | `DEEPSEEK_API_KEY` | paid, very cheap | `deepseek/deepseek-v4-pro` |
| Grok (xAI) | `grok` | `XAI_API_KEY` | paid | `xai/grok-4.6` |
| Perplexity | `perplexity` | `PERPLEXITY_API_KEY` | paid — Agent API (Responses shape); Sonar Chat Completions retires 2026-09-27 | `perplexity/preset/low` |
| Together AI | `together` | `TOGETHER_API_KEY` | paid | `together_ai/openai/gpt-oss-120b` |
| OpenRouter | `openrouter` | `OPENROUTER_API_KEY` | paid | `openrouter/openai/gpt-oss-120b` |
| Triton | `triton` | `TRITON_URL` | your own inference server | — |
| Mock | `mock` | — | tests only, no network | `mock/default` |

Rate limits and quotas belong to the providers and change without notice;
`nvh keys` prints each signup page, `nvh advisor info <name>` shows the
current strengths, weaknesses and quota notes nvHive ships with. Default and
fallback model IDs were verified against LiteLLM's model database for 0.41.1
and are reverified each release; `nvh status --deep` warns when an enabled
provider's default has since been retired, and `nvh config migrate` rewrites
it in your `config.yaml`. GitHub Models was retired by GitHub on 2026-07-30
and is no longer a provider; a leftover `github:` stanza is skipped with a
warning until `nvh config migrate` removes it.

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
`$NVH_HOME/config/.env` and `~/.hive/.env` at startup, so keys saved by the
Wizard or `nvh setup` work everywhere without exporting anything.

The dashboard's **AI Connections** page does the same with a **Test
Connection** button per card. See [CONFIGURATION.md](CONFIGURATION.md) for
the `advisors:` stanza fields and [GETTING_STARTED.md](GETTING_STARTED.md)
for the rootless key story.

## Free-tier routing

With no paid keys configured the router works down a fixed preference list —
a running Ollama first, then Groq, Google, Mistral, Cohere, NVIDIA NIM,
SiliconFlow, LLM7, Fireworks, Cerebras, SambaNova, Hugging Face and AI21 —
defined in `nvh/core/free_tier.py`. LLM7 is the only cloud provider enabled by
default because it needs no account. Once you add paid keys the router scores
capability, cost, latency and health (`routing.weights` in `config.yaml`) and
`nvh ask --fast` or `--strategy cheapest` still prefer the free tiers.

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
