# EnIGMA Interactive Agent Tools — sourced into the workbench PTY so the
# debug_*/connect_* tool functions become available as plain shell commands.
#
# debug.sh and server_connection.sh are vendored verbatim from SWE-agent v0.7
# (EnIGMA). Each function only echoes a <<INTERACTIVE||..||INTERACTIVE>> sentinel;
# the workbench interactive bridge (interactive_bridge.js) parses those sentinels
# and drives a managed gdb / netcat REPL subprocess running alongside this shell.
_GC_ENIGMA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$_GC_ENIGMA_DIR/debug.sh"
source "$_GC_ENIGMA_DIR/server_connection.sh"
