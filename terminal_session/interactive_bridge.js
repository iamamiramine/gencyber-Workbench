'use strict';

/*
 * Interactive Agent Tools (IAT) bridge — port of EnIGMA / SWE-agent v0.7
 * `sweagent/agent/interactive_commands.py` + `_handle_interactive_commands`.
 *
 * The vendored bash tools in enigma_commands/ (debug.sh, server_connection.sh)
 * never touch a real debugger or socket themselves: each function only echoes a
 *
 *     <<INTERACTIVE||<inner command>||INTERACTIVE>>
 *
 * sentinel to the PTY. This module scrapes those sentinels out of a command's
 * output, then keeps a single long-lived REPL subprocess (gdb or the pwntools
 * `_connect.py` netcat shell) alive PER terminal session and feeds the inner
 * commands into it, reading back the real output until the REPL prompt returns.
 *
 * It deliberately does NOT register its child with IsolatedProcessManager: the
 * /api/execute busy-guard rejects new commands while an isolated proc is active,
 * which would block follow-up debug_ and connect_ calls. The REPL is managed
 * here instead, exactly as EnIGMA manages its own subprocess.Popen separate from
 * the main shell.
 */

const { spawn } = require('child_process');

const SENTINEL_RE = /<<INTERACTIVE\|\|([\s\S]*?)\|\|INTERACTIVE>>/;
const SESSION_RE = /SESSION=(.*)/;

// One config per interactive session kind. `cmdline` is [file, args]; the file
// is resolved via PATH (gdb) or given as an absolute script path (_connect.py).
// `prompt` is the trailing REPL prompt that signals the command finished.
const CONFIGS = {
  gdb: {
    cmdline: ['gdb', []],
    prompt: '(gdb)',
    startCommand: 'debug_start',
    stopCommand: 'debug_stop',
    quitInSession: ['quit'],
  },
  connect: {
    // `-u` forces unbuffered stdout so the (nc) prompt + server replies reach us
    // immediately instead of sitting in Python's block buffer.
    cmdline: ['python3', ['-u', __dirname + '/enigma_commands/_connect.py']],
    prompt: '(nc)',
    startCommand: 'connect_start',
    stopCommand: 'connect_stop',
    quitInSession: ['quit'],
  },
};

// Per-command read budget (ms) and how long to keep polling with no new output.
const COMMAND_TIMEOUT_MS = 25000;
const START_TIMEOUT_MS = 15000;
const POLL_INTERVAL_MS = 50;

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

/**
 * Extract the interactive session name and the ordered inner commands from a
 * block of observation text. Mirrors EnIGMA's get_interactive_commands: the
 * `SESSION=<name>` sentinel names the session and is not itself a command.
 *
 * @returns {{ sessionName: string|null, commands: string[] }}
 */
function getInteractiveCommands(output) {
  let sessionName = '';
  const commands = [];
  for (const line of String(output || '').split('\n')) {
    const m = line.match(SENTINEL_RE);
    if (!m) continue;
    const inner = m[1];
    const sm = inner.match(SESSION_RE);
    if (sm) {
      sessionName = sm[1].trim();
    } else {
      commands.push(inner);
    }
  }
  return { sessionName: sessionName || null, commands };
}

function hasSentinel(output) {
  return SENTINEL_RE.test(String(output || ''));
}

class InteractiveBridge {
  constructor() {
    /** @type {Map<string, object>} terminal sessionId -> active interactive session */
    this.sessions = new Map();
  }

  activeName(sessionId) {
    const s = this.sessions.get(sessionId);
    return s ? s.name : null;
  }

  _alreadyOpenMessage(session) {
    return (
      'Interactive session already open. Please close the current interactive ' +
      `session: ${session.name} with the command: \`${session.config.stopCommand}\``
    );
  }

  _isAlive(session) {
    return !!(session && session.child && session.child.exitCode === null && !session.child.killed);
  }

  _startSession(sessionId, name, config, cwd) {
    const [file, args] = config.cmdline;
    const child = spawn(file, args, {
      cwd: cwd || process.cwd(),
      stdio: ['pipe', 'pipe', 'pipe'],
      env: { ...process.env, PYTHONUNBUFFERED: '1' },
    });
    const session = { name, config, child, buf: '', exited: false, spawnError: null };
    child.stdout.on('data', (d) => { session.buf += d.toString(); });
    child.stderr.on('data', (d) => { session.buf += d.toString(); });
    child.on('exit', () => { session.exited = true; });
    child.on('error', (err) => {
      session.spawnError = String(err && err.message ? err.message : err);
      session.exited = true;
    });
    this.sessions.set(sessionId, session);
    return this._readUntilPrompt(session, START_TIMEOUT_MS, 0);
  }

