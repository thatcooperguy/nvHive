# Supported AI Providers

nvHive manages 23 providers and 63 models behind a single `nvh` command, picking the best advisor based on task type, cost, and privacy requirements.

## Provider Table

| Provider | Free Tier | Best For | Models |
|----------|-----------|----------|--------|
| Ollama (Local) | Unlimited | Privacy, offline | nemotron, codellama, llama3 |
| LLM7 | 30 RPM, no signup | Anonymous, instant start | Multiple |
| Groq | 30 RPM free | Ultra-fast inference | llama3, mixtral, gemma |
| GitHub Models | 50-150 req/day | Free frontier models | GPT-4o, Llama, Mistral |
| Google Gemini | 15 RPM free | Long context, multimodal | Gemini 1.5 Pro/Flash |
| NVIDIA NIM | 1000 free credits | NVIDIA-optimized | Nemotron, Llama |
| Cerebras | 30 RPM free | Fast inference | Llama3 |
| SambaNova | Free tier | Llama models | Llama3 |
| Fireworks AI | Free tier | Fast open-source | Multiple |
| SiliconFlow | 1000 RPM free | High-throughput | Multiple |
| Hugging Face | Free API | Open-source models | Thousands |
| AI21 Labs | Free tier | Jamba models | Jamba |
| Mistral | 2 RPM free | Code | Mistral, Mixtral |
| Cohere | Trial key | RAG, embeddings | Command R+ |
| OpenAI | Paid | GPT-4o, reasoning | GPT-4o, o1, o3 |
| Anthropic | Paid | Analysis, coding | Claude 3.5/4 |
| DeepSeek | Very cheap | Code, reasoning | DeepSeek V3/R1 |
| Grok (xAI) | Paid | Real-time knowledge | Grok |
| Perplexity | Paid | Search-augmented | pplx-online |
| Together AI | Paid | Open-source models | Multiple |
| OpenRouter | Paid | Meta-router, fallback | All models |
| Mock | N/A | Unit tests | N/A |

**25 models are free** across 14 providers. Run `nvh setup` to configure any of them.

## Direct Advisor Access

Skip the router and talk directly to a provider:

```bash
nvh openai "question"       # Route to OpenAI
nvh groq "question"         # Route to Groq
nvh google "question"       # Route to Gemini
nvh ollama "question"       # Route to local Ollama
```

Works for all 23 providers. Run `nvh <provider>` with no question to launch that provider's setup.

## GPU-Adaptive Model Selection

nvHive detects your GPU and automatically selects the best local model:

| GPU | VRAM | Best Local Model | Performance |
|-----|------|-------------------|-------------|
| No GPU | -- | Cloud only | Free tiers: LLM7, Groq, GitHub Models |
| GTX 1660 / RTX 2060 | 6 GB | nemotron-mini (4B) | ~30 tok/s |
| RTX 3060 | 12 GB | nemotron-small | ~55 tok/s |
| RTX 3070 / 3080 | 8-10 GB | nemotron-small | ~75 tok/s |
| RTX 3090 | 24 GB | nemotron-small + codellama | ~100 tok/s |
| RTX 4060 | 8 GB | nemotron-small | ~70 tok/s |
| RTX 4070 | 12 GB | nemotron-small | ~90 tok/s |
| RTX 4080 | 16 GB | nemotron-small + models | ~130 tok/s |
| RTX 4090 | 24 GB | nemotron 70B (Q4) | ~40 tok/s (70B) |
| RTX 5090 | 32 GB | nemotron 70B (Q4) | ~60 tok/s (70B) |
| A100 / H100 | 80 GB | nemotron 70B (full) | ~120-180 tok/s |

Models unload after inactivity to free VRAM for gaming. Run `nvh bench` to measure your actual throughput.

---

Back to [README](../README.md)
