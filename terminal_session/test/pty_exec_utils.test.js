/*
 * Dependency-free tests for the PTY sentinel/noise helpers.
 * Run: node test/pty_exec_utils.test.js
 */

'use strict';

const assert = require('assert');
const {
  stripAnsi,
  sentinelRegex,
  buildSentinelPayload,
  stripSentinelAndNoise,
} = require('../pty_exec_utils');

let passed = 0;
function test(name, fn) {
  fn();
  passed += 1;
  console.log('  ok - ' + name);
}

// Simulate what bash echoes + prints for a command under our sentinel scheme.
function simulatePty(command, output, exitCode, token, prompt) {
  prompt = prompt || '[root@box workspace]$ ';
  const payload = buildSentinelPayload(command, token);
  const [echoedCmd, echoedPrintf] = payload.split('\n');
  // 1) prompt + echoed command, 2) command output, 3) prompt + echoed printf,
  // 4) expanded sentinel, 5) next prompt.
  return (
    prompt + echoedCmd + '\n' +
    (output ? output + '\n' : '') +
    prompt + echoedPrintf + '\n' +
    '\n__GC_DONE_' + token + '__' + exitCode + '__\n' +
    prompt
  );
}

test('stripAnsi removes CSI/OSC and CR', () => {
  assert.strictEqual(stripAnsi('\x1b[31mred\x1b[0m\r\nx'), 'red\nx');
  assert.strictEqual(stripAnsi('\x1b]0;title\x07ok'), 'ok');
});

test('buildSentinelPayload splits DONE so the echo cannot match', () => {
  const payload = buildSentinelPayload('echo hi', 'abc123');
  // Real expansion would match; the literal payload must NOT.
  assert.ok(!sentinelRegex('abc123').test(payload),
    'payload literal should not contain the contiguous marker');
  assert.ok(sentinelRegex('abc123').test('__GC_DONE_abc123__0__'));
});

test('parses real exit code 0 and strips noise', () => {
  const token = 'deadbeef';
  const raw = simulatePty('echo hello', 'hello', 0, token);
  const { text, exitCode } = stripSentinelAndNoise(raw, token);
  assert.strictEqual(exitCode, 0);
  assert.strictEqual(text.trim(), 'hello');
});

test('parses non-zero exit code', () => {
  const token = 'cafe';
  const raw = simulatePty('false', '', 1, token);
  const { text, exitCode } = stripSentinelAndNoise(raw, token);
  assert.strictEqual(exitCode, 1);
  assert.strictEqual(text.trim(), '');
});

test('command with trailing comment still completes (newline sentinel)', () => {
  // The dangerous case for a "; printf" approach. With newline separation the
  // sentinel is on its own line and is unaffected by the trailing comment.
  const token = 'f00d';
  const raw = simulatePty('echo hi # note', 'hi', 0, token);
  const { text, exitCode } = stripSentinelAndNoise(raw, token);
  assert.strictEqual(exitCode, 0);
  assert.strictEqual(text.trim(), 'hi');
});

test('multi-line output preserved, prompts stripped', () => {
  const token = 'aa11';
  const raw = simulatePty('printf "a\\nb\\nc"', 'a\nb\nc', 0, token);
  const { text } = stripSentinelAndNoise(raw, token);
  assert.strictEqual(text.trim(), 'a\nb\nc');
});

test('running job (no sentinel yet) -> null exit code, partial output kept', () => {
  const token = 'beef';
  const prompt = '[root@box workspace]$ ';
  const payload = buildSentinelPayload('sleep 100', token);
  const echoedCmd = payload.split('\n')[0];
  const raw = prompt + echoedCmd + '\npartial output so far\n';
  const { text, exitCode } = stripSentinelAndNoise(raw, token);
  assert.strictEqual(exitCode, null);
  assert.ok(text.indexOf('partial output so far') !== -1);
});

test('incremental slice cleans correctly', () => {
  const token = '1234';
  const raw = simulatePty('id', 'uid=0(root) gid=0(root)', 0, token);
  // Simulate reading from offset 0 (whole buffer).
  const { text, exitCode } = stripSentinelAndNoise(raw.slice(0), token);
  assert.strictEqual(exitCode, 0);
  assert.ok(text.indexOf('uid=0(root)') !== -1);
});

console.log('\nAll PTY util tests passed: ' + passed);