  _stopSession(sessionId) {
    const session = this.sessions.get(sessionId);
    if (session && this._isAlive(session)) {
      try { session.child.kill('SIGTERM'); } catch (e) { /* ignore */ }
    }
    this.sessions.delete(sessionId);
  }

  killSession(sessionId) {
    this._stopSession(sessionId);
  }

  /**
   * Write a line to the REPL and read output until its prompt returns (or a
   * timeout fires). Returns the text produced since `sinceOffset`.
   */
  async _readUntilPrompt(session, timeoutMs, sinceOffset) {
    const since = sinceOffset == null ? session.buf.length : sinceOffset;
    const prompt = session.config.prompt;
    const start = Date.now();
    while (true) {
      const chunk = session.buf.slice(since);
      const trimmed = chunk.replace(/\s+$/, '');
      if (session.spawnError) {
        return chunk + `\n[interactive session failed to start: ${session.spawnError}]`;
      }
      if (trimmed.endsWith(prompt)) {
        return chunk;
      }
      if (session.exited) {
        return chunk;
      }
      if (Date.now() - start > timeoutMs) {
        try { session.child.kill('SIGINT'); } catch (e) { /* ignore */ }
        await sleep(200);
        return session.buf.slice(since) + '\nEXECUTION TIMED OUT';
      }
      await sleep(POLL_INTERVAL_MS);
    }
  }

  async _communicate(session, command) {
    const line = command.endsWith('\n') ? command : command + '\n';
    const before = session.buf.length;
    try {
      session.child.stdin.write(line);
    } catch (e) {
      return `\n[interactive write failed: ${e.message}]`;
    }
    // Quitting the REPL produces no useful prompt — let it wind down quietly.
    if (session.config.quitInSession.includes(command.trim())) {
      await sleep(300);
      return '';
    }
    return this._readUntilPrompt(session, COMMAND_TIMEOUT_MS, before);
  }

  /**
   * Process the interactive sentinels found in `observation` for one terminal
   * session. Faithful port of swe_env._handle_interactive_commands.
   *
   * @returns {Promise<{handled: boolean, observation?: string}>}
   */
  async handle(sessionId, observation, cwd) {
    const { sessionName, commands } = getInteractiveCommands(observation);
    if (!sessionName) {
      return { handled: false };
    }

    const existing = this.sessions.get(sessionId);
    if (existing && existing.name !== sessionName) {
      return { handled: true, observation: this._alreadyOpenMessage(existing) };
    }

    let out = '';
    for (const command of commands) {
      if (command === 'START') {
        const open = this.sessions.get(sessionId);
        if (open) {
          return { handled: true, observation: this._alreadyOpenMessage(open) };
        }
        const config = CONFIGS[sessionName];
        if (!config) {
          return { handled: true, observation: `Unknown interactive session: ${sessionName}` };
        }
        out += await this._startSession(sessionId, sessionName, config, cwd);
      } else if (command === 'STOP') {
        if (!this.sessions.get(sessionId)) {
          out = `Interactive session '${sessionName}' is not running, so it cannot be stopped!`;
        } else {
          this._stopSession(sessionId);
          out = `Interactive session '${sessionName}' stopped successfully`;
        }
      } else {
        const session = this.sessions.get(sessionId);
        const config = CONFIGS[sessionName];
        if (!session) {
          const start = config ? config.startCommand : `${sessionName}_start`;
          out = `Interactive session '${sessionName}' is not running! please start it first using \`${start}\``;
        } else if (!this._isAlive(session)) {
          const start = session.config.startCommand;
          this._stopSession(sessionId);
          out = `Interactive session '${sessionName}' was unexpectedly closed! Please start it again using \`${start}\``;
        } else {
          out += await this._communicate(session, command);
          out += '\n';
        }
      }
    }
    return { handled: true, observation: out || '(no interactive output)' };
  }
}

module.exports = { InteractiveBridge, getInteractiveCommands, hasSentinel, CONFIGS };
