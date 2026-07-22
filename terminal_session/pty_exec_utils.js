/*
 * Pure helpers for sentinel-based command execution in the PTY.
 *
 * Extracted from app.js so they can be unit-tested without express / node-pty.
 *
 * Every command is followed by a printf that emits a unique marker carrying the
 * real exit code:
 *
 *   <command>
 *   printf '\n__GC_DONE_<token>__%d__\n' "$?"
 *
 * The "DONE" token is written as DO''NE so the echoed input line never matches
 * the expanded marker - only the real printf output does. Completion is then
 * deterministic and we recover the true exit code.
 */

'use strict';

function stripAnsi(s) {
  if (!s) return '';
  return String(s)
    .replace(/\x1b\[[0-9;?]*[ -\/]*[@-~]/g, '')
    .replace(/\x1b\][^\x07]*(\x07|\x1b\\)/g, '')
    .replace(/\r/g, '');
}

function sentinelRegex(token) {
  return new RegExp('__GC_DONE_' + token + '__(-?\\d+)__');
}

function buildSentinelPayload(command, token) {
  // Single-quoted printf format; DO''NE keeps the echoed line from matching.
  const sentinel =
    "printf '\\n__GC_DO''NE_" + token + "__%d__\\n' \"$?\"\n";
  return command + '\n' + sentinel;
}

// Remove the sentinel marker, the echoed input/printf lines, and trailing
// prompt noise. Returns { text, exitCode }.
function stripSentinelAndNoise(raw, token) {
  let exitCode = null;
  let text = raw || '';
  if (token) {
    const m = text.match(sentinelRegex(token));
    if (m) {
      exitCode = parseInt(m[1], 10);
      text = text.slice(0, m.index);
    }
  }
  let lines = text.split('\n').filter((l) => {
    if (token && l.indexOf(token) !== -1) return false; // echoed printf line
    const clean = stripAnsi(l).replace(/^\s+/, '');
    if (/^\[[^\]]*\][#$]/.test(clean)) return false; // echoed prompt + input
    return true;
  });
  while (
    lines.length &&
    /^(\[[^\]]*\][#$]\s*)?$/.test(stripAnsi(lines[lines.length - 1]).trim())
  ) {
    lines.pop();
  }
  return { text: lines.join('\n'), exitCode };
}

/** True if this line should not appear in the human terminal UI. */
function isDisplayNoiseLine(line) {
  if (!line) return false;
  const clean = stripAnsi(line).replace(/^\s+/, '').trim();
  if (!clean) return false;
  if (/^__GC_DONE_[a-f0-9]+__-?\d+__\s*$/i.test(clean)) return true;
  if (/__GC_DO''NE_|__GC_DONE_[a-f0-9]+/i.test(clean)) return true;
  if (/printf\s+'?\\n__GC_DO/i.test(clean)) return true;
  if (/^command -v \S+/.test(clean) && /echo\s+"[^"]+=[01]"/.test(clean)) return true;
  return false;
}

/**
 * Line-buffered filter for live PTY chunks (socket/UI). Drops sentinel printf
 * echoes and completion markers while preserving real command output.
 */
function filterDisplayChunk(carry, chunk) {
  let buf = (carry || '') + (chunk || '');
  const parts = buf.split('\n');
  const nextCarry = parts.pop() ?? '';
  let out = '';
  for (const line of parts) {
    if (isDisplayNoiseLine(line)) continue;
    out += line + '\n';
  }
  return { carry: nextCarry, text: out };
}

module.exports = {
  stripAnsi,
  sentinelRegex,
  buildSentinelPayload,
  stripSentinelAndNoise,
  isDisplayNoiseLine,
  filterDisplayChunk,
};
