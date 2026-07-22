'use strict';

const assert = require('assert');
const { IsolatedProcessManager } = require('../isolated_process_manager');
const { isInteractiveCommand } = require('../interactive_heuristic');

async function run() {
  assert.strictEqual(isInteractiveCommand('ssh user@host'), true);
  assert.strictEqual(isInteractiveCommand('sshpass -p x ssh user@host'), false);
  assert.strictEqual(isInteractiveCommand('curl http://x'), false);
  console.log('  ok - isInteractiveCommand heuristics');

  const mgr = new IsolatedProcessManager();
  const res = await mgr.start('s1', 'echo hello-isolated', process.cwd(), {
    block: true,
    timeoutMs: 5000,
  });
  assert.ok(res.stdout.includes('hello-isolated'));
  assert.ok(!res.stdout.includes('__GC_DONE_'));
  assert.strictEqual(res.running, false);
  assert.ok(res.exit_code === 0);
  console.log('  ok - start without sentinel');

  const res2 = await mgr.start(
    's2',
    'read -r line; echo "got:$line"',
    process.cwd(),
    { block: false, timeoutMs: 3000 }
  );
  assert.strictEqual(res2.running, true);
  const pid = res2.proc_id;
  mgr.write(pid, 'secret', { appendNewline: true });
  await new Promise((r) => setTimeout(r, 500));
  const out = mgr.read(pid, 0);
  assert.ok(out.stdout.includes('got:secret') || out.stdout.includes('secret'));
  mgr.kill(pid);
  console.log('  ok - write stdin');

  console.log('isolated_process_manager tests passed');
}

run().catch((e) => {
  console.error(e);
  process.exit(1);
});
