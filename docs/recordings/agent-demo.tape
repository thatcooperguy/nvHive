# nvh agent demo — record with: vhs docs/recordings/agent-demo.tape
#
# Install vhs: go install github.com/charmbracelet/vhs@latest
# Or: scoop install vhs / brew install vhs / choco install vhs
#
# This produces agent-demo.gif in the current directory.

Output docs/screenshots/agent-demo.gif
Set Shell "bash"
Set FontSize 14
Set Width 900
Set Height 500
Set Theme "Dracula"
Set Padding 20

# Title
Type "# nvHive Agentic Coding — plan, execute, verify"
Enter
Sleep 1s

# Setup
Type "nvh agent --setup"
Enter
Sleep 8s

# Run a task
Type 'nvh agent "Add a health check endpoint that returns GPU status"'
Enter
Sleep 30s

# Show results
Type "nvh review"
Enter
Sleep 15s
