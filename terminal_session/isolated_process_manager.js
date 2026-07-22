'use strict';

const { spawn } = require('child_process');
const crypto = require('crypto');

/**
 * CAMEL-style isolated subprocess execution: dedicated stdin/stdout per program.
 * Never appends completion sentinels.
 */
class IsolatedProcessManager {
  constructor() {
    /** @type {Map<string, object>} */
    this.procs = new Map();
    /** @type {Map<string, string>} sessionId -> active procId */
    this.sessionActive = new Map();
  }

  _newId() {
    return crypto.randomBytes(8).toString('hex');
  }

  get(procId) {
    return this.procs.get(procId) || null;
  }

  getActiveProcId(sessionId) {
    const id = this.sessionActive.get(sessionId);
    if (!id) return null;
    const rec = this.procs.get(id);
    if (!rec || !rec.running) {
      this.sessionActive.delete(sessionId);
      return null;
    }
    return id;
  }

  sessionHasActive(sessionId) {
    return !!this.getActiveProcId(sessionId);
  }

  killSessionProcs(sessionId) {
    for (const [id, rec] of this.procs.entries()) {
      if (rec.sessionId === sessionId && rec.running) {
        this.kill(id, 'SIGTERM');
      }
    }
    this.sessionActive.delete(sessionId);
  }

  /**
   * @param {string} sessionId
   * @param {string} command
   * @param {string} cwd
   * @param {{ block?: boolean, timeoutMs?: number }} opts
   */
  start(sessionId, command, cwd, opts = {}) {
    const block = !!opts.block;
    const timeoutMs = Math.max(1000, parseInt(opts.timeoutMs, 10) || 30000);

    const active = this.getActiveProcId(sessionId);
    if (active) {
      return {
        busy: true,
        success: false,
        proc_id: active,
        job_id: active,
        running: true,
        stdout: '',
        stderr: '',
        exit_code: null,
        command,
        error:
          'An isolated process is already running for this session. Use isolated input/output APIs.',
      };
    }

    const procId = this._newId();
    let child;
    try {
      child = spawn(command, [], {
        shell: true,
        cwd: cwd || process.cwd(),
        stdio: ['pipe', 'pipe', 'pipe'],
        env: process.env,
      });
    } catch (e) {
      return {
        success: false,
        proc_id: null,
        job_id: null,
        running: false,
        stdout: '',
        stderr: String(e.message || e),
        exit_code: -1,
        command,
        execution_backend: 'isolated',
      };
    }

    const rec = {
      procId,
      sessionId,
      command,
      child,
      stdout: '',
      stderr: '',
      running: true,
      exitCode: null,
      startTime: Date.now(),
      endTime: null,
      timedOut: false,
      _waiters: [],
    };

    const onData = (stream, chunk) => {
      const s = chunk.toString();
      if (stream === 'stdout') rec.stdout += s;
      else rec.stderr += s;
    };
    child.stdout.on('data', (c) => onData('stdout', c));
    child.stderr.on('data', (c) => onData('stderr', c));

    const finalize = (code, signal) => {
      if (!rec.running) return;
      rec.running = false;
      rec.endTime = Date.now();
      if (code !== null && code !== undefined) rec.exitCode = code;
      else if (signal) rec.exitCode = 128;
      if (this.sessionActive.get(sessionId) === procId) {
        this.sessionActive.delete(sessionId);
      }
      const waiters = rec._waiters.splice(0);
      waiters.forEach((w) => w());
    };

    child.on('exit', (code) => finalize(code, null));
    child.on('error', (err) => {
      rec.stderr += (rec.stderr ? '\n' : '') + String(err.message || err);
      finalize(-1, null);
    });

    this.procs.set(procId, rec);
    this.sessionActive.set(sessionId, procId);

    const buildResponse = (partial = {}) => {
      const combined = rec.stdout + (rec.stderr ? (rec.stdout ? '\n' : '') + rec.stderr : '');
      const elapsed = ((rec.endTime || Date.now()) - rec.startTime) / 1000;
      return {
        success: rec.running ? false : rec.exitCode === 0,
        proc_id: procId,
        job_id: procId,
        running: rec.running,
        stdout: combined,
        stderr: '',
        exit_code: rec.running ? null : rec.exitCode,
        command,
        timeout: !!rec.timedOut,
        execution_time: elapsed,
        execution_backend: 'isolated',
        interactive_active: rec.running,
        ...partial,
      };
    };

    return new Promise((resolve) => {
      let settled = false;
      const done = (extra) => {
        if (settled) return;
        settled = true;
        if (timer) clearTimeout(timer);
        resolve(buildResponse(extra));
      };

      const timer = setTimeout(() => {
        if (!rec.running) return;
        rec.timedOut = true;
        if (block) {
          this.kill(procId, 'SIGTERM');
          rec._waiters.push(() => done({}));
        } else {
          done({});
        }
      }, timeoutMs);

      rec._waiters.push(() => done({}));

      if (block) {
        child.on('exit', () => {
          if (!settled) done({});
        });
      } else {
        // Non-blocking: return after a short yield so first bytes may arrive.
        setImmediate(() => {
          if (!rec.running) {
            done({});
          } else {
            settled = true;
            clearTimeout(timer);
            resolve(buildResponse({}));
          }
        });
      }
    });
  }

