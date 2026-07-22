/* Interactive shell with persistent sessions */

/*
* This program creates persistent terminal sessions that can be shared
* across multiple socket connections and survive client disconnections.
*/

var express = require('express')
const pty = require('node-pty');
var app = express();
var fs = require('fs');
var path = require('path');
var crypto = require('crypto');
var bodyParser = require('body-parser');
var http = require('http').createServer(app);
var io = require('socket.io')(http, {
  cors: {
    origin: "*",
    methods: ["GET", "POST"],
    allowedHeaders: ["*"],
    credentials: true
  },
  transports: ['polling', 'websocket'],
  allowEIO3: true,
  pingTimeout: 30000,
  pingInterval: 10000,
  cookie: false,
  serveClient: true,
  path: '/socket.io/',
  upgradeTimeout: 30000,
  maxHttpBufferSize: 1e6
});

app.use(bodyParser.json());
app.use(bodyParser.urlencoded({
  extended: true
}));
app.use(express.static(__dirname + '/public'));

// Enable CORS for all routes
app.use((req, res, next) => {
  res.header('Access-Control-Allow-Origin', '*');
  res.header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS');
  res.header('Access-Control-Allow-Headers', 'Origin, X-Requested-With, Content-Type, Accept, Authorization');
  if (req.method === 'OPTIONS') {
    res.sendStatus(200);
  } else {
    next();
  }
});

var startDir = false;
if (process.argv.length >= 3) {
  startDir = process.argv[2];
}

// Global session storage
const terminalSessions = new Map();
const sessionClients = new Map(); // Track clients per session

const { IsolatedProcessManager } = require('./isolated_process_manager');
const { isInteractiveCommand, isRawInteractiveDebugger } = require('./interactive_heuristic');
const { InteractiveBridge, hasSentinel } = require('./interactive_bridge');
const isolatedManager = new IsolatedProcessManager();
// EnIGMA IAT bridge: routes <<INTERACTIVE||..||>> sentinels (from the vendored
// debug_*/connect_* tools) to managed gdb / netcat REPLs per terminal session.
const interactiveBridge = new InteractiveBridge();

function isolatedExecEnabled() {
  const v = (process.env.GENCYBER_ISOLATED_EXEC || '1').trim().toLowerCase();
  return v !== '0' && v !== 'false' && v !== 'no';
}

// Create default session on startup
function createDefaultSession() {
  const defaultSessionId = 'default';
  if (!terminalSessions.has(defaultSessionId)) {
    getOrCreateSession(defaultSessionId);
  }
}

// Clean up old sessions periodically
setInterval(() => {
  cleanupOldSessions();
}, 60000); // Every minute

function cleanupOldSessions() {
  const now = Date.now();
  const maxAge = 30 * 60 * 1000; // 30 minutes

  for (const [sessionId, session] of terminalSessions.entries()) {
    const clientCount = sessionClients.get(sessionId)?.size || 0;
    const lastActivity = session.lastActivity || session.createdAt;
    
    // Don't cleanup the default session or sessions with active clients
    if (sessionId === 'default') continue;
    
    // Only cleanup sessions with no clients and old activity
    if (clientCount === 0 && (now - lastActivity) > maxAge) {
      destroySession(sessionId);
    }
  }
}

function destroySession(sessionId) {
  isolatedManager.killSessionProcs(sessionId);
  interactiveBridge.killSession(sessionId);
  const session = terminalSessions.get(sessionId);
  if (session && session.terminal) {
    try {
      session.terminal.kill('SIGTERM');
    } catch (e) {
      // Ignore errors when killing terminal
    }
  }
  terminalSessions.delete(sessionId);
  sessionClients.delete(sessionId);
}

