'use strict';

/**
 * Mirror of gencyber-Agent interactive_command_helper heuristics.
 * Used to route commands to isolated subprocesses vs sentinel PTY.
 */

// Kept for backwards-compatible export; the classification now anchors these to a
// command position (see below) rather than matching them anywhere in the string.
const INTERACTIVE_PROGRAMS =
  /\b(ssh|ftp|sftp|telnet|mysql|psql|sqlite3|sudo|su|passwd|login|python3?\s*$|python3?\s+-i|bash\s+-i|sh\s+-i|nc\s+-l|ncat\s+-l)\b/i;

const NONINTERACTIVE_MARKERS =
  /sshpass|expect\b|BatchMode=yes|-n\s|-N\s|-f\s|<\s|<<|-oBatchMode|-oStrictHostKeyChecking|-i\s+\S|mysql\s+-p\S|psql\s+.*PGPASSWORD/i;

// Program NAMES that open an interactive REPL/prompt when INVOKED. Matched only
// when the program is the command being run (argv[0] of the command or of a
// pipeline/;/&&/|| segment) — NOT when the word merely appears as an argument or
// path. This is the fix for false positives like `cat /etc/passwd`, `grep login`,
// or `strings bin | grep su`, which previously matched `passwd`/`login`/`su` as a
// substring and got misrouted to an isolated subprocess (busy-rejecting concurrent
// commands with empty output).
const INTERACTIVE_CMD_NAMES = new Set([
  'ssh', 'ftp', 'sftp', 'telnet', 'mysql', 'psql', 'sqlite3',
  'sudo', 'su', 'passwd', 'login', 'mongo', 'redis-cli',
]);

// Multi-token interactive invocations, tested against a segment's head.
const INTERACTIVE_SEGMENT_PATTERNS = [
  /^python3?\s*$/i, // bare python REPL
  /^python3?\s+-i\b/i, // python -i
  /^(bash|sh)\s+-i\b/i, // interactive shell
  /^n(c|cat)\b.*\s-l\b/i, // netcat/ncat listener
];

// Split a command line into segments at shell separators / subshell openers and
// return each segment's head (the program invoked in that segment), with any
// leading `VAR=val` environment assignments stripped.
function _segmentHeads(command) {
  return String(command)
    .split(/\|\||&&|[|;&\n]|\$\(|`/)
    .map((s) => s.trim())
    .filter(Boolean)
    .map((seg) => seg.replace(/^(\s*[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+/, '').trim());
}

function isInteractiveCommand(command) {
  if (!command || !String(command).trim()) return false;
  const c = String(command).trim();
  if (NONINTERACTIVE_MARKERS.test(c)) return false;
  for (const head of _segmentHeads(c)) {
    const prog = (head.split(/\s+/)[0] || '').split('/').pop().toLowerCase();
    if (INTERACTIVE_CMD_NAMES.has(prog)) return true;
    if (INTERACTIVE_SEGMENT_PATTERNS.some((re) => re.test(head))) return true;
  }
  return false;
}

// Raw interactive debuggers / disassemblers. Launched bare, each opens its own
// REPL (the `(gdb)` prompt) and starts consuming stdin — so the trailing sentinel
// `printf '\n__GC_DONE_<tok>__%d__\n' "$?"` that the PTY appends to every command is
// fed INTO the debugger as one of ITS commands (gdb then errors with
// "Bad format string, missing '"'." because its own printf wants double quotes),
// the completion marker never reaches stdout, and the session wedges forever.
// EnIGMA solves this by wrapping gdb behind Interactive Agent Tools (debug_start/…);
// we reject the raw launch and steer the agent to those tools instead.
const RAW_DEBUGGERS = /\b(gdb|gdbserver|radare2|r2)\b/i;

// Explicitly non-interactive launches run to completion and exit, so they are safe
// in the sentinel PTY and must NOT be rejected: gdb/gdbserver batch modes, and
// radare2/r2 quit-after-command modes (`-q`/`-qq`, typically with `-c`).
const DEBUGGER_BATCH_MARKERS = /--?batch(-silent)?\b/i;
const R2_QUIET_MARKERS = /(^|\s)-qq?\b/i;

function isRawInteractiveDebugger(command) {
  if (!command || !String(command).trim()) return false;
  const c = String(command).trim();
  const m = c.match(RAW_DEBUGGERS);
  if (!m) return false;
  const tool = m[1].toLowerCase();
  if (tool === 'gdb' || tool === 'gdbserver') {
    return !DEBUGGER_BATCH_MARKERS.test(c);
  }
  // radare2 / r2
  return !R2_QUIET_MARKERS.test(c);
}

module.exports = {
  isInteractiveCommand,
  isRawInteractiveDebugger,
  INTERACTIVE_PROGRAMS,
  NONINTERACTIVE_MARKERS,
  RAW_DEBUGGERS,
};
