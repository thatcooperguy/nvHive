# VHS tape file — records the nvHive first-run setup experience as a GIF
#
# Install VHS: go install github.com/charmbracelet/vhs@latest
# Record:      vhs scripts/demo-setup.tape
# Output:      docs/screenshots/terminal-demo.gif
#
# Prerequisites:
#   - Clean environment (no ~/.hive/config.yaml)
#   - NVIDIA GPU available
#   - Internet connection for Ollama + model downloads

Output docs/screenshots/terminal-demo.gif
Set FontSize 14
Set Width 960
Set Height 720
Set Theme "Dracula"
Set TypingSpeed 40ms
Set Padding 20

# Install
Type "pip install 'nvhive[vision]'"
Enter
Sleep 3s

# Launch — triggers first-run setup
Type "nvh"
Enter
Sleep 2s

# Step 1/3: Hardware + Local AI
# GPU detection happens automatically
Sleep 3s

# Ollama download prompt
Type "Y"
Enter
Sleep 5s

# Wait for Ollama download (fast-forward in post)
Sleep 10s

# Model pull prompt
Type "Y"
Enter
Sleep 5s

# Wait for model pulls
Sleep 15s

# Step 2/3: Provider status shows automatically
Sleep 3s

# Step 3/3: API key setup
# Open Groq in browser
Type "Y"
Enter
Sleep 5s

# Clipboard detection happens (user copies key in browser)
Sleep 10s

# Skip remaining providers for demo
Enter
Sleep 1s
Enter
Sleep 1s
Enter
Sleep 2s

# Setup complete — now in REPL
Sleep 3s

# Demo: natural language question
Type "what GPU do I have?"
Enter
Sleep 5s

# Demo: desktop agent
Type "take a screenshot and describe what you see"
Enter
Sleep 10s

# Demo: natural language command
Type "use anthropic"
Enter
Sleep 2s

# Demo: action
Type "open firefox"
Enter
Sleep 3s

# Demo: task
Type "setup comfyui"
Enter
Sleep 15s

# Exit
Type "/quit"
Enter
Sleep 2s