// Create or get existing terminal session
function getOrCreateSession(sessionId, cwd = null) {
  if (terminalSessions.has(sessionId)) {
    const session = terminalSessions.get(sessionId);
    session.lastActivity = Date.now();
    return session;
  }
  
  // Determine working directory
  const workingDir = cwd || startDir || process.cwd();
  
  // Create new pseudo-terminal process
  const terminal = pty.spawn('/bin/bash', [], {
    name: 'xterm-256color',
    cols: 120,
    rows: 30,
    cwd: workingDir,
    handleFlowControl: true,
    env: { 
      ...process.env, 
      TERM: 'xterm-256color',
      LANG: 'en_US.UTF-8',
      SHELL: '/bin/bash',
      USER: process.env.USER || 'root',
      HOME: process.env.HOME || '/root',
      LC_ALL: 'en_US.UTF-8',
      PS1: '[\\u@\\h \\W]\\$ ',
      DEBIAN_FRONTEND: 'noninteractive'
    }
  });

  const session = {
    id: sessionId,
    terminal: terminal,
    createdAt: Date.now(),
    lastActivity: Date.now(),
    outputHistory: [],
    workingDir: workingDir,
    // Job tracking for sentinel-based completion + background commands.
    jobs: new Map(),
    activeJob: null,
    displayCarry: ''
  };

  // Set up terminal event handlers
  terminal.on('data', (data) => {
    session.outputHistory.push({
      type: 'stdout',
      data: data,
      timestamp: Date.now()
    });
    session.lastActivity = Date.now();

    const filtered = filterDisplayChunk(session.displayCarry || '', data.toString());
    session.displayCarry = filtered.carry;
    if (filtered.text) {
      broadcastToSession(sessionId, 'terminal-output-stdout', { message: filtered.text });
    }
  });

  terminal.on('exit', (code, signal) => {
    session.outputHistory.push({
      type: 'exit',
      data: `Process exited with code ${code}`,
      timestamp: Date.now()
    });
    
    // Broadcast exit to all clients
    broadcastToSession(sessionId, 'terminal-exit-code', { message: code || 1337 });
    
    // Restart the session if it's the default session or has active clients
    if (sessionId === 'default' || (sessionClients.get(sessionId)?.size || 0) > 0) {
      setTimeout(() => {
        const newSession = getOrCreateSession(sessionId, workingDir);
        broadcastToSession(sessionId, 'terminal-output-stdout', { 
          message: '\n--- Terminal session restarted ---\n' 
        });
      }, 1000);
    } else {
      // Clean up the session
      session.terminal = null;
    }
  });

  terminal.on('error', (error) => {
    broadcastToSession(sessionId, 'terminal-output-stderr', { 
      message: `Terminal error: ${error.message}\n` 
    });
  });

  // Keep limited history (last 1000 entries)
  setInterval(() => {
    if (session.outputHistory.length > 1000) {
      session.outputHistory = session.outputHistory.slice(-500);
    }
  }, 30000);

  // Send initial prompt + make the EnIGMA IAT tool functions (debug_*/connect_*)
  // available in this persistent shell. Sourced from an absolute path so it works
  // in Docker and local dev alike; missing file is harmless.
  const enigmaInit = path.join(__dirname, 'enigma_commands', 'init.sh');
  setTimeout(() => {
    if (terminal && !terminal.killed) {
      terminal.write(`source ${enigmaInit} 2>/dev/null\n`);
    }
  }, 500);

  terminalSessions.set(sessionId, session);
  return session;
}

function broadcastToSession(sessionId, event, data) {
  const clients = sessionClients.get(sessionId);
  if (clients) {
    clients.forEach(socket => {
      try {
        if (socket.connected) {
          socket.emit(event, data);
        }
      } catch (error) {
        // Remove dead clients
        clients.delete(socket);
      }
    });
  }
}

function addClientToSession(sessionId, socket) {
  if (!sessionClients.has(sessionId)) {
    sessionClients.set(sessionId, new Set());
  }
  sessionClients.get(sessionId).add(socket);
}

function removeClientFromSession(sessionId, socket) {
  const clients = sessionClients.get(sessionId);
  if (clients) {
    clients.delete(socket);
  }
}

/*
 * ---------------------------------------------------------------------------
 * Sentinel-based command execution + background job tracking.
 *
 * Instead of guessing completion from an idle timer, every command is followed
 * by a printf that emits a unique marker carrying the real exit code:
 *
 *   <command>
 *   printf '\n__GC_DONE_<token>__%d__\n' "$?"
 *
 * The "DONE" token is written as DO''NE so the echoed input line never matches
 * the expanded marker, only the real printf output does. Completion is then
 * deterministic and we recover the true exit code. If the marker is not seen
 * within the timeout, the command keeps running in the shared PTY and is
 * tracked as a background job that can be polled / signalled.
 * ---------------------------------------------------------------------------
 */

const MAX_JOBS_PER_SESSION = 30;

const {
  stripAnsi,
  sentinelRegex,
  buildSentinelPayload,
  stripSentinelAndNoise,
  filterDisplayChunk,
} = require('./pty_exec_utils');

// Idempotently mark a job complete once its sentinel has arrived.
function finalizeJob(session, job) {
  if (!job || !job.running) return;
  const m = (job.raw || '').match(sentinelRegex(job.token));
  if (!m) return;
  job.exitCode = parseInt(m[1], 10);
  job.running = false;
  job.endTime = Date.now();
  if (job.listener) {
    try { session.terminal.removeListener('data', job.listener); } catch (e) {}
  }
  if (session.activeJob === job) session.activeJob = null;
}

