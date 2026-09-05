const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function panel() {
  class Element {
    constructor(tag) { this.tagName = tag; this.children = []; this.dataset = {}; this.handlers = {}; this.className = ''; this.value = ''; this.disabled = false; this._text = ''; this.scrollHeight = this.scrollTop = this.clientHeight = 0; }
    get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
    set textContent(value) { this._text = value; this.children = []; }
    get firstChild() { return this.children[0]; }
    get lastChild() { return this.children.at(-1); }
    appendChild(child) { child.parent = this; this.children.push(child); return child; }
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); }
    addEventListener(type, fn) { this.handlers[type] = fn; }
    get classList() { return { add: name => this.className += ' ' + name, remove: name => this.className = this.className.split(' ').filter(n => n !== name).join(' '), contains: name => this.className.split(' ').includes(name) }; }
    querySelectorAll(selector) { return this.children.flatMap(child => [...((selector.startsWith('.') ? child.className.split(' ').includes(selector.slice(1)) : child.tagName === selector) ? [child] : []), ...child.querySelectorAll(selector)]); }
  }
  const elements = new Map();
  const sent = [], listeners = {};
  const doc = { getElementById: id => { if (!elements.has(id)) elements.set(id, new Element('div')); return elements.get(id); }, createElement: tag => new Element(tag), createTextNode: text => { const node = new Element('#text'); node.textContent = text; return node; } };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../../media/main.js'), 'utf8'), {
    document: doc, window: { addEventListener: (name, fn) => listeners[name] = fn },
    acquireVsCodeApi: () => ({ postMessage: msg => sent.push(msg), getState: () => ({ draft: 'Saved draft' }), setState: () => {} }),
  });
  return { get: id => doc.getElementById(id), sent, post: data => listeners.message({ data }), event: event => listeners.message({ data: { type: 'event', event } }) };
}

test('model catalog respects the current selection and session settings', () => {
  const p = panel();
  p.post({ type: 'header', provider: 'local', model: 'two', mode: 'plan', role: 'reviewer' });
  p.event({ type: 'catalog', models: [{ name: 'One', provider: 'local', model: 'one' }, { name: 'Two', provider: 'local', model: 'two' }] });
  assert.equal(p.get('model-select').children.length, 2);
  assert.equal(p.get('model-select').value, JSON.stringify(['local', 'two']));
  assert.equal(p.get('mode-select').value, 'plan');
  assert.equal(p.get('role-select').value, 'reviewer');
});

test('canonical parts replace text rather than duplicating bubbles or executing HTML', () => {
  const p = panel();
  for (const text of ['Hello', 'Hello <script>bad()</script>']) p.event({ type: 'message_part', id: 'part', message_id: 'msg', text });
  assert.equal(p.get('log').children.length, 1);
  assert.equal(p.get('log').children[0].textContent, 'Hello <script>bad()</script>');
  assert.equal(p.get('log').querySelectorAll('script').length, 0);
  p.event({ type: 'message_remove', message_id: 'msg' });
  assert.equal(p.get('log').children.length, 0);
});

test('panel replay and pending approvals cannot make an active task look idle', () => {
  const p = panel();
  p.post({ type: 'busy', busy: true });
  const approval = { type: 'confirm_request', request_id: 'request', detail: 'echo hello' };
  p.event(approval);
  assert.equal(p.get('model-select').disabled, true);
  p.event({ type: 'history_v2', events: [approval, { type: 'approval_resolved', request_id: 'request' }] });
  assert.equal(p.get('model-select').disabled, true);
  assert.ok(p.get('log').querySelectorAll('button').every(button => button.disabled));
  p.event({ type: 'turn_end' });
  assert.equal(p.get('model-select').disabled, false);
});

test('code blocks copy exact content and an IME Enter does not submit', () => {
  const p = panel();
  p.event({ type: 'message_part', id: 'part', message_id: 'msg', text: 'Example:\n```py\nprint("hello")\n```' });
  p.get('log').querySelectorAll('button')[0].onclick();
  assert.equal(p.sent.at(-1).type, 'copy');
  assert.equal(p.sent.at(-1).text, 'print("hello")\n');
  const count = p.sent.length;
  p.get('input').handlers.keydown({ key: 'Enter', isComposing: true });
  assert.equal(p.sent.length, count);
  assert.equal(p.get('input').value, 'Saved draft');
});
