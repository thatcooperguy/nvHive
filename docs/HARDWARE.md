# NVHive — Hardware Requirements

## Minimum Requirements
- **CPU**: Any x86_64 processor (Intel or AMD)
- **RAM**: 8 GB (16 GB recommended)
- **Disk**: 5 GB free (more for model downloads)
- **OS**: Linux (Ubuntu 20.04+), macOS 13+
- **Python**: 3.12+

## GPU Requirements (for Local AI)
NVIDIA GPU recommended but NOT required. Without a GPU, NVHive uses cloud AI providers only.

### GPU → Model Mapping

| GPU | VRAM | Best Local Model | tok/s (approx) | Notes |
|-----|------|-------------------|-----------------|-------|
| No GPU | — | Cloud only | N/A | Free tiers: LLM7, Groq, Google Gemini |
| GTX 1660 / RTX 2060 | 6 GB | gemma3:4b + moondream | varies | Basic local AI |
| RTX 3060 | 12 GB | qwen3:8b + minicpm-v | varies | Good for students |
| RTX 3070 / 3080 | 8-10 GB | qwen3:8b + compact vision | varies | Great balance |
| RTX 3090 | 24 GB | llama3.2-vision + qwen3:8b | varies | Full local suite |
| RTX 4060 | 8 GB | qwen3:8b | varies | Ada architecture boost |
| RTX 4070 | 12 GB | qwen3:8b + minicpm-v | varies | Sweet spot |
| RTX 4070 Ti | 16 GB | qwen/coder + minicpm-v | varies | Excellent |
| RTX 4080 | 16 GB | qwen/coder + minicpm-v | varies | Premium |
| RTX 4090 | 24 GB | llama3.2-vision + qwen3:8b | varies | Best consumer multimodal |
| RTX 5090 | 32 GB | llama3.2-vision + coder fallback | varies | Next-gen |
| A100 80GB | 80 GB | nemotron 70B (full) | ~120 tok/s | Datacenter |
| H100 | 80 GB | nemotron + multimodal fallback | varies | Flagship |

### Impact on Gaming Performance
- NVHive uses GPU VRAM, not compute cores, when idle
- While actively generating: ~50-100% GPU utilization (brief, during inference)
- When idle: ~0% GPU, 200-500 MB VRAM for loaded model
- To free VRAM for gaming: `nvh stop` or close the REPL
- Models unload after inactivity (configurable)

### No GPU? No Problem
NVHive works WITHOUT a GPU using free cloud providers:
- LLM7: anonymous, no signup, 30 RPM
- Groq: free tier, ultra-fast
- Google Gemini: free tier, 15 RPM

Set up with: `nvh setup`
