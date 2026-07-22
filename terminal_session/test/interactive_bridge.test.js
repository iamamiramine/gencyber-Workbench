'use strict';

/*
 * Tests for the EnIGMA IAT bridge:
 *   - getInteractiveCommands parses the real sentinel output emitted by the
 *     vendored debug.sh / server_connection.sh tools.
 *   - InteractiveBridge.handle drives a managed REPL: START opens a session,
 *     subsequent commands are fed in and their real output is read back, the
 *     single-session guard fires, and STOP closes it.
 *
 * Run: node test/interactive_bridge.test.js   (needs node >= 14; gdb optional)
 */

const assert = require('assert');
const { execFileSync, spawnSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const {
  InteractiveBridge,
  getInteractiveCommands,
  hasSentinel,
  CONFIGS,
} = require('../interactive_bridge');

const CMD_DIR = path.join(__dirname, '..', 'enigma_commands');

function bashEmit(script, fn) {
  // Source a vendored tool script and run one of its functions, capturing the
  // sentinel lines it prints — exactly what the PTY would feed the bridge.
  return execFileSync('bash', ['-c', `source ${path.join(CMD_DIR, script)}; ${fn}`], {
    encoding: 'utf8',
  });
}

function testParser() {
  const dbg = bashEmit('debug.sh', 'debug_start /usr/bin/true');
  assert.ok(hasSentinel(dbg), 'debug_start emits sentinels');
  const p1 = getInteractiveCommands(dbg);
  assert.strictEqual(p1.sessionName, 'gdb');
  assert.deepStrictEqual(p1.commands, ['START', 'set confirm off', 'file /usr/bin/true', 'starti']);
  console.log('  ok - parses debug_start sentinels');

  const conn = bashEmit('server_connection.sh', 'connect_start 127.0.0.1 4444');
  const p2 = getInteractiveCommands(conn);
  assert.strictEqual(p2.sessionName, 'connect');
  assert.deepStrictEqual(p2.commands, ['START', 'connect 127.0.0.1 4444']);
  console.log('  ok - parses connect_start sentinels');

  const stop = bashEmit('debug.sh', 'debug_stop');
  const p3 = getInteractiveCommands(stop);
  assert.strictEqual(p3.sessionName, 'gdb');
  assert.deepStrictEqual(p3.commands, ['quit', 'STOP']);
  console.log('  ok - parses debug_stop (quit + STOP)');

  assert.strictEqual(getInteractiveCommands('nothing here').sessionName, null);
  assert.strictEqual(hasSentinel('plain output'), false);
  console.log('  ok - no false positives on plain output');
}

// Build the sentinel block a tool function would print for a given inner cmd.
function sentinels(sessionName, ...cmds) {
  const lines = [`<<INTERACTIVE||SESSION=${sessionName}||INTERACTIVE>>`];
  for (const c of cmds) lines.push(`<<INTERACTIVE||${c}||INTERACTIVE>>`);
  return lines.join('\n');
}

async function testStubRepl() {
  // A minimal REPL: prints "(stub) ", echoes each line, quits on "quit".
  const stub = path.join(os.tmpdir(), `gc_stub_repl_${process.pid}.sh`);
  fs.writeFileSync(
    stub,
    [
      '#!/usr/bin/env bash',
      "printf '(stub) '",
      'while IFS= read -r line; do',
      '  if [ "$line" = "quit" ]; then printf \'bye\\n\'; exit 0; fi',
      '  printf \'echo: %s\\n(stub) \' "$line"',
      'done',
      '',
    ].join('\n'),
  );
  CONFIGS.dummy = {
    cmdline: ['bash', [stub]],
    prompt: '(stub)',
    startCommand: 'dummy_start',
    stopCommand: 'dummy_stop',
    quitInSession: ['quit'],
  };

  const bridge = new InteractiveBridge();
  const sid = 'stub-1';

  // Not started yet -> error guidance.
  const notRunning = await bridge.handle(sid, sentinels('dummy', 'hello'), os.tmpdir());
  assert.ok(notRunning.handled);
  assert.ok(/is not running/.test(notRunning.observation), notRunning.observation);
  console.log('  ok - command before START reports not running');

  // START opens the session and reads the initial prompt.
  const started = await bridge.handle(sid, sentinels('dummy', 'START'), os.tmpdir());
  assert.ok(started.handled);
  assert.strictEqual(bridge.activeName(sid), 'dummy');
  console.log('  ok - START opens session');

  // A command is fed in and its real output read back.
  const r1 = await bridge.handle(sid, sentinels('dummy', 'hello'), os.tmpdir());
  assert.ok(/echo: hello/.test(r1.observation), r1.observation);
  const r2 = await bridge.handle(sid, sentinels('dummy', 'world'), os.tmpdir());
  assert.ok(/echo: world/.test(r2.observation), r2.observation);
  console.log('  ok - commands routed to live REPL, state persists');

  // Single-session guard: a different session kind is rejected.
  const guard = await bridge.handle(sid, sentinels('gdb', 'START'), os.tmpdir());
  assert.ok(/already open/.test(guard.observation), guard.observation);
  console.log('  ok - single-session guard');

  // STOP closes it.
  const stopped = await bridge.handle(sid, sentinels('dummy', 'quit', 'STOP'), os.tmpdir());
  assert.ok(/stopped successfully/.test(stopped.observation), stopped.observation);
  assert.strictEqual(bridge.activeName(sid), null);
  console.log('  ok - STOP closes session');

  try { fs.unlinkSync(stub); } catch (e) { /* ignore */ }
}

async function testRealGdb() {
  const hasGdb = spawnSync('bash', ['-c', 'command -v gdb'], { encoding: 'utf8' }).status === 0;
  if (!hasGdb) {
    console.log('  -- skip real gdb test (gdb not installed)');
    return;
  }
  const bridge = new InteractiveBridge();
  const sid = 'gdb-1';
  const start = await bridge.handle(
    sid,
    sentinels('gdb', 'START', 'set confirm off', 'file /usr/bin/true', 'starti'),
    os.tmpdir(),
  );
  assert.ok(start.handled);
  assert.strictEqual(bridge.activeName(sid), 'gdb');
  const regs = await bridge.handle(sid, sentinels('gdb', 'info registers'), os.tmpdir());
  assert.ok(/rip|rsp|eip|esp/i.test(regs.observation), 'register dump expected:\n' + regs.observation);
  console.log('  ok - real gdb: starti + info registers');
  const stop = await bridge.handle(sid, sentinels('gdb', 'quit', 'STOP'), os.tmpdir());
  assert.ok(/stopped successfully/.test(stop.observation), stop.observation);
  assert.strictEqual(bridge.activeName(sid), null);
  console.log('  ok - real gdb: stop');
}

async function run() {
  testParser();
  await testStubRepl();
  await testRealGdb();
  console.log('interactive_bridge tests passed');
}

run().catch((e) => {
  console.error(e);
  process.exit(1);
});
