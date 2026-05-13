# GPU Detection & Model Selection

nvHive auto-detects your GPU hardware and selects the optimal Nemotron model for local inference. No manual configuration needed.

<p align="center">
  <img src="../docs/screenshots/gpu-detection-demo.gif" alt="nvHive GPU Detection" width="640">
</p>

## How It Works

```mermaid
flowchart TB
    START[nvh setup / nvh nvidia] --> DETECT[GPU Detection<br/>pynvml reads VRAM · driver<br/>CUDA · temp · power · PCIe]
    
    DETECT --> VRAM{Available VRAM?}
    
    VRAM -->|No GPU / < 4GB| REC_MINI[Recommend: nemotron-mini<br/>CPU mode · ~2GB RAM]
    VRAM -->|4 – 6 GB| REC_MINI_GPU[Recommend: nemotron-mini<br/>GPU accelerated]
    VRAM -->|6-12 GB| REC_SMALL[Recommend: qwen3:8b<br/>+ compact vision fallback]
    VRAM -->|12-24 GB| REC_DUAL[Recommend: qwen/coder<br/>+ MiniCPM-V]
    VRAM -->|24-40 GB| REC_FULL[Recommend: llama3.2-vision<br/>+ coder fallback]
    VRAM -->|40 GB+| REC_FLAG[Recommend: nemotron first<br/>+ multimodal fallback]

    REC_MINI --> OLLAMA_CHECK
    REC_MINI_GPU --> OLLAMA_CHECK
    REC_SMALL --> OLLAMA_CHECK
    REC_DUAL --> OLLAMA_CHECK
    REC_FULL --> OLLAMA_CHECK
    REC_FLAG --> OLLAMA_CHECK
    
    OLLAMA_CHECK{Ollama running?}
    
    OLLAMA_CHECK -->|Not installed| INSTALL[Show install command<br/>curl -fsSL ollama.com/install.sh]
    OLLAMA_CHECK -->|Installed, not running| START_OLL[Show: ollama serve]
    OLLAMA_CHECK -->|Running| MODEL_CHECK{Model already<br/>pulled?}
    
    MODEL_CHECK -->|Yes| READY[Model ready ✓<br/>Registered as provider]
    MODEL_CHECK -->|No| PULL[Pull model now? Y/n<br/>ollama pull best fitting model]
    PULL --> READY
    
    READY --> ROUTE[nvHive Router<br/>Local GPU provider active<br/>Learning loop measures quality]
    
    style REC_SMALL fill:#76B900,color:#000
    style DETECT fill:#1a1a2e,color:#76B900,stroke:#76B900
    style READY fill:#76B900,color:#000
    style ROUTE fill:#1a1a2e,color:#76B900,stroke:#76B900
```

## Model Recommendations by VRAM

| VRAM | Primary Local Model | Multimodal Fallback | Use Case |
|------|---------------------|---------------------|----------|
| No GPU / < 4 GB | cloud/free providers | optional tiny local | CPU mode |
| 4-8 GB | `gemma3:4b` | `moondream` | lightweight local chat |
| 8-12 GB | `qwen3:8b` or `llama3.1:8b` | `moondream` / `llava:7b` | student chat and code |
| 12-24 GB | `qwen3:8b` / `qwen2.5-coder:7b` | `minicpm-v` | coding plus image help |
| 24-40 GB | `llama3.2-vision` | `qwen3:8b` | multimodal desktop assistant |
| 40 GB+ | `nemotron` | `llama3.2-vision` + coding fallback | strongest local AI first |

**Local council:** nvHive pulls the strongest fitting primary model first, then smaller multimodal/coding fallbacks so the wizard can keep working if the largest model is busy or unavailable.

Multi-GPU systems: Ollama automatically distributes layers across all detected GPUs.

## GPU Detection Details

nvHive uses **pynvml** (NVIDIA Management Library Python bindings) for direct GPU access. Falls back to parsing `nvidia-smi` output if pynvml is not installed.

