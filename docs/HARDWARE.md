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
| No GPU | — | Cloud only | N/A | Free tiers: LLM7, Groq, GitHub Models |
| GTX 1660 / RTX 2060 | 6 GB | nemotron-mini (4B) | ~30 tok/s | Basic local AI |
| RTX 3060 | 12 GB | nemotron-small | ~55 tok/s | Good for students |
| RTX 3070 / 3080 | 8-10 GB | nemotron-small | ~75 tok/s | Great balance |
| RTX 3090 | 24 GB | nemotron-small + codellama | ~100 tok/s | Full local suite |
| RTX 4060 | 8 GB | nemotron-small | ~70 tok/s | Ada architecture boost |
| RTX 4070 | 12 GB | nemotron-small | ~90 tok/s | Sweet spot |
| RTX 4070 Ti | 16 GB | nemotron-small + models | ~110 tok/s | Excellent |
| RTX 4080 | 16 GB | nemotron-small + models | ~130 tok/s | Premium |
| RTX 4090 | 24 GB | nemotron 70B (Q4) | ~40 tok/s (70B) | Best consumer |
| RTX 5090 | 32 GB | nemotron 70B (Q4) | ~60 tok/s (70B) | Next-gen |
| A100 80GB | 80 GB | nemotron 70B (full) | ~120 tok/s | Datacenter |
| H100 | 80 GB | nemotron:120b | ~180 tok/s | Flagship |

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
- GitHub Models: free GPT-4o (50/day)
- Google Gemini: free tier, 15 RPM

Set up with: `nvh setup`
