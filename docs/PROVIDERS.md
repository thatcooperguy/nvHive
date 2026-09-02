# Supported AI Providers

nvHive manages local Ollama plus the cloud providers in `nvh/providers/` behind a single `nvh` command, picking the best advisor based on task type, cost, and privacy requirements.

## Provider Table

| Provider | Free Tier | Best For | Models |
|----------|-----------|----------|--------|
| Ollama (Local) | Unlimited | Privacy, offline | gemma3, nemotron, qwen3 |
| LLM7 | 30 RPM, no signup | Anonymous, instant start | gpt-oss, minimax-m2.7 |
| Groq | 30 RPM free | Ultra-fast inference | gpt-oss-120b, gpt-oss-20b |
| Google Gemini | 15 RPM free | Long context, multimodal | Gemini 3.7 Flash, 3.5 Flash-Lite |
| NVIDIA NIM | 1000 free credits | NVIDIA-optimized | Llama 3.3 70B, Llama 3.1 8B |
| Cerebras | 30 RPM free | Fast inference | gpt-oss-120b |
| SambaNova | Free tier | Llama models | Llama 3.3 70B, gpt-oss-120b |
| Fireworks AI | Free tier | Fast open-source | gpt-oss-120b, Nemotron Lightning |
| SiliconFlow | 1000 RPM free | High-throughput | Qwen2.5 |
| Hugging Face | Free API | Open-source models | gpt-oss-120b, gpt-oss-20b |
| AI21 Labs | Free tier | Jamba models | Jamba Large 1.7, Jamba Mini 2 |
| Mistral | 2 RPM free | Code | Mistral Large, Small |
| Cohere | Trial key | RAG, embeddings | Command A, Command R |
| OpenAI | Paid | Reasoning, multimodal | GPT-5.6 Terra, GPT-5.6 Luna |
| Anthropic | Paid | Analysis, coding | Claude Sonnet 5, Haiku 4.5 |
| DeepSeek | Very cheap | Code, reasoning | DeepSeek V4 Pro/Flash |
| Grok (xAI) | Paid | Real-time knowledge | Grok 4.6, Grok 4.3 |
| Perplexity | Paid | Search-augmented | Sonar Pro, Sonar |
| Together AI | Paid | Open-source models | gpt-oss-120b, gpt-oss-20b |
| OpenRouter | Paid | Meta-router, fallback | All models |
| Mock | N/A | Unit tests | N/A |

Most providers above have a free tier. Run `nvh setup` to configure any of them.

## Direct Advisor Access

Skip the router and talk directly to a provider:

```bash
nvh openai "question"       # Route to OpenAI
nvh groq "question"         # Route to Groq
nvh google "question"       # Route to Gemini
nvh ollama "question"       # Route to local Ollama
```

Works for every provider in the table above. Run `nvh <provider>` with no question to launch that provider's setup.

## GPU-Adaptive Model Selection

nvHive detects your GPU and automatically selects the best local model:

| GPU | VRAM | Best Local Model | Performance |
|-----|------|-------------------|-------------|
| No GPU | -- | Cloud only | Free tiers: LLM7, Groq, Google Gemini |
| GTX 1660 / RTX 2060 | 6 GB | gemma3:4b + moondream | varies |
| RTX 3060 | 12 GB | qwen3:8b + minicpm-v | varies |
| RTX 3070 / 3080 | 8-10 GB | qwen3:8b + compact vision | varies |
| RTX 3090 | 24 GB | llama3.2-vision + qwen3:8b | varies |
| RTX 4060 | 8 GB | qwen3:8b | varies |
| RTX 4070 | 12 GB | qwen3:8b + minicpm-v | varies |
| RTX 4080 | 16 GB | qwen/coder + minicpm-v | varies |
| RTX 4090 | 24 GB | llama3.2-vision + qwen3:8b | varies |
| RTX 5090 | 32 GB | llama3.2-vision + coder fallback | varies |
| A100 / H100 | 80 GB | nemotron + multimodal fallback | varies |

Models unload after inactivity to free VRAM for gaming. Run `nvh bench` to measure your actual throughput.

---

Back to [README](../README.md)
