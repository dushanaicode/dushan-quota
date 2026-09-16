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

function context(storage = new Map()) {
  const nodes = new Map();
  const node = () => {
    const classes = new Set(), attributes = {}, animations = [];
    return {
      innerHTML: '', style: {}, dataset: {}, textContent: '', animations,
      focus() {this.focused = true;},
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
    if (!nodes.has(id)) nodes.set(id, {...node(), id});
    return nodes.get(id);
  };
  return vm.createContext({
    window: {}, document: {body: get('document-body'), getElementById: get, querySelectorAll() {return [];}, addEventListener() {},
      getAnimations() {return [...nodes.values()].flatMap(item => item.getAnimations());}},
    setTimeout() {}, setInterval() {}, esc: String, get,
    localStorage: {getItem(key) {return storage.get(key) ?? null;}, setItem(key, value) {storage.set(key, String(value));}},
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
  const pausedClaude = {title: 'Claude Code', provider: 'claude', identity: 'c', ok: true,
    windows: [{name: '5 小时额度', remaining_percent: 88}],
    error: '', notice: '账号信息暂不可用：暂时被限流（429）', retry_at: new Date(2030, 0, 1, 9, 5).getTime() / 1000};
  const limitedClaude = {...pausedClaude, ok: false, windows: [], notice: '令牌续期暂时被限流（429）'};
  const floating = context();
  vm.runInContext(script('float.html'), floating);
  const now = Date.UTC(2030, 0, 1);
  const resetTs = now / 1000 + 6 * 86400 + 5 * 3600 + 9 * 60;
  assert.equal(floating.quotaCountdown(resetTs, now), '6d05h09m');
  assert.equal(floating.quotaCountdown(resetTs, now + 60000), '6d05h08m');
  assert.equal(floating.quotaCountdown(now / 1000, now), '待重置');
  assert.equal(floating.quotaCountdown(now / 1000 + 30, now), '<1m');
  const quotaRow = floating.row({name:'Week quota', remaining_percent:99.9, reset_ts:resetTs});
  assert(quotaRow.includes('99.9%'), 'Partial quota must not round up to 100%');
  assert(!quotaRow.includes('class="reset-at"'), 'No duplicate reset line');
  assert(quotaRow.includes(`data-reset-ts="${resetTs}"`));
  assert(quotaRow.includes('额度重置时间：') && !quotaRow.includes('本地'));
  assert(quotaRow.includes('onclick="toggleResetTime(this)"') && quotaRow.includes('<button type="button"'));
  assert(floating.row({name:'Week quota', remaining_percent:0}).includes('>0%</span>'));
  assert(floating.row({name:'Week quota', remaining_percent:100}).includes('>100%</span>'));
  assert(!floating.row({name:'Week quota', remaining_percent:80, reset_ts:null}).includes('reset-at'));
  assert(!floating.row({name:'Week quota', remaining_percent:80, reset_ts:'bad'}).includes('Invalid Date'));
  const resetKey = JSON.stringify(['openai:a', '周额度']);
  const attributes = {};
  const countdownNode = {dataset: {resetTs: String(resetTs), resetKey}, textContent:'', setAttribute(key, value){attributes[key]=value;}};
  const queryAll = floating.document.querySelectorAll;
  floating.document.querySelectorAll = () => [countdownNode];
  floating.updateCountdowns();
  assert.equal(countdownNode.textContent, floating.quotaCountdown(resetTs));
  floating.toggleResetTime(countdownNode);
  const exactReset = countdownNode.textContent;
  assert(exactReset.startsWith('重置于 2030-01-'));
  assert.equal(attributes['aria-pressed'], 'true');
  floating.updateCountdowns();
  assert.equal(countdownNode.textContent, exactReset, 'Timer must preserve the selected date view');
  assert(floating.row({name:'周额度', remaining_percent:95, reset_ts:resetTs}, 'openai:a').includes('>重置于 2030-01-'), 'Refresh retains this row selection');
  assert(!floating.row({name:'周额度', remaining_percent:95, reset_ts:resetTs}, 'openai:b').includes('>重置于'), 'Other accounts retain their own selection');
  floating.toggleResetTime(countdownNode);
  assert.equal(attributes['aria-pressed'], 'false');
  assert.equal(countdownNode.textContent, floating.quotaCountdown(resetTs));
  floating.document.querySelectorAll = queryAll;
  Object.assign(floating, {fixture, grok, bridge: {
    quota: async () => ({results: [], snapshot: {state: 'fresh'}}), save_settings(raw) {saved.push(JSON.parse(raw));},
  }});
  vm.runInContext("cache = [fixture, grok]; S.show['#usage'] = true; api = bridge;", floating);
  floating.toggleResetTime(countdownNode);
  assert.equal(saved.at(-1).reset_modes[resetKey], true, 'The reset display preference is saved');
  const usage = floating.usageBlock(fixture);
  assert(usage.includes('123 Token'));
  assert.equal((usage.match(/<select /g) || []).length, 2);
  assert(usage.includes('aria-label="时间范围"') && usage.includes('aria-label="客户端"'));
  assert(usage.includes('value="3d"') && !usage.includes('usage-periods'));
  floating.setUsagePeriod('3d');
  const threeDayFixture = {...fixture, usage: [row, {...row, period: '3d', total_tokens: 45}]};
  assert(floating.usageBlock(threeDayFixture).includes('45 Token'));
  assert(!floating.usageBlock(threeDayFixture).includes('123 Token'));
  assert(floating.usageBlock(threeDayFixture).includes('value="3d" selected'));
  assert.equal(saved.at(-1).usage_period, '3d');
  floating.setUsagePeriod('30d');
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

  // Float window: cards can be dragged into an order that outlives a refresh.
  vm.runInContext("S.card_order = ['openai:a']", floating);
  assert.equal(floating.orderedItems([grok, fixture]).map(it => it.title).join('|'), 'OpenAI|xAI',
    'A saved order wins over the payload order');
  vm.runInContext("S.card_order = []", floating);
  assert.equal(floating.orderedItems([grok, fixture]).map(it => it.title).join('|'), 'xAI|OpenAI',
    'Without a saved order the payload order stands');
  const dragged = {dataset: {key: 'openai:a'}, classList: {add() {}, remove() {}}, releasePointerCapture() {}};
  floating.get('list').children = [{dataset: {key: 'grok:x'}}, dragged];
  vm.runInContext("S.card_order = ['openai:a', 'kimi:hidden']", floating);
  vm.runInContext("cardDrag = {card: get('list').children[1], moved: true, pointerId: 1}", floating);
  floating.cardDragEnd();
  assert.equal(vm.runInContext("S.card_order.join('|')", floating), 'grok:x|openai:a|kimi:hidden',
    'Dropping saves the visible order and keeps hidden cards queued behind it');
  assert.equal(saved.at(-1).card_order.join('|'), 'grok:x|openai:a|kimi:hidden', 'The order is persisted');
  vm.runInContext("cardDrag = {card: get('list').children[1], moved: false, pointerId: 1}", floating);
  floating.cardDragEnd();
  assert.equal(vm.runInContext("S.card_order.join('|')", floating), 'grok:x|openai:a|kimi:hidden',
    'A click that never moved must not rewrite the order');
  floating.resetCardOrder();
  assert.equal(vm.runInContext("S.card_order.length", floating), 0, 'Reset restores the payload order');

  vm.runInContext("S.show['#plan'] = true", floating);
  for (const plan of ['OpenAI (Pro 5x)', 'OpenAI (Pro 20x)', 'Claude Max 5x', 'Claude Max 20x']) {
    floating.render([{...fixture, plan}]);
    assert(floating.get('list').innerHTML.includes(`>${plan}</span>`),
      `The float card must show ${plan} in its visible badge`);
  }

  const storage = new Map();
  floating.render([pausedClaude]);
  assert(floating.get('list').innerHTML.includes('88%'));
  assert(floating.get('list').innerHTML.includes('<div class="note">账号信息暂不可用：暂时被限流（429），显示上次数据，09:05 自动重试</div>'),
    'A temporary failure keeps valid quota and says when it retries');
  assert(!floating.get('list').innerHTML.includes('class="err"'), 'A temporary failure is not shown as an account error');
  floating.render([limitedClaude]);
  assert(floating.get('list').innerHTML.includes('<div class="note">令牌续期暂时被限流（429），09:05 自动重试</div>'));
  assert(!floating.get('list').innerHTML.includes('class="err"'));
  const web = context(storage);
  const source = script('index.html');
  const usageSource = source.slice(source.indexOf('function compactNumber'), source.indexOf('function renderSide'));
  vm.runInContext(usageSource, web);
  assert.equal(web.exactNumber(null), '\u2014');
  assert.equal(web.exactNumber(undefined), '\u2014');
  assert.equal(web.exactNumber(0), '0');
  assert.equal(web.compactNumber(null), '\u2014');
  web.renderUsageDetail({account: {title: 'OpenAI'}, usage: [row], harnesses: fixture.harnesses});
  const rendered = web.get('usageBody').innerHTML;
  assert.equal((rendered.match(/<select /g) || []).length, 2);
  assert(rendered.includes('id="usagePeriod"') && rendered.includes('id="usageHarness"'));
  assert(rendered.includes('value="3d"') && !rendered.includes('data-period='));
  const detail = {account: fixture, usage: threeDayFixture.usage, harnesses: fixture.harnesses};
  web.renderUsageDetail(detail);
  web.get('usagePeriod').value = '3d';
  web.get('usageHarness').value = 'codex';
  web.get('usagePeriod').onchange({target: web.get('usagePeriod')});
  assert(web.get('usageBody').innerHTML.includes('45 Token'));
  assert(!web.get('usageBody').innerHTML.includes('123 Token'));
  assert(web.get('usagePeriod').focused, 'Changing the dropdown must retain keyboard focus');
  assert.equal(storage.get('quota-usage-period'), '3d');
  assert.equal(storage.get('quota-usage-harness:openai:a'), 'codex');

  // Reset time in the web UI: the same countdown/absolute toggle the float window has.
  const clockStorage = new Map();
  const rowSource = source.slice(source.indexOf('function textRowClass'), source.indexOf('function fmtActivation'));
  const bootClock = store => {
    const ctx = context(store);
    vm.runInContext("const colorOf = p => p >= 40 ? 'g' : p >= 15 ? 'y' : 'r';", ctx);
    vm.runInContext(rowSource, ctx);
    return ctx;
  };
  const clock = bootClock(clockStorage);
  const webRow = clock.winRow({name: '周额度', remaining_percent: 95, reset_ts: resetTs}, 'openai:a');
  assert(webRow.includes('<button type="button" class="rst rst-toggle"'), 'Reset time is a button, not a bare help cursor');
  assert(webRow.includes('onclick="toggleResetTime(this)"') && webRow.includes('点击切换重置时间'));
  assert(!clock.winRow({name: '周额度', remaining_percent: 95, reset_ts: null}, 'openai:a').includes('rst-toggle'),
    'Windows without a reset time stay plain text');
  const webAttributes = {};
  const webNode = {dataset: {resetTs: String(resetTs), resetKey: 'openai:a|周额度'}, textContent: '',
    setAttribute(key, value) {webAttributes[key] = value;}};
  clock.toggleResetTime(webNode);
  assert(webNode.textContent.startsWith('重置于 2030-01-'));
  assert.equal(webAttributes['aria-pressed'], 'true');
  clock.document.querySelectorAll = () => [webNode];
  clock.updateCountdowns();
  assert(webNode.textContent.startsWith('重置于 2030-01-'), 'The ticker leaves a row pinned to absolute time alone');
  assert(clock.winRow({name: '周额度', remaining_percent: 95, reset_ts: resetTs}, 'openai:a').includes('>重置于 2030-01-'),
    'A refresh keeps the row selection');
  assert(!clock.winRow({name: '周额度', remaining_percent: 95, reset_ts: resetTs}, 'openai:b').includes('>重置于'),
    'Other accounts keep their own selection');
  clock.toggleResetTime(webNode);
  assert.equal(webAttributes['aria-pressed'], 'false');
  assert(webNode.textContent.includes(' · '), 'The countdown view keeps the exact time beside it');
  assert.equal(vm.runInContext("resetModes['openai:a|周额度']", bootClock(clockStorage)), false,
    'The reset display preference survives a reload');

  const reloaded = context(storage);
  vm.runInContext(usageSource, reloaded);
  reloaded.loadUsageDetail = () => {};
  reloaded.openUsage(fixture);
  reloaded.renderUsageDetail(detail);
  assert(reloaded.get('usageBody').innerHTML.includes('value="3d" selected'));
  assert(reloaded.get('usageBody').innerHTML.includes('value="codex" selected'));
  reloaded.openUsage(grok);
  assert.equal(vm.runInContext('usageHarness', reloaded), 'all', 'Client selections must not leak to other accounts');
  reloaded.openUsage(fixture);
  assert.equal(vm.runInContext('usageHarness', reloaded), 'codex', 'Reopening an account retains its client');
  vm.runInContext("usageHarness = 'remote'", reloaded);
  reloaded.renderUsageDetail(detail);
  assert.equal(vm.runInContext('usageHarness', reloaded), 'all', 'Unavailable saved clients fall back to all');

  const invalidSaved = context(new Map([['quota-usage-period', 'invalid']]));
  vm.runInContext(usageSource, invalidSaved);
  assert.equal(vm.runInContext('usagePeriod', invalidSaved), '30d');
  const noStorage = context();
  noStorage.localStorage = {getItem() {throw Error('Storage unavailable');}, setItem() {throw Error('Storage unavailable');}};
  vm.runInContext(usageSource, noStorage);
  noStorage.renderUsageDetail(detail);
  noStorage.get('usagePeriod').value = '3d';
  noStorage.get('usageHarness').value = 'all';
  noStorage.get('usagePeriod').onchange({target: noStorage.get('usagePeriod')});
  assert(noStorage.get('usageBody').innerHTML.includes('45 Token'), 'Filtering still works without storage');
  vm.runInContext("usagePeriod = '30d'", web);
  assert(!rendered.includes(row.detail));
  assert(rendered.match(/class="ud-note">([^<]*)/)[1].length <= 20);
  assert(!rendered.includes('value="grok_cli"'));
  web.renderUsageDetail({account: {title: 'OpenAI'}, usage: [], harnesses: [{key: 'opencode', label: 'OpenCode', configured: true}]});
  assert(web.get('usageBody').innerHTML.includes('value="opencode"'));
  const auth = context(), authCalls = [];
  const authSource = script('index.html').split('/* ---------- add account')[1].split('/* ---------- sync to harness')[0];
  vm.runInContext('/* ---------- add account' + authSource, auth);
  auth.api = async (url, options) => {
    authCalls.push({url, body: options?.body && JSON.parse(options.body)});
    if (url.endsWith('/start')) return {login_id:'claude-login', verification_uri_complete:'https://claude.com/cai/oauth/authorize?state=test'};
    return {status:'ok'};
  };
  auth.refreshQuota = () => {};
  vm.runInContext("addProv = 'claude'; selectMode('oauth');", auth);
  assert(auth.get('fields').innerHTML.includes('授权码'));
  await auth.submitAdd();
  assert(auth.get('oauthBox').innerHTML.includes('id="oauthCode"'));
  assert(auth.get('oauthBox').innerHTML.includes('打开授权页面'));
  assert.equal(auth.get('btnAdd').disabled, false);
  assert.equal(auth.get('btnAdd').textContent, '完成授权');
  assert(!authCalls.some(call => call.url.includes('/poll')), 'Claude uses manual completion, without device polling');
  auth.get('oauthCode').value = 'sample-code#test';
  await auth.submitAdd();
  assert.deepEqual(authCalls.at(-1), {url:'/api/oauth/claude/complete', body:{login_id:'claude-login', code:'sample-code#test'}});
  assert.equal(vm.runInContext('oauthLogin', auth), null);
  await auth.submitAdd();
  auth.selectMode('local');
  assert.equal(authCalls.at(-1).url, '/api/oauth/claude/cancel', 'Switching modes cancels the previous login');
  assert.equal(auth.get('btnAdd').disabled, false);
  let resolveStart;
  auth.api = (url, options) => url.endsWith('/start') ? new Promise(resolve => {resolveStart = resolve;}) : Promise.resolve(authCalls.push({url, body:JSON.parse(options.body)}));
  auth.selectMode('oauth');
  const starting = auth.startOAuth('claude');
  auth.closeAdd();
  resolveStart({login_id:'late-login', verification_uri_complete:'https://claude.com'});
  await starting;
  assert.equal(authCalls.at(-1).body.login_id, 'late-login', 'A dismissed dialog cannot leave a late OAuth session active');
  console.log('UI checks passed: dropdowns, saved filters, account-scoped clients, keyboard focus, animations, empty states, metrics, and Claude OAuth.');
}

main().catch(error => {console.error(error); process.exitCode = 1;});
