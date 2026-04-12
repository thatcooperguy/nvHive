# Deploying nvHive Without Root Access

This guide covers installing and running nvHive on a server where you
don't have root/sudo — common on shared workstations, cloud VMs managed
by IT, and enterprise NVIDIA DGX/HGX systems.

## Quick Start

```bash
# 1. Create a virtual environment (no root needed)
python3 -m venv ~/nvhive-env
source ~/nvhive-env/bin/activate

# 2. Install nvhive
pip install nvhive

# 3. Set API keys via environment variables (not keyring)
export GROQ_API_KEY=gsk_...          # free, no signup needed for basic tier
export OPENAI_API_KEY=sk-...         # optional, for cloud orchestrator

# 4. Verify
nvh health
nvh "Hello from nvHive"
```

## GPU Access (RTX 6000 Pro BSE / DGX Spark)

The NVIDIA driver must be installed system-wide (your admin handles
this). Once the driver is present, nvHive reads GPU info via pynvml
— no root needed.

```bash
# Verify GPU is accessible
nvidia-smi                           # should show your GPU
nvh nvidia                           # nvHive's GPU detection
```

## Ollama Without Root

The standard Ollama installer (`curl | sh`) needs root. User-space
alternative:

```bash
# Download the binary to your home directory
mkdir -p ~/bin
curl -L https://ollama.com/download/ollama-linux-amd64 -o ~/bin/ollama
chmod +x ~/bin/ollama

# Add to PATH
echo 'export PATH="$HOME/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc

# Start Ollama (runs in user space, stores models in ~/.ollama)
ollama serve &

# Pull models for your GPU tier
# RTX 6000 Pro BSE (96 GB) = Tier 4:
ollama pull llama3.3:70b             # ~40 GB, main coder
ollama pull qwen2.5-coder:32b       # ~34 GB, reviewer

# Or use nvh agent --setup (does the same thing)
nvh agent --setup
```

Models are stored in `~/.ollama/models/` — no root needed.

## API Keys Without Keyring

On headless servers (no desktop session), the Python `keyring` module
can't store secrets because there's no gnome-keyring or kwallet. nvHive
handles this gracefully — keyring failures are caught silently — but
keys saved via `nvh setup` won't persist across reboots.

**Recommended: use environment variables instead.**

```bash
# Add to ~/.bashrc or ~/.profile
export GROQ_API_KEY=gsk_...
export OPENAI_API_KEY=sk-...
export GITHUB_TOKEN=ghp_...
export GOOGLE_API_KEY=AI...

# Or use a .env file in your project
echo "GROQ_API_KEY=gsk_..." >> .env
echo "OPENAI_API_KEY=sk-..." >> .env
```

nvHive auto-loads `.env` files in the current directory via
`python-dotenv`.

**Alternative: file-based keyring backend.**

```bash
pip install keyrings.alt
export PYTHON_KEYRING_BACKEND=keyrings.alt.file.PlaintextKeyring
# Now `nvh setup` will persist keys to ~/.local/share/python_keyring/
```

Note: PlaintextKeyring stores keys unencrypted. Use file permissions
(`chmod 600`) to protect the keyring file.

## WebUI Without Root

The webui needs Node.js. If it's not installed system-wide, use nvm:

```bash
# Install nvm (user space, no root)
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
source ~/.bashrc

# Install Node.js
nvm install 20

# Start the webui
nvh webui --port 3000
```

The webui binds to port 3000 by default (unprivileged). Access it at
`http://<server-ip>:3000`. Port 80 requires root and is skipped
automatically.

## Running the API Server

```bash
# Start the API server (background)
nvh serve --port 8000 &

# Or with nohup for persistence
nohup nvh serve --port 8000 > ~/nvhive-api.log 2>&1 &
```

The API server binds to `127.0.0.1:8000` by default. To expose it
on the network:

```bash
nvh serve --host 0.0.0.0 --port 8000
```

## Agentic Coding on Tier 4

With the RTX 6000 Pro BSE (96 GB), you're on Tier 4 — dual-model mode
with Llama 70B coder + Qwen 32B reviewer, both running locally:

```bash
# One-time setup
nvh agent --setup

# Run a coding task
nvh agent "Add retry logic to the API client" --dir ~/myproject

# Code review with multi-model cross-verification
nvh review --mode multi

# Generate tests
nvh test-gen src/api/client.py
```

Both models fit in 96 GB (40 GB + 34 GB = 74 GB, leaving 22 GB for
KV cache). The cloud orchestrator handles planning and verification
with minimal token usage (~$0.01-0.03 per task).

## Systemd User Service (No Root)

Run nvHive as a persistent service without root using systemd user
mode:

```bash
mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/nvhive.service << 'EOF'
[Unit]
Description=nvHive API Server
After=network.target

[Service]
Type=simple
ExecStart=%h/nvhive-env/bin/nvh serve --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=5
Environment=GROQ_API_KEY=gsk_...
Environment=OPENAI_API_KEY=sk-...

[Install]
WantedBy=default.target
EOF

# Enable and start
systemctl --user daemon-reload
systemctl --user enable nvhive
systemctl --user start nvhive

# Check status
systemctl --user status nvhive

# Enable lingering so the service survives logout
loginctl enable-linger $USER
```

## Troubleshooting

**`ModuleNotFoundError: No module named 'nvh'`**
→ Make sure the venv is activated: `source ~/nvhive-env/bin/activate`

**`pynvml.NVMLError: Driver not loaded`**
→ NVIDIA driver not installed. Ask your admin to install it.

**`keyring.errors.NoKeyringError`**
→ Headless server without keyring backend. Use env vars instead.

**`OSError: [Errno 13] Permission denied: '/etc/hosts'`**
→ Normal on no-root systems. nvHive skips the hostname setup and uses
`localhost` instead. No functionality is affected.

**`OSError: [Errno 98] Address already in use`**
→ Another process is on that port. Try `nvh serve --port 8001` or
`nvh webui --port 3001`.

**Ollama `connection refused`**
→ Ollama isn't running. Start it: `~/bin/ollama serve &`
