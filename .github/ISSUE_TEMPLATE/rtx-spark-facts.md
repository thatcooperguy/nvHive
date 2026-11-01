---
name: RTX Spark hardware facts
about: Report what an RTX Spark (Windows on Arm) machine shows, so Windows on Arm support can be planned from facts rather than guesses
title: "RTX Spark hardware facts: <make and model>"
labels: ["enhancement"]
---

<!--
nvHive is untested on Windows on Arm and publishes no windows-arm64 binary.
This template collects the facts maintainers need before any Windows on Arm
work starts. Paste raw command output rather than a summary, and remove
serial numbers or anything else you consider private before posting.
-->

## Machine

- Make and model:
- Windows edition and build (`winver`, or in PowerShell `[System.Environment]::OSVersion.Version`):
- Python version and architecture, from `python -c "import platform, sys; print(sys.version); print(platform.machine())"`:
- Was that Python installed as native arm64, or as x64 running under emulation?

## `pip install nvhive`

- Did `pip install nvhive` complete with native arm64 Python, without installing a compiler or Rust toolchain? If not, paste the failing package name and the last 30 lines of output.
- Did it complete with x64 Python under emulation?
- Output of `nvh version` (or the error it printed):

```text
paste here
```

## GPU

Output of `nvidia-smi -q` (the full report, not the summary table):

```text
paste here
```

NVML device name and memory. nvHive reads the GPU through the optional NVML
bindings when they are installed (`pip install nvhive[nvidia]`, which pulls
the `nvidia-ml-py3` binding) and falls back to `nvidia-smi` otherwise. Output
of:

```text
python -c "import pynvml; pynvml.nvmlInit(); h = pynvml.nvmlDeviceGetHandleByIndex(0); print(pynvml.nvmlDeviceGetName(h)); print(pynvml.nvmlDeviceGetMemoryInfo(h).total)"
```

```text
paste here
```

Output of `Get-CimInstance Win32_VideoController | Format-List Name, AdapterRAM, DriverVersion, VideoProcessor` in PowerShell:

```text
paste here
```

## Ollama

- Ollama build (`ollama --version`), and whether it came from the Windows installer or a manual install:
- Output of `ollama ps` after loading one model (`ollama run <model>` in one terminal, `ollama ps` in another):

```text
paste here
```

## Anything else

Driver versions, how unified memory is reported, thermal or power behaviour,
and anything that worked or failed that is not covered above.
