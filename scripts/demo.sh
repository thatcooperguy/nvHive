#!/bin/bash
# NVHive Demo Script
# Run this to showcase key features. Uses mock provider for demo reliability.

set -euo pipefail

G='\033[0;32m'; C='\033[0;36m'; Y='\033[1;33m'; N='\033[0m'; B='\033[1m'

pause() { echo ""; read -p "  Press Enter to continue..." _; echo ""; }
title() { echo -e "\n${B}${C}=== $1 ===${N}\n"; }

echo -e "${G}${B}"
echo "╔══════════════════════════════════════════════╗"
echo "║         NVHive — Live Demo                   ║"
echo "╚══════════════════════════════════════════════╝"
echo -e "${N}"

title "1. System Status"
nvh status 2>/dev/null || echo "(status requires initialized environment)"
pause

title "2. GPU Detection"
nvh doctor 2>/dev/null | head -20 || echo "(doctor requires GPU)"
pause

title "3. Model Catalog"
nvh model list 2>/dev/null | head -25
pause

title "4. Agent Presets / Cabinets"
nvh agent presets
pause

title "5. Action Detection (system commands just work)"
echo -e "${Y}These would execute system actions directly:${N}"
python3 -c "
from nvh.core.action_detector import detect_action
tests = ['install pandas', 'open firefox', 'open google.com',
         'what processes are running', 'find large files',
         'how much disk space', 'kill python', 'show system info',
         'what is machine learning', 'explain CUDA cores']
for t in tests:
    a = detect_action(t)
    if a:
        c = ' [CONFIRM]' if a.requires_confirm else ''
        print(f'  ACTION: \"{t}\" -> {a.tool_name}({a.arguments}){c}')
    else:
        print(f'  QUESTION: \"{t}\" -> routes to LLM')
"
pause

title "6. Available Tools (27 total)"
python3 -c "
from nvh.core.tools import ToolRegistry
t = ToolRegistry()
safe = [x for x in t.list_tools() if x.safe]
unsafe = [x for x in t.list_tools() if not x.safe]
print(f'Safe ({len(safe)}):')
for tool in safe: print(f'  + {tool.name}: {tool.description[:60]}')
print(f'\nUnsafe ({len(unsafe)}) -- require confirmation:')
for tool in unsafe: print(f'  ! {tool.name}: {tool.description[:60]}')
"
pause

title "7. Advisor Profiles"
nvh advisor info groq 2>/dev/null || echo "(requires config)"
pause

title "8. Template Library"
nvh template list 2>/dev/null
pause

title "9. All CLI Commands"
nvh --help 2>/dev/null
pause

echo -e "${G}${B}"
echo "╔══════════════════════════════════════════════╗"
echo "║         Demo Complete!                       ║"
echo "╚══════════════════════════════════════════════╝"
echo -e "${N}"
echo "To try it yourself:"
echo "  nvh                    # interactive chat"
echo "  nvh \"your question\"    # quick answer"
echo "  nvh do \"complex task\"  # autonomous agent"
echo ""