  write(procId, data, { appendNewline = true } = {}) {
    const rec = this.procs.get(procId);
    if (!rec) {
      return { success: false, error: 'Process not found', proc_id: procId };
    }
    if (!rec.running) {
      return {
        success: true,
        proc_id: procId,
        running: false,
        stdout: rec.stdout + (rec.stderr ? '\n' + rec.stderr : ''),
        exit_code: rec.exitCode,
      };
    }
    try {
      if (rec.child.stdin && !rec.child.stdin.destroyed) {
        rec.child.stdin.write(String(data) + (appendNewline ? '\n' : ''));
      }
    } catch (e) {
      return { success: false, error: e.message, proc_id: procId };
    }
    const combined = rec.stdout + (rec.stderr ? '\n' + rec.stderr : '');
    return {
      success: true,
      proc_id: procId,
      job_id: procId,
      running: rec.running,
      stdout: combined,
      exit_code: rec.running ? null : rec.exitCode,
      execution_backend: 'isolated',
    };
  }

  read(procId, since = 0) {
    const rec = this.procs.get(procId);
    if (!rec) {
      return {
        success: false,
        error: 'Process not found',
        proc_id: procId,
        stdout: '',
        running: false,
        next_offset: since,
      };
    }
    const combined = rec.stdout + (rec.stderr ? '\n' + rec.stderr : '');
    const slice = combined.slice(since);
    return {
      success: true,
      proc_id: procId,
      job_id: procId,
      running: rec.running,
      exit_code: rec.running ? null : rec.exitCode,
      stdout: slice,
      next_offset: combined.length,
      command: rec.command,
      timeout: !!rec.timedOut,
      execution_time: ((rec.endTime || Date.now()) - rec.startTime) / 1000,
      execution_backend: 'isolated',
    };
  }

  signal(procId, signalName = 'SIGINT') {
    const rec = this.procs.get(procId);
    if (!rec) {
      return { success: false, error: 'Process not found', proc_id: procId };
    }
    if (!rec.running) {
      return {
        success: true,
        signal: signalName,
        proc_id: procId,
        stdout: rec.stdout,
        running: false,
      };
    }
    try {
      const sig = String(signalName || 'SIGINT').toUpperCase();
      if (sig === 'EOF' && rec.child.stdin && !rec.child.stdin.destroyed) {
        rec.child.stdin.end();
      } else {
        rec.child.kill(sig === 'SIGKILL' ? 'SIGKILL' : sig === 'SIGTERM' ? 'SIGTERM' : 'SIGINT');
      }
    } catch (e) {
      return { success: false, error: e.message, proc_id: procId };
    }
    const combined = rec.stdout + (rec.stderr ? '\n' + rec.stderr : '');
    return {
      success: true,
      signal: signalName,
      proc_id: procId,
      job_id: procId,
      stdout: combined,
      running: rec.running,
      execution_backend: 'isolated',
    };
  }

  kill(procId, signal = 'SIGTERM') {
    const rec = this.procs.get(procId);
    if (!rec) {
      return { success: false, error: 'Process not found' };
    }
    if (rec.running) {
      try {
        rec.child.kill(signal);
      } catch (e) {
        /* ignore */
      }
      rec.running = false;
      rec.endTime = Date.now();
      if (rec.exitCode === null) rec.exitCode = -1;
      if (this.sessionActive.get(rec.sessionId) === procId) {
        this.sessionActive.delete(rec.sessionId);
      }
    }
    return { success: true, proc_id: procId, running: false, exit_code: rec.exitCode };
  }
}

module.exports = { IsolatedProcessManager };