function autoReclaimEnabled() {
  return (process.env.GENCYBER_PTY_AUTORECLAIM || '1').trim().toLowerCase() !== '0';
}

// Reclaim the single shared PTY when a prior foreground command wedged it (a bare
// interactive command — `nc host port`, bare `gdb`, foreground `make`, an `apt`
// prompt, `ssh`, or a python `p.interactive()` — that never returns its completion
// sentinel). Without this, the job stays `running` forever and EVERY later
// /api/execute busy-rejects, so the agent burns its whole budget looping
// kill/pkill/signal (the "command already running" death spiral seen across the
// pwn/crypto/misc traces). In this one-command-per-turn agent, a NEW command means
// "stop waiting on the last one," so we interrupt the stuck job and hand the PTY
// back: send SIGINT (Ctrl-C), settle, then escalate to SIGQUIT/EOF; finally
// force-detach so the terminal is reusable. Returns true if the PTY is free after.
async function reclaimForegroundJob(session) {
  const job = session.activeJob;
  if (!job || !job.running) return true;
  const term = session.terminal;
  if (!term || term.killed) return false;
  const settle = (ms) => new Promise((r) => setTimeout(r, ms));
  // SIGINT only, repeated. Do NOT send EOF (^D closes bash) or SIGQUIT (can core-dump
  // the shell) — either would kill the persistent PTY and break every later command.
  // Ctrl-C interrupts the foreground child (nc/cat/make/apt/sleep/python p.interactive),
  // the shell returns to its prompt, and the command's queued completion printf then
  // fires so finalizeJob can confirm the exit.
  for (let i = 0; i < 3 && job.running; i++) {
    try { term.write('\x03'); } catch (e) {}
    await settle(450);
    finalizeJob(session, job);
  }
  if (job.running) {
    // Still wedged after repeated SIGINT — force-mark done and detach so the PTY is
    // reusable; the foreground child (if any survives) is left, but the shell prompt is back.
    job.running = false;
    job.timedOut = true;
    job.endTime = Date.now();
    if (job.listener) { try { term.removeListener('data', job.listener); } catch (e) {} }
  }
  if (session.activeJob === job) session.activeJob = null;
  console.log(`[reclaim] freed wedged PTY for session ${session.id} (job ${job.id}: ${String(job.command).slice(0, 60)})`);
  return !(session.activeJob && session.activeJob.running);
}

function pruneJobs(session) {
  if (session.jobs.size <= MAX_JOBS_PER_SESSION) return;
  const ids = Array.from(session.jobs.keys());
  const remove = ids.slice(0, ids.length - MAX_JOBS_PER_SESSION);
  for (const id of remove) {
    const j = session.jobs.get(id);
    if (j && !j.running) session.jobs.delete(id);
  }
}

// Run a command in the session PTY, resolving when the sentinel arrives or the
// timeout elapses. On timeout the job keeps running in the background.
function runCommandInSession(session, command, timeoutSec) {
  return new Promise((resolve) => {
    const token = crypto.randomBytes(8).toString('hex');
    const job = {
      id: crypto.randomBytes(6).toString('hex'),
      command: command,
      token: token,
      raw: '',
      exitCode: null,
      running: true,
      timedOut: false,
      startTime: Date.now(),
      endTime: null,
      listener: null
    };
    session.jobs.set(job.id, job);
    session.activeJob = job;
    pruneJobs(session);

    let resolved = false;
    let overall = null;
    const done = () => {
      if (resolved) return;
      resolved = true;
      if (overall) clearTimeout(overall);
      if (job.running) job.timedOut = true; // resolved due to timeout
      resolve(job);
    };

    job.listener = (data) => {
      job.raw += data.toString();
      finalizeJob(session, job);
      if (!job.running) done();
    };
    session.terminal.on('data', job.listener);

    try {
      session.terminal.write(buildSentinelPayload(command, token));
      session.lastActivity = Date.now();
    } catch (e) {
      job.running = false;
      job.exitCode = -1;
      job.raw += `\n[terminal write error] ${e.message}\n`;
      done();
      return;
    }

    overall = setTimeout(done, Math.max(1, timeoutSec) * 1000);
  });
}

// Capture output for a short settle window after writing to the PTY.
function captureWindow(session, settleMs) {
  return new Promise((resolve) => {
    let buf = '';
    const lstn = (d) => { buf += d.toString(); };
    session.terminal.on('data', lstn);
    setTimeout(() => {
      try { session.terminal.removeListener('data', lstn); } catch (e) {}
      resolve(buf);
    }, Math.min(8000, Math.max(0, settleMs)));
  });
}