### What pynvml reads:
- GPU model name (e.g. "NVIDIA GeForce RTX 4090")
- Total and available VRAM
- Driver version and CUDA version
- GPU utilization percentage
- Temperature (Celsius)
- Power draw and power limit (watts)
- GPU and memory clock speeds (MHz)
- PCIe generation and width
- Running processes using the GPU

### What nvidia-smi reads (fallback):
- GPU model name
- Total and used VRAM
- Driver version
- GPU utilization

```bash
# Install pynvml for full detection (optional — nvidia-smi fallback works)
pip install nvidia-ml-py3

# See what nvHive detects
nvh nvidia
```

## Automatic Setup via `nvh setup`

The easiest way to get local inference running is `nvh setup`. Step 3 handles everything:

1. Detects your GPU and recommends the optimal Nemotron model
2. Checks if Ollama is installed and running
3. Checks if the recommended model is already pulled
4. If not, asks before pulling the strongest fitting model for your GPU
5. After pulling, registers the model with nvHive's router

No manual configuration needed. One wizard, zero to local GPU inference.

## Commands

```bash
# Full setup wizard (includes GPU detection + model pull)
nvh setup

# GPU + inference stack status
nvh nvidia

# Benchmark your GPU (tokens/sec)
nvh bench

# Force all queries to local GPU
nvh safe "your question"

# Routing bonus for NVIDIA hardware
nvh --prefer-nvidia "your question"

# Or set permanently
nvh config set defaults.prefer_nvidia true
```

## OOM Protection

nvHive checks if a model fits in your GPU VRAM before loading:

- **Fits in VRAM**: Full GPU acceleration, best performance
- **Partially fits**: GPU + CPU offload (slower layers on RAM)
- **Doesn't fit**: Warning with smaller model recommendation

```bash
# Check if a specific model will fit
nvh bench --model nemotron    # shows VRAM usage during benchmark
```

## How Routing Uses GPU Info

Once Ollama is running with a Nemotron model, nvHive's router:

1. Registers the local model as a provider
2. Scores it on capability per task type (conversation, Q&A, code, etc.)
3. Routes simple queries locally — free, private, no latency
4. Escalates complex queries to cloud when local quality isn't sufficient
5. The **adaptive learning loop** measures the local model's actual quality on your hardware and adjusts routing thresholds over time

With `--prefer-nvidia`, local NVIDIA providers get a 1.3x routing bonus, keeping more queries on your GPU.

## Community Baselines

### Measured: DGX Spark (NVIDIA GB10, 120GB unified memory)

| Model | Family | Size | tok/s | Measured |
|-------|--------|------|------:|:--------:|
| nemotron-mini | NVIDIA Nemotron | 2.7 GB | **86.6** | ✓ |
| gemma3 | Google Gemma 3 | 3.3 GB | **73.4** | ✓ |
| llama3.1 | Meta Llama 3.1 | 4.9 GB | **48.1** | ✓ |
| qwen3:8b | Qwen 3 | ~6 GB | estimate varies | pending |
| nemotron-3-super | NVIDIA Nemotron | 86 GB | **24.8** | ✓ |

### Estimated: Other NVIDIA GPUs

| GPU | VRAM | Expected Performance |
|-----|------|---------------------|
| RTX 3060 | 12 GB | qwen3:8b / MiniCPM-V tier |
| RTX 3080 | 10 GB | qwen3:8b / compact vision tier |
| RTX 3090 | 24 GB | ~90 tok/s with nemotron |
| RTX 4070 | 12 GB | qwen/coder + MiniCPM-V tier |
| RTX 4080 | 16 GB | qwen/coder + MiniCPM-V tier |
| RTX 4090 | 24 GB | ~140 tok/s with nemotron |
| A100 | 40/80 GB | ~250 tok/s with nemotron 70B |
| H100 | 80 GB | ~380 tok/s with nemotron 70B |

Run `nvh bench` to measure your actual performance and contribute to community baselines.

Apple Silicon (M1/M2/M3/M4) is also supported via Ollama's Metal backend, but without pynvml GPU detection details.
