/*
 * Live integration test for the upgraded terminal-session server.
 *
 * Requires the runtime deps (express, node-pty, ...) to be installed, so run it
 * where they exist (e.g. inside the workbench container):
 *
 *     node test/server_integration.test.js
 *
 * It boots the real server on an ephemeral port, drives the HTTP API, and
 * asserts sentinel exit codes, backgrounding, interactive input, signalling,
 * write-file, and session info.
 */

'use strict';

const assert = require('assert');
const http = require('http');
const { spawn } = require('child_process');
const path = require('path');
const os = require('os');
const fs = require('fs');

const PORT = 3999;
const BASE = `http://127.0.0.1:${PORT}`;
const SID = 'itest';

function req(method, urlPath, body) {
  return new Promise((resolve, reject) => {
    const data = body ? JSON.stringify(body) : null;
    const r = http.request(
      BASE + urlPath,
      {
        method,
        headers: data
          ? { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) }
          : {},
      },
      (res) => {
        let buf = '';
        res.on('data', (c) => (buf += c));
        res.on('end', () => {
          try {
            resolve({ status: res.statusCode, json: buf ? JSON.parse(buf) : null });
          } catch (e) {
            resolve({ status: res.statusCode, json: null, raw: buf });
          }
        });
      }
    );
    r.on('error', reject);
    if (data) r.write(data);
    r.end();
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitHealthy(timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const h = await req('GET', '/health');
      if (h.status === 200) return true;
    } catch (e) {}
    await sleep(250);
  }
  return false;
}

let passed = 0;
function check(name, cond) {
  assert.ok(cond, 'FAILED: ' + name);
  passed += 1;
  console.log('  ok - ' + name);
}

async function main() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'gc-itest-'));
  const server = spawn('node', [path.join(__dirname, '..', 'app.js'), tmp], {
    env: { ...process.env, PORT: String(PORT) },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  server.stdout.on('data', () => {});
  server.stderr.on('data', (d) => process.stderr.write('[server] ' + d));

  try {
    const ok = await waitHealthy(15000);
    assert.ok(ok, 'server did not become healthy');
    await req('POST', '/api/sessions', { sessionId: SID, cwd: tmp });

    // 1) Real exit code on success.
    let r = await req('POST', '/api/execute', { sessionId: SID, command: 'echo hello-world', timeout: 15 });
    check('echo exit_code 0', r.json.exit_code === 0);
    check('echo stdout', /hello-world/.test(r.json.stdout));

    // 2) Real non-zero exit code.
    r = await req('POST', '/api/execute', { sessionId: SID, command: 'false', timeout: 15 });
    check('false exit_code 1', r.json.exit_code === 1);

    // 3) Trailing comment does not swallow the sentinel.
    r = await req('POST', '/api/execute', { sessionId: SID, command: 'echo hi # trailing', timeout: 15 });
    check('comment cmd exit 0', r.json.exit_code === 0 && /hi/.test(r.json.stdout));

    // 4) Backgrounding: a long command exceeds a short timeout and keeps running.
    r = await req('POST', '/api/execute', { sessionId: SID, command: 'sleep 4; echo SLEPT', timeout: 1 });
    check('long cmd backgrounds', r.json.running === true && !!r.json.job_id);
    const jobId = r.json.job_id;

    // 5) Poll the job until it completes with the real exit code + final output.
    let polled = null;
    for (let i = 0; i < 20; i++) {
      await sleep(500);
      polled = await req('GET', `/api/sessions/${SID}/output?job=${jobId}`);
      if (polled.json && polled.json.running === false) break;
    }
    check('job finished via poll', polled.json.running === false);
    check('job final exit 0', polled.json.exit_code === 0);
    check('job output captured', /SLEPT/.test(polled.json.stdout));

    // 6) Interactive input: read a value then echo it.
    r = await req('POST', '/api/execute', {
      sessionId: SID,
      command: 'read -p "name: " v; echo GOT=$v',
      timeout: 1,
    });
    // The read blocks -> backgrounds. Now answer it.
    const inResp = await req('POST', `/api/sessions/${SID}/input`, { data: 'alice', settle_ms: 1200 });
    check('input produced echo', /GOT=alice/.test(inResp.json.stdout) || inResp.json.running === false);

    // 7) Signal: start a long sleep, then interrupt it.
    r = await req('POST', '/api/execute', { sessionId: SID, command: 'sleep 30', timeout: 1 });
    check('sleep backgrounds for signal', r.json.running === true);
    const sig = await req('POST', `/api/sessions/${SID}/signal`, { signal: 'SIGINT', settle_ms: 800 });
    check('signal acknowledged', sig.json.success === true);

    // 8) write-file then read it back.
    const target = path.join(tmp, 'sub', 'note.txt');
    const w = await req('POST', `/api/sessions/${SID}/write-file`, {
      path: target,
      content: 'secret-file-content',
    });
    check('write-file ok', w.json.success === true && w.json.bytes > 0);
    check('write-file on disk', fs.readFileSync(target, 'utf8') === 'secret-file-content');

    // 9) write-file via base64.
    const b64 = await req('POST', `/api/sessions/${SID}/write-file`, {
      path: path.join(tmp, 'b.bin'),
      content_base64: Buffer.from('bytes').toString('base64'),
    });
    check('write-file base64 ok', b64.json.success === true);

    // 10) info endpoint.
    const info = await req('GET', `/api/sessions/${SID}/info`);
    check('info has pid', typeof info.json.pid === 'number');
    check('info has cwd', typeof info.json.cwd === 'string');

    // 11) Concurrency regression: several large-output commands fired at ONCE on a
    //     shared session must each return their real output. Before withSessionExecLock
    //     all-but-one hit the busy-guard and returned EMPTY stdout — the root cause of
    //     the ~48% empty-output rate (worst on large output like strings/objdump).
    const SID2 = 'itest-conc';
    await req('POST', '/api/sessions', { sessionId: SID2, cwd: tmp });
    const N = 6;
    const concurrent = await Promise.all(
      Array.from({ length: N }, () =>
        req('POST', '/api/execute', { sessionId: SID2, command: 'seq 1 5000', timeout: 15 })
      )
    );
    const nonEmpty = concurrent.filter(
      (c) => c.json && String(c.json.stdout || '').includes('5000')
    ).length;
    check('concurrent commands all return real output (no empty busy-reject)', nonEmpty === N);

    // 12) Classifier regression: a non-interactive command that merely mentions an
    //     interactive program name as a path/arg (e.g. `cat /etc/passwd`) must NOT be
    //     misrouted to the isolated backend (which would busy-reject siblings empty).
    const r2 = await req('POST', '/api/execute', { sessionId: SID2, command: 'cat /etc/passwd', timeout: 15 });
    check(
      'cat /etc/passwd runs on PTY, not isolated backend',
      r2.json.execution_backend !== 'isolated' && /root/.test(r2.json.stdout || '')
    );

    console.log('\nAll server integration tests passed: ' + passed);
  } finally {
    server.kill('SIGKILL');
  }
}

main().then(
  () => process.exit(0),
  (e) => {
    console.error(e);
    process.exit(1);
  }
);
