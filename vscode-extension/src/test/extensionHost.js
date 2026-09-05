const vscode = require('vscode');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');

exports.run = async () => {
  const results = [];
  const requests = [];
  let scenario = 'ask', activeRequests = 0;
  const sockets = new Set();
  const server = http.createServer(async (req, res) => {
    activeRequests++;
    res.once('close', () => activeRequests--);
    let raw = '';
    for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw);
    requests.push(body);
    if (scenario === 'stall') return; // Stop must abort this real in-flight model HTTP call.
    const lastUser = body.messages.findLastIndex(m => m.role === 'user');
    const names = body.messages.slice(lastUser + 1).flatMap(m => (m.tool_calls || []).map(c => c.function.name));
    let name = '', args = {};
    if (scenario === 'build') {
      if (!names.includes('read')) { name = 'read'; args = { filePath: 'calc.py', offset: 1, limit: 100 }; }
      else if (!names.includes('edit')) { name = 'edit'; args = { filePath: 'calc.py', oldString: 'return a - b', newString: 'return a + b' }; }
    }
    if (scenario === 'approval' && !names.includes('bash')) {
      name = 'bash'; args = { command: 'echo apprentice-approval-test', description: 'Print a test marker' };
    }
    const delta = { role: 'assistant', ...(name ? { tool_calls: [{ index: 0, id: `call_${requests.length}`, type: 'function', function: { name, arguments: JSON.stringify(args) } }] }
      : { content: scenario === 'ask' ? 'This is a concise final answer about your application.' : 'The requested change is ready for independent verification.' }) };
    const common = { id: `chat_${requests.length}`, object: 'chat.completion.chunk', created: 1, model: body.model };
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    res.end([
      { ...common, choices: [{ index: 0, delta, finish_reason: null }] },
      { ...common, choices: [{ index: 0, delta: {}, finish_reason: name ? 'tool_calls' : 'stop' }], usage: { prompt_tokens: 100, completion_tokens: 20, total_tokens: 120 } },
    ].map(chunk => `data: ${JSON.stringify(chunk)}\n\n`).join('') + 'data: [DONE]\n\n');
  });
  server.on('connection', socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const config = {
    providers: { default: 'fixture', fixture: { enabled: true, kind: 'openai-compatible', base_url: `http://127.0.0.1:${server.address().port}/v1`, models: { one: 'fixture-one', two: 'fixture-two' } } },
    agent_chat: { backend: 'opencode', use_corrections: false, verify: 'tests' },
    gate: { enabled: false }, metering: { enabled: false }, opencode: { max_attempts: 2, task_timeout_s: 60 },
  };
  fs.mkdirSync(path.join(process.env.APPRENTICE_HOME, 'config'), { recursive: true });
  fs.writeFileSync(path.join(process.env.APPRENTICE_HOME, 'config/qwen.json'), JSON.stringify(config));
  const until = async (condition, label, ms = 45000) => {
    const deadline = Date.now() + ms;
    while (!condition()) {
      if (Date.now() > deadline) throw new Error(`Timed out: ${label}`);
      await new Promise(resolve => setTimeout(resolve, 50));
    }
  };
  let view;
  try {
    const extension = vscode.extensions.getExtension('m-555.apprentice-vscode');
    assert.ok(extension, 'Development extension is installed');
    view = (await extension.activate()).testController;
    assert.ok(view, 'Test-only controller API');
    const receive = view.onAgentEvent.bind(view);
    view.onAgentEvent = event => {
      fs.appendFileSync(path.join(process.env.APPRENTICE_TEST_ROOT, 'events.jsonl'), JSON.stringify(event) + '\n');
      receive(event);
    };
    const folder = vscode.workspace.workspaceFolders[0];
    assert.equal(await view.ensureStarted(folder), true);
    await until(() => view.replay.some(e => e.type === 'catalog'), 'model catalog');
    assert.equal(view.replay.find(e => e.type === 'catalog').models.length, 2);
    const sessionID = view.sessionInfo.id;
    results.push('real extension activation and model catalog');

    // Exercise the actual webview host message handler, not a second UI backend.
    await vscode.commands.executeCommand('apprentice.openPanel');
    await view.onMessage({ type: 'select', provider: 'fixture', model: 'two' });
    await until(() => view.sessionInfo.model === 'two', 'selected model acknowledged');
    await view.sendUser('Explain my app.\nDo not edit code.');
    await until(() => !view.inTurn, 'Ask completion');
    assert.equal(requests.at(-1).model, 'fixture-two');
    assert.ok(view.replay.some(e => e.type === 'message_part' && e.text.includes('concise final answer')));
    assert.ok(view.replay.some(e => e.type === 'user' && e.text.includes('\n')));
    assert.match(fs.readFileSync(path.join(folder.uri.fsPath, 'calc.py'), 'utf8'), /a - b/);
    results.push('Ask final answer, selected model actually used, multiline input, no file changes');

    scenario = 'build';
    await view.onMessage({ type: 'select', mode: 'build', role: 'implementer' });
    await until(() => view.sessionInfo.mode === 'build', 'Build acknowledged');
    await view.sendUser('Fix add in calc.py.');
    await until(() => !view.inTurn, 'Build completion');
    const delivery = view.replay.find(e => e.type === 'delivery' && e.applied);
    assert.ok(delivery, JSON.stringify(view.replay));
    assert.ok(delivery.done_passed);
    assert.match(fs.readFileSync(path.join(folder.uri.fsPath, 'calc.py'), 'utf8'), /a \+ b/);
    await view.onMessage({ type: 'openDiff', path: 'calc.py' });
    assert.ok(vscode.workspace.textDocuments.some(d => d.uri.scheme === 'apprentice-task' && d.getText().includes('a - b')));
    results.push('Build through real OpenCode read/edit tools, independent tests, task-specific VS Code diff');

    await view.resume(sessionID);
    await until(() => view.replay.some(e => e.type === 'delivery'), 'resume history');
    assert.equal(view.sessionInfo.model, 'two');
    assert.equal(view.sessionInfo.mode, 'build');
    assert.ok(view.changed.has('calc.py'));
    assert.ok(view.manifests.has('calc.py'));
    await view.onMessage({ type: 'ready' });
    assert.equal(view.inTurn, false);
    results.push('Resume restores selection, native session, transcript and task diff; panel-ready does not invent busy state');

    scenario = 'approval';
    await view.sendUser('Print a test marker with the shell.');
    await until(() => view.replay.some(e => e.type === 'confirm_request'), 'shell approval');
    const approval = view.replay.findLast(e => e.type === 'confirm_request');
    assert.equal(view.inTurn, true);
    await view.onMessage({ type: 'ready' });
    assert.equal(view.inTurn, true, 'reload must retain running/approval state');
    await view.onMessage({ type: 'confirm', request_id: approval.request_id, allow: false });
    await until(() => !view.inTurn, 'denied approval completion');
    results.push('Shell approval survives panel reload and denial routes by request ID');

    scenario = 'stall';
    await view.sendUser('Wait for a slow response.');
    await until(() => activeRequests > 0, 'active model request');
    view.stop();
    await until(() => !view.inTurn && activeRequests === 0, 'Stop drains the upstream HTTP request', 20000);
    results.push('Stop cancels real OpenCode and releases its upstream request before reporting idle');

    scenario = 'ask';
    await Promise.all([view.newSession(), view.newSession()]);
    assert.notEqual(view.sessionInfo.id, sessionID);
    assert.equal(view.changed.size, 0);
    assert.equal(view.sessionInfo.mode, 'ask');
    await view.sendUser('Hello in the new session.');
    await until(() => !view.inTurn, 'new session completion');
    assert.ok(view.replay.some(e => e.type === 'message_part'));
    results.push('Concurrent New Session requests settle into one usable clean session');
    fs.writeFileSync(path.join(process.env.APPRENTICE_TEST_ROOT, 'result.json'), JSON.stringify({ passed: true, results, modelRequests: requests.length }));
  } catch (error) {
    fs.writeFileSync(path.join(process.env.APPRENTICE_TEST_ROOT, 'result.json'), JSON.stringify({ passed: false, results, error: error.stack, events: view?.replay, modelRequests: requests.length }));
    throw error;
  } finally {
    if (view) await view.agent.stop();
    for (const socket of sockets) socket.destroy();
    await new Promise(resolve => server.close(resolve));
  }
};
