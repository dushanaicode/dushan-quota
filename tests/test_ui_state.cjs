const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function script(name) {
  const html = fs.readFileSync(path.join(__dirname, '../lib/assets', name), 'utf8');
  const source = html.match(/<script\b[^>]*>([\s\S]*?)<\/script>/i)[1];
  new vm.Script(source, {filename: name});
  return source;
}

function context() {
  const nodes = new Map();
  const node = () => {
    const classes = new Set(), attributes = {}, animations = [];
    return {
      innerHTML: '', style: {}, dataset: {}, textContent: '', animations,
      classList: {
        add(value) {classes.add(value);}, remove(value) {classes.delete(value);}, contains(value) {return classes.has(value);},
        toggle(value, on = !classes.has(value)) {if (on) classes.add(value); else classes.delete(value); return on;},
      },
      querySelectorAll() {return [];},
      setAttribute(key, value) {attributes[key] = value;}, getAttribute(key) {return attributes[key];},
      animate() {const item = {cancelled: false, cancel() {this.cancelled = true;}}; animations.push(item); return item;},
      getAnimations() {return animations.filter(item => !item.cancelled);},
    };
  };
  const get = id => {
    if (!nodes.has(id)) nodes.set(id, node());
    return nodes.get(id);
  };
  return vm.createContext({
    window: {}, document: {body: get('document-body'), getElementById: get, querySelectorAll() {return [];}, addEventListener() {},
      getAnimations() {return [...nodes.values()].flatMap(item => item.getAnimations());}},
    setTimeout() {}, setInterval() {}, esc: String, get,
  });
}

async function main() {
  const row = {source: 'local', harness: 'codex', period: '30d', label: 'Codex',
    total_tokens: 123, breakdown: {input: 100, output: 23}, detail: 'Legacy attribution explanation'};
  const fixture = {title: 'OpenAI', provider: 'openai', identity: 'a', ok: true, windows: [],
    sub_end: '2030-01-02T00:00:00Z', usage: [row], harnesses: [{key: 'codex', label: 'Codex', configured: true}]};
  const grok = {title: 'xAI', provider: 'grok', identity: 'x', ok: true, windows: [],
    usage: [{...row, harness: 'grok_cli', label: 'Grok CLI'}], harnesses: [{key: 'grok_cli', label: 'Grok CLI'}]};
  const saved = [];
  const floating = context();
  vm.runInContext(script('float.html'), floating);
  Object.assign(floating, {fixture, grok, bridge: {
    quota: async () => ({results: [], snapshot: {state: 'fresh'}}), save_settings(raw) {saved.push(JSON.parse(raw));},
  }});
  vm.runInContext("cache = [fixture, grok]; S.show['#usage'] = true; api = bridge;", floating);
  const usage = floating.usageBlock(fixture);
  assert(usage.includes('123 Token'));
  assert(!usage.includes(row.detail));
  assert.deepEqual(Array.from(floating.usageHarnesses(fixture), h => h[0]), ['codex']);
  assert(!usage.includes('grok_cli'));
  assert(!floating.usageBlock(grok).includes('value="codex"'));
  assert(!floating.usageBlock(grok).includes('value="remote"'));
  floating.setUsageHarness('grok:x', 'grok_cli');
  floating.setUsageHarness('openai:a', 'codex');
  assert.equal(saved.at(-1).usage_harnesses['grok:x'], 'grok_cli');
  assert.equal(saved.at(-1).usage_harnesses['openai:a'], 'codex');
  floating.setUsageHarness('openai:a', 'kimi_code');
  assert(floating.usageBlock(fixture).includes('value="all" selected'));
  await floating.refresh();
  assert.equal(vm.runInContext('cache.length', floating), 0, 'Successful empty results must clear old cards');
  vm.runInContext('cache = [fixture]', floating);
  floating.bridge.quota = async () => ({results: [], snapshot: {state: 'error'}});
  await floating.refresh();
  assert.equal(vm.runInContext('cache.length', floating), 1, 'Failed refresh must preserve existing cards');
  vm.runInContext("S.show['#plan'] = false; render([fixture]);", floating);
  assert(!floating.get('list').innerHTML.includes('2030-01-02'));
  floating.applyMotion();
  floating.toggleSettings();
  assert(floating.get('panel').classList.contains('settings-open'));
  assert.equal(floating.get('cfg').animations.length, 0);
  floating.toggleAnimations();
  assert.equal(saved.at(-1).animations, true);
  assert(floating.document.body.classList.contains('motion'));
  assert.equal(floating.get('animationChip').getAttribute('aria-pressed'), 'true');
  assert.equal(floating.get('cfg').animations.length, 1);
  floating.toggleSettings();
  assert(!floating.get('panel').classList.contains('settings-open'));
  assert.equal(floating.get('body').animations.length, 1);
  floating.toggleAnimations();
  assert.equal(saved.at(-1).animations, false);
  assert(!floating.document.body.classList.contains('motion'));
  assert.equal(floating.document.getAnimations().length, 0);

  vm.runInContext("S.animations = true; renderedContent = ''; renderedStructure = '';", floating);
  floating.render([fixture]);
  const listAnimations = floating.get('list').animations.length;
  assert(listAnimations > 0, 'First render plays the entrance animation');
  floating.render([{...fixture, usage: [{...row, total_tokens: 456}]}]);
  assert.equal(floating.get('list').animations.length, listAnimations,
    'Value-only auto-refresh must not replay the entrance animation');
  floating.render([{...fixture, usage: [{...row, total_tokens: 456}]}, grok]);
  assert.equal(floating.get('list').animations.length, listAnimations + 1,
    'Adding a card replays the entrance animation');

  const web = context();
  const source = script('index.html');
  vm.runInContext(source.slice(source.indexOf('function compactNumber'), source.indexOf('async function loadUsageDetail')), web);
  assert.equal(web.exactNumber(null), '\u2014');
  assert.equal(web.exactNumber(undefined), '\u2014');
  assert.equal(web.exactNumber(0), '0');
  assert.equal(web.compactNumber(null), '\u2014');
  web.renderUsageDetail({account: {title: 'OpenAI'}, usage: [row], harnesses: fixture.harnesses});
  const rendered = web.get('usageBody').innerHTML;
  assert(!rendered.includes(row.detail));
  assert(rendered.match(/class="ud-note">([^<]*)/)[1].length <= 20);
  assert(!rendered.includes('data-harness="grok_cli"'));
  web.renderUsageDetail({account: {title: 'OpenAI'}, usage: [], harnesses: [{key: 'opencode', label: 'OpenCode', configured: true}]});
  assert(web.get('usageBody').innerHTML.includes('data-harness="opencode"'));
  console.log('UI checks passed: account-scoped clients, independent filters, animations, settings, empty states, and metrics.');
}

main().catch(error => {console.error(error); process.exitCode = 1;});