// Per-session serialization for foreground command execution.
//
// The agent's DeepAgents loop and its category specialists all share ONE PTY
// session and can POST /api/execute concurrently. Without serialization only the
// first command acquired the PTY; every concurrent sibling hit the "a command is
// already running" busy-guard and came back with EMPTY stdout — which the agent
// read as "the command produced no output" and re-ran, blind. (Large-output
// commands like `strings`/`objdump` held the PTY longer, so they collided far
// more often — matching the observed ~48% empty rate, worst on big output.)
//
// Chaining each execution after the previous one lets every command run on the
// PTY in turn and get its real output. The chain is bounded by each command's own
// timeout (runCommandInSession resolves and backgrounds a still-running job on
// timeout), so a genuinely hung command cannot wedge the queue: the next waiter
// then sees a backgrounded activeJob and gets the normal busy response.
function withSessionExecLock(session, fn) {
  const prev = session._execChain || Promise.resolve();
  const result = prev.then(fn);
  // Keep the chain alive and non-rejecting so one failed command can't wedge it.
  session._execChain = result.then(() => {}, () => {});
  return result;
}

/* Output index file */
app.get('/', function(req, res) {
  res.sendFile(__dirname + '/index.html');
});

app.get('/node_modules/*', function(req, res) {
  res.sendFile(__dirname + req.originalUrl);
});

// Health check endpoint
app.get('/health', function(req, res) {
  res.json({ 
    status: 'ok', 
    timestamp: new Date().toISOString(),
    sessions: terminalSessions.size,
    uptime: process.uptime()
  });
});

// API endpoint to get session info
app.get('/api/sessions', function(req, res) {
  const sessions = [];
  for (const [sessionId, session] of terminalSessions.entries()) {
    const clientCount = sessionClients.get(sessionId)?.size || 0;
    sessions.push({
      id: sessionId,
      createdAt: session.createdAt,
      lastActivity: session.lastActivity,
      clientCount: clientCount,
      isActive: !!session.terminal && !session.terminal.killed
    });
  }
  res.json(sessions);
});

