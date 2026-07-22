'use strict';

const test = require('node:test');
const assert = require('node:assert');
const {
  stripSentinelAndNoise,
  isDisplayNoiseLine,
  filterDisplayChunk,
} = require('../pty_exec_utils');

test('stripSentinelAndNoise removes GC_DONE marker', () => {
  const raw =
    'root@box:/workspace# echo hi\n' +
    'hi\n' +
    "root@box:/workspace# printf '\\n__GC_DONE_abc123__%d__\\n' \"$?\"\n\n" +
    '__GC_DONE_abc123__0__\n';
  const { text, exitCode } = stripSentinelAndNoise(raw, 'abc123');
  assert.strictEqual(exitCode, 0);
  assert.ok(text.includes('hi'));
  assert.ok(!text.includes('__GC_DONE_'));
});

test('isDisplayNoiseLine hides sentinel printf and markers', () => {
  assert.strictEqual(
    isDisplayNoiseLine(
      "root@6f0b0ae3f99f:/workspace# printf '\\n__GC_DO''NE_14b3e794fb7086e2__%d__\\n' \"$?\"",
    ),
    true,
  );
  assert.strictEqual(isDisplayNoiseLine('__GC_DONE_14b3e794fb7086e2__1__'), true);
  assert.strictEqual(
    isDisplayNoiseLine("Current Working Directory: /workspace"),
    false,
  );
});

test('filterDisplayChunk preserves command output only', () => {
  const chunk =
    "root@box:/workspace# printf '\\n__GC_DO''NE_abc__%d__\\n' \"$?\"\n" +
    '__GC_DONE_abc__0__\n' +
    "Current Working Directory: /workspace\n" +
    "'/workspace/challenges/foo' exists.\n";
  const { text } = filterDisplayChunk('', chunk);
  assert.ok(!text.includes('__GC_DONE_'));
  assert.ok(!text.includes('printf'));
  assert.ok(text.includes('Current Working Directory'));
});