// API endpoint to create a new session
app.post('/api/sessions', function(req, res) {
  const sessionId = req.body.sessionId || `session-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
  const cwd = req.body.cwd;
  
  try {
    const session = getOrCreateSession(sessionId, cwd);
    res.json({
      success: true,
      sessionId: sessionId,
      createdAt: session.createdAt
    });
  } catch (error) {
    res.status(500).json({
      success: false,
      error: error.message
    });
  }
});

// API endpoint to execute a command via HTTP.
// Uses a completion sentinel for deterministic finish + real exit codes, and
// converts commands that exceed the timeout into tracked background jobs.
app.post('/api/execute', async function(req, res) {
  const { sessionId = 'default', command, timeout = 30, background = false } = req.body;

  if (!command) {
    res.status(400).json({ success: false, error: 'Command is required' });
    return;
  }

  try {
    const session = getOrCreateSession(sessionId);

    const activeIso = isolatedManager.getActiveProcId(sessionId);
    if (activeIso) {
      res.json({
        success: false,
        busy: true,
        running: true,
        job_id: activeIso,
        proc_id: activeIso,
        execution_backend: 'isolated',
        error:
          'An isolated interactive process is running. Use /api/isolated/' +
          activeIso + '/input and /output, or DELETE to terminate.',
        stdout: '',
        stderr: '',
        exit_code: null,
        command: command
      });
      return;
    }

    if (req.body.force === 'sentinel' && activeIso) {
      res.status(409).json({
        success: false,
        error: 'Cannot force sentinel while isolated process is active',
        busy: true
      });
      return;
    }

    // EnIGMA-style guardrail: a raw interactive debugger launch (`gdb ./bin`,
    // `gdb --args ...`, `radare2 ./bin`) opens its own REPL and would wedge the
    // sentinel PTY — the trailing completion `printf` is fed into the debugger as
    // one of its commands, gdb errors with "Bad format string, missing '"'.", and
    // the completion marker never returns. Reject it and steer the agent to the
    // Interactive Agent Tools (debug_start/…), exactly as EnIGMA wraps gdb behind
    // start/stop verbs rather than exposing the raw binary.
    if (isRawInteractiveDebugger(command)) {
      const msg =
        'Raw interactive debuggers (gdb / gdbserver / radare2) are disabled in this ' +
        'terminal: launched bare they open their own prompt and hang the session. ' +
        'Use the Interactive Agent Tools instead:\n' +
        '  debug_start <binary> [args]   # open a managed gdb session\n' +
        '  debug_add_breakpoint <loc>\n' +
        '  debug_continue\n' +
        '  debug_step\n' +
        "  debug_exec '<gdb command>'    # e.g. debug_exec 'info registers'\n" +
        '  debug_stop\n' +
        "For a single non-interactive inspection you may use batch mode, e.g. " +
        "`gdb --batch -ex '<cmd>' <binary>`.";
      res.json({
        success: false,
        error: msg,
        stdout: '',
        stderr: msg,
        exit_code: 1,
        command: command,
      });
      return;
    }

    // Server-side assist: interactive commands use isolated subprocesses.
    if (isolatedExecEnabled() && isInteractiveCommand(command)) {
      const block = !background;
      const result = await isolatedManager.start(
        sessionId,
        command,
        session.workingDir,
        { block, timeoutMs: Math.max(1000, parseInt(timeout, 10) * 1000) }
      );
      res.json(result);
      return;
    }

    if (!session.terminal || session.terminal.killed) {
      res.status(500).json({
        success: false,
        error: 'Terminal session not available',
        stdout: '',
        stderr: 'Terminal session not available',
        exit_code: -1,
        command: command
      });
      return;
    }

    // Serialize foreground execution per session (see withSessionExecLock): the
    // shared PTY can be hit by concurrent tool-calls, which previously collided on
    // the busy-guard and returned empty stdout. Queue instead so each command runs
    // in turn and gets its real output.
    await withSessionExecLock(session, async () => {
      // A PRIOR command genuinely backgrounded (timed out and is still running in the
      // PTY). Historically we busy-rejected here, which — for a bare interactive
      // command that never returns — wedged the single PTY forever and sent the agent
      // into a kill/pkill/signal death spiral. Instead, auto-reclaim the PTY (interrupt
      // the stuck job) and run the new command: in this one-command-per-turn agent a new
      // command means "stop waiting on the last one." Only if reclaim genuinely fails do
      // we fall back to the old busy response. Disable with GENCYBER_PTY_AUTORECLAIM=0.
      if (session.activeJob && session.activeJob.running) {
        let reclaimed = false;
        if (autoReclaimEnabled()) {
          reclaimed = await reclaimForegroundJob(session);
        }
        if (!reclaimed && session.activeJob && session.activeJob.running) {
          res.json({
            success: false,
            busy: true,
            running: true,
            job_id: session.activeJob.id,
            error:
              'A command is already running in this session. Poll ' +
              '/api/sessions/' + sessionId + '/output, send input via ' +
              '/api/sessions/' + sessionId + '/input, or interrupt via ' +
              '/api/sessions/' + sessionId + '/signal.',
            stdout: '',
            stderr: '',
            exit_code: null,
            command: command
          });
          return;
        }
      }

      const job = await runCommandInSession(session, command, timeout);
      const cleaned = stripSentinelAndNoise(job.raw, job.token);
      const elapsed = (job.endTime || Date.now()) - job.startTime;

      // EnIGMA IAT: the vendored debug_*/connect_* tools print only
      // <<INTERACTIVE||..||>> sentinels. Route them to the managed gdb/netcat REPL
      // and return the real session output instead of the dummy sentinel lines.
      if (!job.running && hasSentinel(cleaned.text)) {
        const bridged = await interactiveBridge.handle(sessionId, cleaned.text, session.workingDir);
        if (bridged.handled) {
          broadcastToSession(sessionId, 'terminal-output-clean', {
            command: command,
            message: bridged.observation,
            exit_code: 0,
          });
          res.json({
            success: true,
            stdout: bridged.observation,
            stderr: '',
            exit_code: 0,
            command: command,
            running: false,
            job_id: job.id,
            execution_backend: 'interactive',
            interactive_session: interactiveBridge.activeName(sessionId),
            execution_time: elapsed / 1000,
          });
          return;
        }
      }

      if (!job.running && cleaned.text) {
        broadcastToSession(sessionId, 'terminal-output-clean', {
          command: command,
          message: cleaned.text,
          exit_code: cleaned.exitCode,
        });
      }

      res.json({
        success: job.running ? false : cleaned.exitCode === 0,
        stdout: cleaned.text,
        stderr: '',
        exit_code: job.running ? null : cleaned.exitCode,
        command: command,
        running: job.running,
        job_id: job.id,
        timeout: job.running, // exceeded timeout -> backgrounded
        execution_time: elapsed / 1000
      });
    });
  } catch (error) {
    res.status(500).json({
      success: false,
      error: error.message,
      stdout: '',
      stderr: error.message,
      exit_code: -1,
      command: command
    });
  }
});

// API endpoint to delete a session
app.delete('/api/sessions/:sessionId', function(req, res) {
  const sessionId = req.params.sessionId;
  
  if (sessionId === 'default') {
    res.status(400).json({ success: false, error: 'Cannot delete default session' });
    return;
  }
  
  if (terminalSessions.has(sessionId)) {
    destroySession(sessionId);
    res.json({ success: true, message: `Session ${sessionId} destroyed` });
  } else {
    res.status(404).json({ success: false, error: 'Session not found' });
  }
});

// Poll output of a (possibly background) job. Supports incremental reads via
// ?since=<offset>; returns next_offset for the following poll.
app.get('/api/sessions/:sessionId/output', function(req, res) {
  const session = terminalSessions.get(req.params.sessionId);
  if (!session) {
    res.status(404).json({ success: false, error: 'Session not found' });
    return;
  }
  const jobId = req.query.job;
  const since = parseInt(req.query.since || '0', 10) || 0;
  const job = jobId ? session.jobs.get(jobId) : session.activeJob;
  if (!job) {
    res.json({
      success: true, running: false, exit_code: null, stdout: '',
      next_offset: 0, message: 'No matching job for this session'
    });
    return;
  }
  finalizeJob(session, job);
  const rawSlice = (job.raw || '').slice(since);
  const cleaned = stripSentinelAndNoise(rawSlice, job.token);
  res.json({
    success: true,
    job_id: job.id,
    running: job.running,
    exit_code: job.running ? null : cleaned.exitCode,
    stdout: cleaned.text,
    next_offset: (job.raw || '').length,
    timeout: !!job.timedOut,
    command: job.command,
    execution_time: ((job.endTime || Date.now()) - job.startTime) / 1000
  });
});

// Send raw input/keystrokes to the PTY (interactive prompts, REPL, ssh
// password, pager keys, etc.). Captures a short output window for feedback.
app.post('/api/sessions/:sessionId/input', async function(req, res) {
  const session = getOrCreateSession(req.params.sessionId);
  if (!session.terminal || session.terminal.killed) {
    res.status(500).json({ success: false, error: 'Terminal session not available' });
    return;
  }
  let { data, input, append_newline = true, settle_ms = 900 } = req.body;
  if (data === undefined) data = input;
  if (data === undefined || data === null) {
    res.status(400).json({ success: false, error: 'data is required' });
    return;
  }
  const capture = captureWindow(session, settle_ms);
  try {
    session.terminal.write(String(data) + (append_newline ? '\n' : ''));
    session.lastActivity = Date.now();
  } catch (e) {
    res.status(500).json({ success: false, error: e.message });
    return;
  }
  const buf = await capture;
  if (session.activeJob) finalizeJob(session, session.activeJob);
  const token = session.activeJob ? session.activeJob.token : null;
  const cleaned = stripSentinelAndNoise(buf, token);
  res.json({
    success: true,
    stdout: cleaned.text,
    running: session.activeJob ? session.activeJob.running : false,
    exit_code: session.activeJob && !session.activeJob.running ? cleaned.exitCode : null,
    job_id: session.activeJob ? session.activeJob.id : null
  });
});

// Send a control signal to the foreground command without killing the shell.
app.post('/api/sessions/:sessionId/signal', async function(req, res) {
  const session = getOrCreateSession(req.params.sessionId);
  if (!session.terminal || session.terminal.killed) {
    res.status(500).json({ success: false, error: 'Terminal session not available' });
    return;
  }
  const { signal = 'SIGINT', settle_ms = 700 } = req.body;
  const ctrlMap = { SIGINT: '\x03', SIGQUIT: '\x1c', SIGTSTP: '\x1a', EOF: '\x04' };
  const ctrl = ctrlMap[String(signal).toUpperCase()] || '\x03';
  const capture = captureWindow(session, settle_ms);
  try {
    session.terminal.write(ctrl);
    session.lastActivity = Date.now();
  } catch (e) {
    res.status(500).json({ success: false, error: e.message });
    return;
  }
  const buf = await capture;
  const job = session.activeJob;
  if (job) {
    finalizeJob(session, job); // queued sentinel printf often still fires (code 130)
    if (job.running) {
      job.running = false;
      job.endTime = Date.now();
      if (job.listener) {
        try { session.terminal.removeListener('data', job.listener); } catch (e) {}
      }
      if (session.activeJob === job) session.activeJob = null;
    }
  }
  const cleaned = stripSentinelAndNoise(buf, job ? job.token : null);
  res.json({
    success: true,
    signal: String(signal).toUpperCase(),
    stdout: cleaned.text,
    job_id: job ? job.id : null
  });
});

// --- Isolated subprocess API (CAMEL-style process isolation) ----------------

app.post('/api/sessions/:sessionId/isolated/exec', async function(req, res) {
  const sessionId = req.params.sessionId;
  const { command, block = false, timeout = 30, background } = req.body;
  if (!command) {
    res.status(400).json({ success: false, error: 'command is required' });
    return;
  }
  try {
    const session = getOrCreateSession(sessionId);
    const bg = background === true || background === 'true';
    const result = await isolatedManager.start(
      sessionId,
      command,
      session.workingDir,
      {
        block: !bg && !!block,
        timeoutMs: Math.max(1000, parseInt(timeout, 10) * 1000),
      }
    );
    res.json(result);
  } catch (e) {
    res.status(500).json({ success: false, error: e.message, command });
  }
});

app.post('/api/isolated/:procId/input', function(req, res) {
  const { data, input, append_newline = true } = req.body;
  const payload = data !== undefined ? data : input;
  if (payload === undefined || payload === null) {
    res.status(400).json({ success: false, error: 'data is required' });
    return;
  }
  const result = isolatedManager.write(req.params.procId, payload, { append_newline });
  if (result.error && result.error === 'Process not found') {
    res.status(404).json(result);
    return;
  }
  res.json(result);
});

app.get('/api/isolated/:procId/output', function(req, res) {
  const since = parseInt(req.query.since || '0', 10) || 0;
  const result = isolatedManager.read(req.params.procId, since);
  if (result.error === 'Process not found') {
    res.status(404).json(result);
    return;
  }
  res.json(result);
});

app.post('/api/isolated/:procId/signal', function(req, res) {
  const { signal = 'SIGINT' } = req.body;
  const result = isolatedManager.signal(req.params.procId, signal);
  if (result.error === 'Process not found') {
    res.status(404).json(result);
    return;
  }
  res.json(result);
});

app.delete('/api/isolated/:procId', function(req, res) {
  const result = isolatedManager.kill(req.params.procId, 'SIGTERM');
  if (result.error === 'Process not found') {
    res.status(404).json(result);
    return;
  }
  res.json(result);
});

// Write a file directly onto the workbench volume (no shell quoting). Accepts
// either content_base64 (preferred for binary / arbitrary bytes) or content.
app.post('/api/sessions/:sessionId/write-file', function(req, res) {
  const session = getOrCreateSession(req.params.sessionId);
  let { path: filePath, content, content_base64, mode } = req.body;
  if (!filePath) {
    res.status(400).json({ success: false, error: 'path is required' });
    return;
  }
  try {
    if (!path.isAbsolute(filePath)) {
      filePath = path.join(session.workingDir || process.cwd(), filePath);
    }
    fs.mkdirSync(path.dirname(filePath), { recursive: true });
    let buf;
    if (content_base64 !== undefined && content_base64 !== null) {
      buf = Buffer.from(content_base64, 'base64');
    } else {
      buf = Buffer.from(String(content || ''), 'utf8');
    }
    fs.writeFileSync(filePath, buf);
    if (mode) {
      try {
        fs.chmodSync(filePath, typeof mode === 'string' ? parseInt(mode, 8) : mode);
      } catch (e) {}
    }
    res.json({ success: true, path: filePath, bytes: buf.length });
  } catch (e) {
    res.status(500).json({ success: false, error: e.message, path: filePath });
  }
});

// Introspect a session: shell pid, current cwd (via /proc), running job status.
app.get('/api/sessions/:sessionId/info', function(req, res) {
  const session = terminalSessions.get(req.params.sessionId);
  if (!session) {
    res.status(404).json({ success: false, error: 'Session not found' });
    return;
  }
  const term = session.terminal;
  let pid = null;
  let cwd = session.workingDir;
  if (term && !term.killed) {
    pid = term.pid;
    try { cwd = fs.readlinkSync('/proc/' + pid + '/cwd'); } catch (e) {}
  }
  const active = session.activeJob;
  res.json({
    success: true,
    session_id: session.id,
    pid: pid,
    cwd: cwd,
    initial_cwd: session.workingDir,
    created_at: session.createdAt,
    last_activity: session.lastActivity,
    is_active: !!term && !term.killed,
    running: !!(active && active.running),
    active_job: active
      ? { id: active.id, command: active.command, running: active.running }
      : null,
    jobs: Array.from(session.jobs.values()).slice(-10).map((j) => ({
      id: j.id,
      command: j.command,
      running: j.running,
      exit_code: j.exitCode,
      timed_out: !!j.timedOut
    }))
  });
});

/* Redirect the rest to index */
app.get('*', function(req, res) {
  res.redirect('/');
});

io.on('connection', (socket) => {
  let currentSessionId = null;

  // Auto-join default session after a brief delay
  setTimeout(() => {
    if (!currentSessionId) {
      joinSessionInternal(socket, 'default');
    }
  }, 1000);

  function joinSessionInternal(socket, sessionId, cwd = null) {
    // Leave current session if any
    if (currentSessionId) {
      removeClientFromSession(currentSessionId, socket);
    }
    
    // Join new session
    currentSessionId = sessionId;
    const session = getOrCreateSession(sessionId, cwd);
    addClientToSession(sessionId, socket);
    
    // Send session info to client
    socket.emit('session-joined', {
      sessionId: sessionId,
      createdAt: session.createdAt,
      lastActivity: session.lastActivity
    });
    
    // Send recent output history to new client
    let historyCarry = '';
    const recentHistory = session.outputHistory.slice(-20); // Last 20 entries
    recentHistory.forEach(entry => {
      if (entry.type === 'stdout') {
        const filtered = filterDisplayChunk(historyCarry, String(entry.data || ''));
        historyCarry = filtered.carry;
        if (filtered.text) {
          socket.emit('terminal-output-stdout', { message: filtered.text });
        }
      } else if (entry.type === 'stderr') {
        socket.emit('terminal-output-stderr', { message: entry.data });
      }
    });
  }

  // Handle joining a specific session
  socket.on('join-session', (data) => {
    const sessionId = data.sessionId || 'default';
    const cwd = data.cwd;
    joinSessionInternal(socket, sessionId, cwd);
  });

  // Handle terminal resize
  socket.on('resize', (data) => {
    if (currentSessionId) {
      const session = terminalSessions.get(currentSessionId);
      if (session && session.terminal && !session.terminal.killed) {
        try {
          session.terminal.resize(data.cols || 120, data.rows || 30);
        } catch (error) {
          // Ignore resize errors
        }
      }
    }
  });

  // Handle command input
  socket.on('input', (command) => {
    if (!currentSessionId) {
      // Auto-join default session if not already in one
      joinSessionInternal(socket, 'default');
    }
    
    const session = terminalSessions.get(currentSessionId);
    if (session && session.terminal && !session.terminal.killed) {
      try {
        // Send command as-is to the terminal
        session.terminal.write(command);
        session.lastActivity = Date.now();
      } catch (error) {
        socket.emit('terminal-output-stdout', { 
          message: `Error: Terminal session not available\n` 
        });
      }
    } else {
      socket.emit('terminal-output-stdout', { 
        message: `Error: No active terminal session\n` 
      });
    }
  });

  // Handle disconnect
  socket.on('disconnect', () => {
    if (currentSessionId) {
      removeClientFromSession(currentSessionId, socket);
    }
  });

  // Handle connection errors
  socket.on('error', (error) => {
    // TODO: Graceful error handling
    console.error(`Socket error for client ${socket.id}:`, error);
  });
});

// Graceful shutdown
function gracefulShutdown() {
  console.log('\nShutting down gracefully...');
  for (const sessionId of terminalSessions.keys()) {
    isolatedManager.killSessionProcs(sessionId);
    interactiveBridge.killSession(sessionId);
  }
  // Close all terminal sessions
  for (const [sessionId, session] of terminalSessions.entries()) {
    if (session.terminal && !session.terminal.killed) {
      console.log(`Killing terminal for session: ${sessionId}`);
      try {
        session.terminal.kill('SIGKILL');
      } catch (e) {
        console.error(`Failed to kill terminal for session ${sessionId}:`, e);
      }
    }
  }
  
  // Close the HTTP server
  http.close(() => {
    console.log('Server and all connections closed.');
    process.exit(0);
  });

  // Force exit after a timeout
  setTimeout(() => {
    console.error('Could not close connections in time, forcing shutdown.');
    process.exit(1);
  }, 10000);
}

process.on('SIGTERM', gracefulShutdown);
process.on('SIGINT', gracefulShutdown);

const PORT = process.env.PORT || 3000;
http.listen(PORT, () => {
  console.log(`Terminal session server started on port ${PORT}`);
  
  // Create default session after server starts
  setTimeout(createDefaultSession, 1000);
});
