/* Offline UI behavior with a minimal DOM/Fetch double. Real-browser checks use a throwaway local instance. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.dataset = {}; this.attributes = {};
    this.listeners = {}; this.value = ''; this.hidden = false; this.checked = false;
    this.classList = {toggle() {}, add() {}};
  }
  get textContent() { return (this._text || '') + this.children.map(child => child.textContent || '').join(''); }
  set textContent(value) { this._text = value == null ? '' : String(value); this.children = []; }
  set innerHTML(value) { throw new Error('Untrusted HTML must never be inserted'); }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this.children = children; }
  focus() { this.focused = true; }
  getAttribute(name) { return this.attributes[name]; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  querySelectorAll(selector) {
    const matches = element => {
      if (selector === 'button') return element.tagName === 'button';
      if (selector === 'dialog[open]') return element.tagName === 'dialog' && element.open;
      if (selector === 'form.integration') return element.tagName === 'form' && element.className === 'integration';
      if (selector === '[data-kind]') return Boolean(element.dataset.kind);
      if (selector === '[data-secret]') return Boolean(element.dataset.secret);
      if (selector === '[data-agent-token]') return Boolean(element.dataset.agentToken);
      return false;
    };
    const result = [];
    const walk = element => {
      for (const child of element.children) { if (matches(child)) result.push(child); walk(child); }
    };
    walk(this);
    return result;
  }
  querySelector() { return new Element('button'); }
  reset() {}
  showModal() { this.open = true; }
  close() { this.open = false; this.listeners.close?.(); }
}
function response(data, status = 200) {
  return {ok: status < 400, status, json: async () => status < 400 ? {data} : {error: {message: data}}};
}
async function harness() {
  const elements = new Map();
  const ctx = vm.createContext({
    document: {
      getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
      createElement: tag => new Element(tag), createElementNS: (_, tag) => new Element(tag),
      querySelectorAll: selector => selector === 'button' || selector === 'dialog[open]' ?
        [...new Set([...elements.values()].flatMap(el => descendants(el)).filter(el =>
          selector === 'button' ? el.tagName === 'button' : el.tagName === 'dialog' && el.open))] :
        elements.get('settings-fields')?.querySelectorAll(selector) || [],
      addEventListener() {}, hidden: false,
    },
    window: {addEventListener() {}}, location: {pathname: '/', search: '', hash: ''},
    URL, URLSearchParams, AbortController, DOMException, console, confirm: () => true,
    setTimeout: () => 1, clearTimeout() {},
    fetch: async () => response('Authentication is required.', 401),
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../dashboard/static/dashboard.js'), 'utf8'), ctx);
  await new Promise(resolve => setImmediate(resolve));
  vm.runInContext("state.csrf='session-csrf';", ctx);
  return {ctx, elements, run: code => vm.runInContext(code, ctx)};
}
function target() {
  return {job_id: 7, state: 'DONE', status: {message: 'Completed'}, label: {message: 'Reserve episode'},
    cancellation: {operation_id: 3, state: 'ACTIVE'}, acquisition_status: {message: 'Not staged'},
    actions: {keep: {enabled: false, execution_available: false, availability_reason: {message: 'Keeping videos is not available yet.'}},
      cancel: {enabled: true, disabled_reason: null}, cleanup: {enabled: false}},
    release_name: '<img src=x onerror=alert(1)>', is_management_action: false};
}
test('Intake groups acquisitions in one series row', async () => {
  const h = await harness();
  h.ctx.series = {anidb_id: 123, title: 'Example Anime', staged_count: 2,
    kept_count: 0, missing_count: 1};
  h.run('state.jobs=[series]; renderJobs();');
  const rows = h.elements.get('intake-rows').children;
  assert.equal(rows.length, 1);
  assert.equal(rows[0].children[1].children[0].textContent, 'Example Anime');
  assert.equal(rows[0].children[2].textContent, '2 staged');
  assert.equal(rows[0].children[5].children[0].textContent, 'details');
});

test('series Reject sends the frozen operations and records the operator reason', async () => {
  const h = await harness();
  const calls = [];
  h.ctx.fetch = async (url, options = {}) => {
    calls.push({url, options});
    if (url === '/api/v1/intake/series/123') {
      return response({anidb_id: 123, title: 'Example Anime', english_title: null,
        episodes: [], missing: [{anidb_episode: 6, season: 2, episode: 6, status: 'NOT_AIRED', can_retry: true}],
        keep_operations: [11, 12], reject_operations: [11, 12], rejection: null});
    }
    if (url === '/api/v1/intake/series/123/reject') {
      return response({job_id: 900, message: 'Series rejection recorded; cleanup queued.'}, 202);
    }
    return response({items: [], next_before_id: null});
  };
  await h.run('openSeries(123)');
  const button = h.elements.get('series-reject');
  h.ctx.document.getElementById('series-reject-reason').value = 'Not interested';
  h.ctx.document.getElementById('series-reject-note').value = 'Not my cup of tea';
  await button.onclick();
  const posts = calls.filter(call => call.options.method === 'POST');
  assert.equal(posts.length, 1);
  assert.equal(posts[0].url, '/api/v1/intake/series/123/reject');
  assert.deepEqual(JSON.parse(posts[0].options.body), {
    operation_ids: [11, 12], reason: 'Not interested', note: 'Not my cup of tea'
  });
  assert.equal(posts[0].options.headers['X-CSRF-Token'], 'session-csrf');
});

test('disabled buttons explain the running job before unavailable Keep execution', async () => {
  const h = await harness(); h.ctx.fixture = target();
  h.ctx.fixture.actions.keep.disabled_reason = {message: 'A job is already in progress.'};
  const wrapper = h.run("actionButton(fixture,'keep','Keep')");
  assert.equal(wrapper.children[0].disabled, true);
  assert.equal(wrapper.title, 'A job is already in progress.');
  assert.equal(wrapper.tabIndex, 0);
});
test('double clicks send one CSRF-protected cancellation and reconcile with GET', async () => {
  const h = await harness(); h.ctx.fixture = target();
  let release; const waiting = new Promise(resolve => { release = resolve; });
  const calls = [];
  h.ctx.fetch = async (url, options) => {
    calls.push({url, options});
    if (options.method === 'POST') {
      await waiting;
      return response({notification: {message: 'Job submitted.'}, job_id: 8});
    }
    return response({items: [], next_before_id: null});
  };
  const first = h.run("submitAction(fixture,'cancel')");
  await h.run("submitAction(fixture,'cancel')");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.headers['X-CSRF-Token'], 'session-csrf');
  assert.equal(calls[0].options.credentials, 'same-origin');
  assert.deepEqual(JSON.parse(calls[0].options.body), {});
  release(); await first;
  assert.equal(calls.filter(c => c.options.method === 'POST').length, 1);
  assert.ok(calls.some(c => c.url.startsWith('/api/v1/intake/series?')));
  assert.equal(h.elements.get('notice').textContent, 'Job submitted.');
});
test('uncertain mutation is never automatically replayed', async () => {
  const h = await harness(); h.ctx.fixture = target();
  let posts = 0;
  h.ctx.fetch = async (url, options) => {
    if (options.method === 'POST') { posts += 1; throw new Error('lost response'); }
    return response({items: [], next_before_id: null});
  };
  await h.run("submitAction(fixture,'cancel')");
  assert.equal(posts, 1);
  assert.match(h.elements.get('notice').textContent, /Refresh the job status/);
});
test('release text is rendered as text and Keep never submits while execution is unavailable', async () => {
  const h = await harness(); h.ctx.fixture = target();
  h.run("state.page='jobs'; state.jobs=[fixture]; renderJobs();");
  const text = element => [element.textContent || '', ...element.children.map(text)].join(' ');
  assert.ok(text(h.elements.get('job-rows')).includes(h.ctx.fixture.release_name));
  h.ctx.fetch = async () => { throw new Error('Keep must not send a request'); };
  await h.run("submitAction(fixture,'keep')");
});
test('status bar names each service and Keep is disabled with the Sonarr reason', async () => {
  const h = await harness();
  const services = [
    {service: 'qbittorrent', label: 'qBittorrent', status: 'UP', message: 'qBittorrent is available.', checked_at_ms: 1, stale: false},
    {service: 'sonarr', label: 'Sonarr', status: 'DOWN', message: 'Sonarr is unavailable: HTTP 503 at http://sonarr:8989', checked_at_ms: 1, stale: false},
    {service: 'prowlarr', label: 'Prowlarr', status: 'DISABLED', message: 'Prowlarr is turned off (optional).', checked_at_ms: 1, stale: false},
  ];
  h.ctx.fetch = async url => { assert.equal(url, '/api/v1/services'); return response({services}); };
  await h.run('loadServices()');
  const bar = h.elements.get('service-bar');
  assert.equal(bar.hidden, false);
  const text = element => element.textContent;
  assert.deepEqual(bar.children.map(text), ['qbittorrent', 'sonarr', 'prowlarr']);
  assert.ok(bar.children[1].title.startsWith('Sonarr is unavailable: HTTP 503 at http://sonarr:8989'));
  assert.equal(h.run('keepBlockedMessage()'), 'Sonarr is unavailable: HTTP 503 at http://sonarr:8989');
  services[1] = {...services[1], stale: true};
  await h.run('loadServices()');
  assert.equal(h.run('keepBlockedMessage()'), null);        // A stopped worker's old status never blocks.
  assert.equal(text(bar.children[1]), 'sonarr');
  h.run('clearPrivate()');
  assert.equal(bar.hidden, true);
  assert.equal(bar.children.length, 0);
});
test('session expiry clears private rows and in-memory session state', async () => {
  const h = await harness(); h.ctx.fixture = target();
  h.run("state.page='jobs'; state.jobs=[fixture]; renderJobs();");
  h.ctx.fetch = async () => response('Authentication is required.', 401);
  await h.run('loadActivity()');
  assert.equal(h.run('state.csrf'), null);
  assert.equal(h.elements.get('job-rows').children.length, 0);
  assert.equal(h.elements.get('workspace').hidden, true);
  assert.equal(h.elements.get('login-panel').hidden, false);
});

test('dashboard login sends password, never the webhook or old management token field', async () => {
  const h = await harness();
  const requests = [];
  h.ctx.fetch = async (url, options) => {
    requests.push({url, options});
    if (url === '/api/v1/auth/session') return response({csrf_token: 'new-session-csrf', expires_in: 43200}, 201);
    return response({items: [], next_before_id: null});
  };
  h.elements.get('access-key').value = 'correct horse battery staple';
  const form = h.elements.get('login-form');
  await form.listeners.submit({preventDefault() {}, currentTarget: form});
  const login = requests.find(item => item.url === '/api/v1/auth/session');
  assert.ok(login);
  assert.deepEqual(JSON.parse(login.options.body), {password: 'correct horse battery staple'});
  assert.equal(login.options.credentials, 'same-origin');
  assert.equal(h.elements.get('access-key').value, '');
  assert.equal(h.run('state.csrf'), 'new-session-csrf');
});


function descendants(element) {
  return [element, ...element.children.flatMap(descendants)];
}
test('one save button per integration and credential inputs use an embedded eye', async () => {
  const h = await harness();
  const services = ['prowlarr', 'shoko', 'jellyfin', 'sonarr', 'tvdb', 'qbittorrent', 'access'];
  h.run("state.credentials = {revision: 1}; renderCredentials();");
  const container = h.elements.get('settings-fields');
  assert.deepEqual(container.children.map(panel => panel.dataset.section), services);
  for (const panel of container.children) {
    assert.equal(panel.tagName, 'details');
    const forms = panel.children.filter(child => child.tagName === 'form');
    assert.equal(forms.length, 1);
    const buttons = descendants(forms[0]).filter(el => el.tagName === 'button');
    assert.equal(buttons.filter(el => el.type === 'submit').length, 1);
    assert.equal(forms[0]._saveButton.textContent, 'save');
    assert.equal(forms[0]._saveButton.disabled, true);
    assert.equal(buttons.some(el => el.textContent === 'Show saved credentials'), false);
    assert.equal(buttons.some(el => el.textContent === 'Replace credentials'), false);
  }
  const qb = container.children.find(panel => panel.dataset.section === 'qbittorrent');
  const inputs = descendants(qb).filter(el => el.dataset.secret);
  assert.deepEqual(inputs.map(input => input.name), ['username', 'password']);
  assert.equal(inputs[1].type, 'password');
  const html = fs.readFileSync(path.join(__dirname, '../dashboard/static/index.html'), 'utf8');
  assert.doesNotMatch(html, /id="credential-forms"|<h2>Private credentials<\/h2>/);
});

test('opening an integration populates masked saved values and editing enables saving', async () => {
  const h = await harness();
  h.run('state.credentials = {revision: 1, qbittorrent_configured: true}; renderCredentials();');
  const panel = h.elements.get('settings-fields').children.find(el => el.dataset.section === 'qbittorrent');
  const form = panel.children.find(el => el.tagName === 'form');
  const inputs = descendants(form).filter(el => el.dataset.secret);
  const toggle = descendants(form).find(el => el.className === 'credential-eye secondary');
  const requests = [];
  h.ctx.fetch = async (url, options) => {
    requests.push({url, options});
    if (url.endsWith('/reveal')) return response({username: 'alice', password: 'stored-password'});
    if (url === '/api/v1/credentials/qbittorrent' && options.method === 'PATCH')
      return response({status: {revision: 2, qbittorrent_configured: true}, durability_confirmed: true});
    throw Error('unexpected API request ' + url);
  };
  assert.equal(requests.length, 0, 'credentials must not load before opening panel');
  panel.open = true; panel.listeners.toggle();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(inputs.map(input => input.value), ['alice', 'stored-password']);
  assert.equal(inputs[1].type, 'password');
  assert.equal(form._saveButton.disabled, true, 'loading values alone is not an edit');
  assert.equal(requests[0].url, '/api/v1/credentials/qbittorrent/reveal');
  assert.equal(requests[0].options.method, 'POST');
  assert.equal(requests[0].options.headers['X-CSRF-Token'], 'session-csrf');
  toggle.listeners.click();
  assert.equal(inputs[1].type, 'text');
  assert.equal(form._saveButton.disabled, true, 'visibility change is not an edit');
  toggle.listeners.click();
  assert.equal(inputs[1].type, 'password');
  inputs[1].value = 'updated-password';
  form.listeners.input();
  assert.equal(form._saveButton.disabled, false);
  await form.listeners.submit({preventDefault() {}});
  const patch = requests.find(call => call.options.method === 'PATCH');
  assert.deepEqual(JSON.parse(patch.options.body), {
    expected_revision: 1, username: 'alice', password: 'updated-password',
  });
  assert.equal(form._saveButton.disabled, true);
  assert.equal(inputs[1].value, 'updated-password');
  assert.equal(inputs[1].type, 'password');
  assert.equal(h.run('state.dirty'), false);
});

test('Matching details stays separate from History and renders recorded season checks as text', async () => {
  const h = await harness();
  const job = target();
  job.kind = 'IDENTIFY_ANIME';
  job.event_id = 1;
  job.state = 'NEEDS_REVIEW';
  job.reason = {message: 'Anime identification is incomplete.'};
  h.ctx.fixture = job;
  h.run("state.page='jobs'; state.jobs = [fixture]; renderJobs();");
  const tr = h.elements.get('job-rows').children[0];
  const buttons = descendants(tr).filter(el => el.tagName === 'button');
  const matching = buttons.find(el => el.textContent === 'details');
  assert.ok(matching);
  assert.ok(buttons.some(el => el.textContent === 'history'));
  const calls = [];
  h.ctx.fetch = async (url, options) => {
    calls.push({url, options});
    return response({
      job_id: 7, requested_job_id: 7, attempts: 1,
      state: 'NEEDS_REVIEW', reason: {message: 'Anime identification is incomplete.'},
      available: true, evidence: {
        version: 1, season: 2, episode: 1,
        queries: ['Higurashi When They Cry'],
        anidb_candidates: [], selected_anidb_id: null, selected_tvdb_id: null,
        tvdb_titles: [], checks: [
          {step: 'release_title', status: 'passed', note: null},
          {step: 'season_scope', status: 'failed', note: 'unverified_sequel'},
        ],
      },
    });
  };
  await matching.listeners.click();
  assert.equal(h.elements.get('matching-dialog').open, true);
  assert.equal(calls[0].url, '/api/v1/jobs/7/matching');
  assert.equal(calls[0].options.credentials, 'same-origin');
  const visible = descendants(h.elements.get('matching-body'))
    .map(item => item.textContent || '').join(' ');
  assert.match(visible, /Season 2/);
  assert.match(visible, /Release season compatibility/);
  assert.match(visible, /cannot safely inherit Season 1/);
  assert.match(visible, /No AniDB candidates recorded/);
  assert.equal(h.elements.get('history-dialog').open, undefined);
  h.elements.get('matching-dialog').close();
  assert.equal(h.elements.get('matching-body').children.length, 0);
});

test('Matching details does not fabricate evidence for older or queued jobs', async () => {
  const h = await harness();
  h.ctx.fetch = async () => response({
    job_id: 11, requested_job_id: 11, attempts: 1,
    state: 'NEEDS_REVIEW', reason: null, available: false, evidence: null,
  });
  await h.run('openMatching(11)');
  const text = descendants(h.elements.get('matching-body'))
    .map(item => item.textContent || '').join(' ');
  assert.match(text, /No detailed evidence was saved/);
});


test('checkpoint Retry posts the same reviewed job snapshot without restarting acquisition', async () => {
  const h = await harness();
  h.ctx.fixture = target();
  h.ctx.fixture.state = 'NEEDS_REVIEW';
  h.ctx.fixture.attempt_count = 4;
  h.ctx.fixture.reason = {code: 'JOB_RETRY_EXHAUSTED', message: 'Retry limit reached.'};
  h.ctx.fixture.actions.retry = {enabled: true, disabled_reason: null};
  h.run("state.page='jobs'; state.jobs=[fixture]; renderJobs();");
  const rows = h.elements.get('job-rows');
  const text = element => [element.textContent || '', ...element.children.map(text)].join(' ');
  assert.ok(text(rows).includes('retry'));
  const calls = [];
  h.ctx.fetch = async (url, options) => {
    calls.push({url, options});
    return response(url.includes('/retry') ? {notification:{message:'Job submitted.'},job_id:7} :
      {items:[],next_before_id:null});
  };
  await h.run("submitAction(fixture,'retry')");
  const sent = calls.filter(item => item.options.method === 'POST');
  assert.equal(sent.length,1);
  assert.equal(sent[0].url,'/api/v1/jobs/7/retry');
  assert.deepEqual(JSON.parse(sent[0].options.body),
    {expected_attempt:4,expected_reason:'JOB_RETRY_EXHAUSTED'});
  assert.equal(sent[0].options.headers['X-CSRF-Token'],'session-csrf');
});

test('Retry does not submit if the API capability disables the reviewed step', async () => {
  const h=await harness(); h.ctx.fixture=target();
  h.ctx.fixture.state='NEEDS_REVIEW';
  h.ctx.fixture.actions.retry={enabled:false,disabled_reason:{message:'This action is not available.'}};
  const button=h.run("actionButton(fixture,'retry','Retry')");
  assert.equal(button.children[0].disabled,true);
  assert.equal(button.title,'This action is not available.');
  h.ctx.fetch=async () => {throw Error('disabled Retry must not POST');};
  await h.run("submitAction(fixture,'retry')");
});

test('bulk UI uses individual endpoints for five jobs and queues a worker for six', async () => {
  for (const count of [5, 6]) {
    const h = await harness();
    h.run("state.page='jobs'; state.selected=new Set([" +
      Array.from({length: count}, (_, i) => i + 1).join(',') + "]);");
    const calls = [];
    h.ctx.fetch = async (url, options) => {
      calls.push({url, options});
      if (options.method === 'POST') {
        return response({job_id: 81, notification: {message: 'Job submitted.'}});
      }
      return response({items: [], next_before_id: null});
    };
    await h.run("submitSelected('cancel')");
    const posts = calls.filter(call => call.options.method === 'POST');
    assert.equal(posts.length, count <= 5 ? count : 1);
    if (count <= 5) {
      assert.deepEqual(posts.map(call => call.url), Array.from({length: count},
        (_, i) => '/api/v1/jobs/' + (i + 1) + '/cancel'));
      assert.ok(posts.every(call => call.options.headers['X-CSRF-Token'] === 'session-csrf'));
    } else {
      assert.equal(posts[0].url, '/api/v1/jobs/bulk');
      assert.deepEqual(JSON.parse(posts[0].options.body),
        {action: 'cancel', job_ids: [1,2,3,4,5,6]});
      assert.match(h.elements.get('notice').textContent, /Bulk job #81/);
    }
    assert.ok(calls.some(call => call.url.includes('show_completed=0&show_canceled=0')));
  }
});
test('selected jobs remain selected across filtered pages until explicitly cleared', async () => {
  const h = await harness();
  h.run("state.page='jobs'; state.selected=new Set([1,2]);");
  h.ctx.fixture = target();
  h.ctx.fixture.job_id = 7;
  h.ctx.fixture.state = 'PENDING';
  h.run("state.jobs=[fixture]; renderJobs();");
  assert.equal(h.elements.get('selection-count').textContent, '2 selected (max 100)');
  h.elements.get('select-page').checked = true;
  h.elements.get('select-page').listeners.change();
  assert.equal(h.run('state.selected.size'), 3);
  h.ctx.fetch = async () => response({items: [], next_before_id: null});
  h.elements.get('status-filter').value = 'CANCELED';
  await h.run('resetJobsPage()');
  assert.equal(h.run('state.selected.size'), 3);
  h.elements.get('bulk-clear').listeners.click();
  assert.equal(h.run('state.selected.size'), 0);
});

test('History lists every release tried for a backfill episode and why it stopped', async () => {
  const h = await harness();
  h.ctx.fetch = async () => response({
    items: [], next_after_id: null,
    job: {job_id: 151, kind: 'VERIFY_MEDIA', label: {message: 'Verify media'}, module: 'verify_media_job'},
    backfill_attempts: {episode: 2, attempts: [
      {rank: 1, state: 'FAILED', release_name: '[SubsPlease] Show - 02 (1080p)', trace_id: 'AD-131',
       reason: {message: 'The video is over the episode size limit.'},
       detail: 'The video is 1,420 MB; the episode limit is 600 MB.'},
      {rank: 2, state: 'STAGED', release_name: '<b>[ASW] Show - 02</b>', trace_id: 'AD-139', reason: null, detail: null},
      {rank: 3, state: 'NOT_TRIED', release_name: null, trace_id: null, reason: null, detail: null},
    ]},
  });
  await h.run('openHistory(151)');
  const text = element => [element.textContent || '', ...element.children.map(text)].join(' ');
  const shown = text(h.elements.get('history-rows'));
  assert.match(shown, /Releases tried for episode 2/);
  assert.match(shown, /1\. \[SubsPlease\] Show - 02 \(1080p\)/);
  assert.match(shown, /Failed · The video is over the episode size limit\. · AD-131/);
  assert.match(shown, /1,420 MB; the episode limit is 600 MB/);
  assert.match(shown, /2\. <b>\[ASW\] Show - 02<\/b>/);   // Rendered as text.
  assert.match(shown, /Staged · AD-139/);
  assert.match(shown, /3\. Ranked release #3 \(not sent yet\)/);
});
test('History separates deferred polling checks from other worker runs', async () => {
  const h = await harness();
  h.ctx.fetch = async () => response({
    items: [], next_after_id: null,
    job: {job_id: 3079, kind: 'CLEANUP_DECIDED_DOWNLOAD',
      label: {message: 'Remove the finished download from qBittorrent'},
      module: 'cleanup_decided_download_job',
      scheduler_claim_count: 6065, poll_check_count: 6064, work_attempt_count: 1,
      cleanup_progress: {phase: 'STOP_UNCERTAIN', since_ms: 1793373174000}},
  });
  await h.run('openHistory(3079)');
  const text = element => [element.textContent || '', ...element.children.map(text)].join(' ');
  const shown = text(h.elements.get('history-rows'));
  assert.match(shown, /6064 deferred checks · 1 other runs · 6065 total claims/);
  assert.match(shown, /Cleanup checkpoint: STOP_UNCERTAIN/);
});
test('Search asks the API for the current page scope and resets paging', async () => {
  const h = await harness();
  const calls = [];
  h.ctx.fetch = async url => { calls.push(url); return response({items: [], next_before_id: null}); };
  h.run("state.page='jobs'; state.cursor='55'; state.previous=['99'];");
  h.elements.get('search').value = '  frieren 05 ';
  await h.run('applySearch()');
  assert.ok(calls[0].startsWith('/api/v1/search?scope=jobs&q=frieren%2005&before_id=9223372036854775807'), calls[0]);
  assert.ok(calls[0].includes('&show_canceled='));
  assert.equal(h.run('state.previous.length'), 0);
  assert.match(h.elements.get('no-jobs').textContent, /No matches/);
  h.elements.get('search').value = 'f';
  await h.run('applySearch()');
  assert.ok(calls[1].startsWith('/api/v1/jobs?before_id='), calls[1]);
  h.run("state.page='recognized';");
  h.elements.get('recognized-search').value = 'journeys';
  await h.run('applySearch()');
  assert.equal(calls[2], '/api/v1/search?scope=recognized&q=journeys');
  h.run("state.page='intake';");
  h.elements.get('intake-search').value = 'dandadan';
  await h.run('applySearch()');
  assert.ok(calls[3].startsWith('/api/v1/search?scope=intake&q=dandadan&before_id='), calls[3]);
  assert.ok(!calls[3].includes('show_canceled'));
});
test('Intake status filter is sent to the API and labels come from status', async () => {
  const h = await harness();
  const calls = [];
  h.ctx.fetch = async url => { calls.push(url); return response({items: [
    {anidb_id: 1, title: 'A', status: 'WAITING_FOR_DUB', staged_count: 0, kept_count: 1, missing_count: 1}],
    next_before_id: null}); };
  h.run("state.page='intake'; state.cursor='55'; state.previous=['99'];");
  h.elements.get('intake-filter').value = 'WAITING_FOR_DUB';
  await h.elements.get('intake-filter').listeners.change();
  assert.ok(calls[0].startsWith('/api/v1/intake/series?before_id=9223372036854775807&limit=30&status=WAITING_FOR_DUB'), calls[0]);
  const row = h.elements.get('intake-rows').children[0];
  assert.equal(row.children[3].children[0].textContent, 'Waiting for dub');
});


test('Search again defaults to cached magnets and checks Delete cache only on request', async () => {
  for (const deleteCache of [false, true]) {
    const h = await harness();
    const calls = [];
    h.ctx.fetch = async (url, options = {}) => {
      calls.push({url, options});
      if (url === '/api/v1/intake/series/123')
        return response({anidb_id:123,title:'Example Anime',english_title:null,
          episodes:[],missing:[{anidb_episode:1,season:1,episode:1,status:'WAITING_FOR_DUB',can_retry:true}],
          keep_operations:[],reject_operations:[],rejection:null});
      return response({job_id:42,reused:false,items:[],next_before_id:null});
    };
    await h.run('openSeries(123)');
    const row = h.elements.get('series-missing').children[0];
    const retry = row.children[2].children.find(x => x.textContent === 'search again');
    assert.equal(retry.textContent, 'search again');
    const submitted = retry.listeners.click();
    const dialog = h.elements.get('search-again-dialog');
    assert.equal(dialog.open, true);
    assert.equal(h.elements.get('search-again-delete').checked, false);
    h.elements.get('search-again-delete').checked = deleteCache;
    dialog.returnValue = 'search'; dialog.close();
    await submitted;
    const posts = calls.filter(x => x.options.method === 'POST');
    assert.equal(posts.length, 1);
    assert.equal(posts[0].url, '/api/v1/intake/series/123/backfill/1/retry');
    assert.deepEqual(JSON.parse(posts[0].options.body), {delete_cache: deleteCache});
  }
});


test('NO_RESULTS renders as No usable candidates and offers Search again', async () => {
  const h = await harness();
  const calls = [];
  h.ctx.fetch = async (url, options = {}) => {
    calls.push({url, options});
    if (url === '/api/v1/intake/series/123')
      return response({anidb_id:123,title:'Example Anime',english_title:null,
        episodes:[],missing:[{anidb_episode:2,season:1,episode:2,status:'NO_RESULTS',can_retry:true}],
        keep_operations:[],reject_operations:[],rejection:null});
    return response({job_id:43,reused:false,items:[],next_before_id:null});
  };
  await h.run('openSeries(123)');
  const row = h.elements.get('series-missing').children[0];
  assert.equal(row.children[1].textContent, 'No usable candidates');
  const retry = row.children[2].children.find(x => x.textContent === 'search again');
  assert.ok(retry);
  const submitted = retry.listeners.click();
  const dialog = h.elements.get('search-again-dialog');
  assert.equal(dialog.open, true);
  dialog.returnValue = 'search'; dialog.close();
  await submitted;
  const posts = calls.filter(x => x.options.method === 'POST');
  assert.equal(posts.length, 1);
  assert.equal(posts[0].url, '/api/v1/intake/series/123/backfill/2/retry');
});


test('failed anchor offers guarded retry, try-next and a direct Jobs link', async () => {
  for (const action of ['retry', 'cancel']) {
    const h = await harness();
    const calls = [];
    let navigated = null;
    h.ctx.location.assign = url => { navigated = url; };
    h.ctx.fetch = async (url, options = {}) => {
      calls.push({url, options});
      if (url === '/api/v1/intake/series/123')
        return response({anidb_id:123,title:'Example Anime',english_title:null,
          episodes:[],missing:[{anidb_episode:5,season:1,episode:5,status:'NEEDS_REVIEW',
            reason_code:'TORRENT_METADATA_TIMEOUT',job_id:1585,reservation_id:99,
            waiting_job_ids:[2001,2002,2003],can_retry:false,
            can_retry_job:true,can_try_next:true}],
          keep_operations:[],reject_operations:[],rejection:null});
      if (url === '/api/v1/jobs/1585')
        return response({job_id:1585,attempt_count:2,
          reason:{code:'TORRENT_METADATA_TIMEOUT'},
          actions:{retry:{enabled:true},cancel:{enabled:true}}});
      if (url === '/api/v1/jobs/1585/' + action)
        return response({notification:{message:'Submitted'}},202);
      return response({items:[],next_before_id:null});
    };
    await h.run('openSeries(123)');
    const row = h.elements.get('series-missing').children[0];
    const buttons = row.children[2].children;
    assert.equal(buttons[0].textContent, 'jobs');
    assert.equal(buttons[1].textContent, 'add release');
    assert.equal(buttons[2].textContent, 'jobs');
    assert.equal(buttons[3].textContent, 'retry');
    assert.equal(buttons[4].textContent, 'cancel');
    assert.equal(row.children[1].children[0].children[1].textContent, 'TORRENT_METADATA_TIMEOUT');
    assert.match(row.children[1].children[1].textContent, /3 competing/);
    buttons[0].listeners.click();
    assert.equal(navigated, '/jobs#anidb_id=123&season=1&episode=5');
    buttons[2].listeners.click();
    assert.equal(navigated, '/jobs#job=1585');
    await buttons[action === 'retry' ? 3 : 4].listeners.click();
    const posts = calls.filter(x => x.options.method === 'POST');
    assert.equal(posts.length, 1);
    assert.equal(posts[0].url, '/api/v1/jobs/1585/' + action);
    assert.deepEqual(JSON.parse(posts[0].options.body),
      action === 'retry'
        ? {expected_attempt:2,expected_reason:'TORRENT_METADATA_TIMEOUT'} : {});
  }
});

test('Jobs prioritize configured anime title and verified episode, while release expands safely', async () => {
  const h = await harness();
  h.ctx.fixture = {...target(), anime_title: 'Translated title',
    episode_targets: [{season:1,episode:5}],
    release_name: '[Group] Japanese title - 06 [1080p]'};
  h.run("state.page='jobs'; state.jobs=[fixture]; renderJobs();");
  const cell = h.elements.get('job-rows').children[0].children[2];
  assert.equal(cell.children[0].textContent, 'Translated title');
  assert.match(cell.children[1].textContent, /TVDB S01E05/);
  assert.equal(cell.children[2].attributes.title, undefined);
  assert.equal(cell.children[2].title, h.ctx.fixture.release_name);
  const details = cell.children[3];
  assert.equal(details.tagName, 'details');
  assert.equal(details.children[0].textContent, 'details');
  assert.equal(details.children[1].textContent, h.ctx.fixture.release_name);
  assert.match(details.children[2].textContent, /S01E05/);
});

test('manual candidate form submits the selected episode without a second modal', async () => {
  const h = await harness();
  const calls = [];
  h.ctx.fetch = async (url,options={}) => {
    calls.push({url,options});
    if (url === '/api/v1/intake/series/123') return response({
      anidb_id:123,title:'Example Anime',english_title:null,episodes:[],
      missing:[{anidb_episode:5,season:1,episode:5,status:'WAITING_FOR_DUB',reservation_count:1,
        manual_submission_count:2}],keep_operations:[],reject_operations:[],rejection:null});
    if (url.endsWith('/submit')) return response({
      message:'Manual release candidate submitted.',job_id:1600},202);
    return response({items:[],next_before_id:null});
  };
  await h.run('openSeries(123)');
  const buttons=h.elements.get('series-missing').children[0].children[2].children;
  buttons[1].listeners.click();
  assert.equal(h.elements.get('series-manual-episode').textContent,'S01E05');
  assert.equal(h.elements.get('series-manual-panel').hidden,false);
  h.ctx.document.getElementById('series-manual-title').value='Example Anime S01E05';
  h.ctx.document.getElementById('series-manual-magnet').value='magnet:?xt=urn:btih:'+'a'.repeat(40);
  h.ctx.document.getElementById('series-manual-source').value='magnet';
  await h.elements.get('series-manual-form').listeners.submit({preventDefault() {}});
  const action=calls.find(x=>x.url.endsWith('/submit'));
  assert.equal(action.url,'/api/v1/intake/series/123/episodes/1/5/submit');
  assert.equal(JSON.parse(action.options.body).release_name,'Example Anime S01E05');
  assert.match(h.elements.get('notice').textContent,/Job #1600/);
});

async function manualForm(submitReply) {
  const h = await harness();
  const calls = [];
  h.ctx.fetch = async (url,options={}) => {
    calls.push({url,options});
    if (url === '/api/v1/intake/series/123') return response({
      anidb_id:123,title:'Example Anime',english_title:null,episodes:[],
      missing:[{anidb_episode:1,season:1,episode:1,status:'WAITING_FOR_DUB',reservation_count:6}],
      keep_operations:[],reject_operations:[],rejection:null});
    if (url.endsWith('/submit')) return submitReply();
    return response({items:[],next_before_id:null});
  };
  await h.run('openSeries(123)');
  const open = () => h.elements.get('series-missing').children[0].children[2].children[1].listeners.click();
  const fill = async (title, magnet) => {
    open();  // A successful submit closes the form.
    h.ctx.document.getElementById('series-manual-title').value = title;
    h.ctx.document.getElementById('series-manual-source').value = 'magnet';
    h.ctx.document.getElementById('series-manual-magnet').value = magnet;
    await h.elements.get('series-manual-form').listeners.submit({preventDefault() {}});
  };
  const submits = () => calls.filter(x => x.url.endsWith('/submit')).length;
  return {h, open, fill, submits, error: () => h.elements.get('series-manual-error').textContent};
}

test('manual form refuses a release for another episode before sending it', async () => {
  // As seen in production: an S01E05 release submitted from the S01E01 row.
  const f = await manualForm(() => response({message:'unexpected',job_id:1},202));
  await f.fill('Reborn as a Space Mercenary S01E05 The Singing Crystal 1080p CR WEB-DL MULTi (Multi-Audio, Multi-Subs)',
    'magnet:?xt=urn:btih:58460e26711f2b215e3d6e1a97fece18e953c41e&dn=x');
  assert.equal(f.submits(), 0);
  assert.equal(f.error(), 'This release is S01E05, but this is S01E01. Only a release for S01E01 can be submitted here.');
  assert.equal(f.h.elements.get('series-manual-panel').hidden, false);
  // The same title with the right episode, a repeated marker or none at all is left to the server.
  await f.fill('Example Anime S1E1v2 1080p', 'magnet:?xt=urn:btih:' + 'A'.repeat(40));
  await f.fill('Example Anime S01E01 [S01E01] 1080p', 'magnet:?xt=urn:btih:' + 'a'.repeat(40));
  await f.fill('[Group] Example Anime - 01 [1080p]', 'magnet:?xt=urn:btih:' + 'a'.repeat(40));
  assert.equal(f.submits(), 3);
});

test('manual form refuses a missing or malformed magnet before sending it', async () => {
  const f = await manualForm(() => response({message:'unexpected',job_id:1},202));
  for (const [magnet, text] of [
      ['', /Paste the magnet link/],
      ['https://example.invalid/x.torrent', /not valid/],
      ['magnet:?dn=Example', /not valid/],
      ['magnet:?xt=urn:btih:' + 'a'.repeat(39), /not valid/],
      ['magnet:?xt=urn:btih:' + 'a'.repeat(40) + '&xt=urn:btih:' + 'b'.repeat(40), /not valid/]]) {
    await f.fill('Example Anime S01E01', magnet);
    assert.match(f.error(), text, magnet);
  }
  assert.equal(f.submits(), 0);
});

test('manual form shows the server refusal inside the dialog', async () => {
  const f = await manualForm(() => response('This release is for a different episode than the one selected.', 400));
  await f.fill('Example Anime S01E01', 'magnet:?xt=urn:btih:' + 'a'.repeat(40));
  assert.equal(f.submits(), 1);
  assert.equal(f.error(), 'This release is for a different episode than the one selected.');
  // Opening the form again starts without the old error.
  f.open();
  assert.equal(f.error(), '');
});
test('the bell count comes with the service poll, shows 99+, and items mark read', async () => {
  const h = await harness();
  let unread = 3;
  const calls = [];
  h.ctx.fetch = async (url, options) => {
    calls.push([options.method, url]);
    if (url === '/api/v1/services') return response({services: [], notifications_unread: unread});
    if (url === '/api/v1/notifications') return response({unread: 1, next_before_id: null, items: [
      {notification_id: 9, type: 'SERIES_EPISODE_TROUBLE', title: 'Episode trouble: Frieren', count: 2,
       message: 'Frieren: trouble getting episodes 5, 7.', last_at_ms: 1, actions: ['open_series', 'open_job']}]});
    if (url === '/api/v1/notifications/9') return response({deleted: 1, unread: 0});
    throw new Error(url);
  };
  await h.run('loadServices()');
  const badge = h.elements.get('bell-badge');
  assert.equal(badge.hidden, false); assert.equal(badge.textContent, '3');
  unread = 150; await h.run('loadServices()');
  assert.equal(badge.textContent, '99+');
  await h.run('loadBell()');
  const item = h.elements.get('bell-items').children[0];
  const text = element => [element.textContent || '', ...element.children.map(text)].join(' ');
  assert.match(text(item), /Episode trouble: Frieren ×2/);
  const buttons = item.children[3].children;
  assert.deepEqual(buttons.map(b => b.textContent), ['series', 'history', 'mark read']);
  await buttons[2].listeners.click();
  assert.deepEqual(calls.at(-1), ['DELETE', '/api/v1/notifications/9']);
  assert.equal(badge.hidden, true);
  h.run('clearPrivate()');
  assert.equal(h.elements.get('bell-items').children.length, 0);
});

test('transcode health shows the alive count and partial availability in yellow', async () => {
  const h = await harness();
  h.run("state.services=[{service:'transcode',label:'Transcode agents',status:'UP',message:'Agents alive: 1/2. pc:8787: up, busy, gpu; lxc:8787: down'}]; renderServices();");
  const chip = h.elements.get('service-bar').children[0];
  assert.match(chip.className, /unknown/);
  assert.equal(chip.children[0].textContent, 'agents 1/2');
  assert.match(chip.title, /busy/);
});

test('staged transcode batch is shown above jobs and hidden on Intake', async () => {
  const h = await harness();
  h.run("state.page='jobs'; state.jobs=[]; state.transcodeBatch={state:'RUNNING',done:37,skipped:0,failed:2,total:200,current_file:'Episode 40.mkv'}; renderJobs();");
  const panel = h.elements.get('transcode-batch');
  assert.equal(panel.hidden, false);
  assert.match(panel.children[0].textContent, /39\/200/);
  assert.match(panel.children[1].textContent, /Episode 40.mkv.*2 failed/);
  h.run("state.page='intake'; renderJobs();");
  assert.equal(panel.hidden, true);
});

// Agent block layout: [strong, address label, token label, status, help, actions].
const agentParts = card => ({host: card.children[1].children[1], token: card.children[2].children[1],
  generate: card.children[2].children[2], status: card.children[3], help: card.children[4],
  up: card.children[5].children[0], down: card.children[5].children[1], remove: card.children[5].children[2]});

test('remote agents can be added and reordered without returning stored tokens', async () => {
  const h = await harness();
  const view = h.run("agentSettings('transcode.agents',{label:'Remote agents'},['http://first:8787','http://second:8787'])");
  const hidden = view.children[1];
  const list = view.children[2];
  agentParts(list.children[0]).down.listeners.click();
  assert.deepEqual(JSON.parse(hidden.value), ['http://second:8787','http://first:8787']);
  const first = agentParts(list.children[0]);
  assert.equal(first.token.type, 'password');
  assert.equal(first.token.value, '');
  view.children[3].listeners.click();                     // Add agent: a blank block is not saved yet.
  assert.equal(list.children.length, 3);
  assert.equal(JSON.parse(hidden.value).length, 2);
  const added = agentParts(list.children[2]);
  added.host.value = 'third:8787'; added.host.listeners.input();
  assert.deepEqual(JSON.parse(hidden.value), ['http://second:8787','http://first:8787','third:8787']);
});

test('with no agents, Remote shows one empty agent block to fill in', async () => {
  const h = await harness();
  const view = h.run("agentSettings('transcode.agents',{label:'Remote agents'},[])");
  assert.equal(view.children[2].children.length, 1);
  assert.equal(view.children[1].value, '[]');
  assert.match(agentParts(view.children[2].children[0]).status.textContent, /No token yet/);
});

test('Generate makes a strong token and shows the exact line for the agent', async () => {
  const h = await harness();
  h.ctx.crypto = require('node:crypto').webcrypto;
  const view = h.run("agentSettings('transcode.agents',{label:'Remote agents'},['http://first:8787'])");
  const parts = agentParts(view.children[2].children[0]);
  parts.generate.listeners.click();
  assert.match(parts.token.value, /^[0-9a-f]{64}$/);
  assert.equal(parts.token.type, 'text');
  assert.equal(parts.help.children[1].textContent, 'TRANSCODE_TOKEN=' + parts.token.value);
  const first = parts.token.value;
  parts.generate.listeners.click();
  assert.notEqual(parts.token.value, first);                // Each click makes a new token.
});

test('one Save stores the agent list and then its token', async () => {
  const h = await harness();
  h.ctx.crypto = require('node:crypto').webcrypto;
  const calls = [];
  h.ctx.fetch = async (url, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : null; calls.push({url, method: options.method, body});
    if (url === '/api/v1/settings') return response({configuration: {revision: 6, settings: {}}, durability_confirmed: true, restart_fields: []});
    if (url === '/api/v1/credentials/transcode') return response({status: {revision: 3, transcode_agents: ['http://new:8787']}, saved: true});
    return response({});
  };
  h.run("state.settings={desired:{revision:5,settings:{}}}; state.credentials={revision:2,transcode_agents:[]};");
  const form = h.run("node('form', null, 'integration')");
  const view = h.run("agentSettings('transcode.agents',{label:'Remote agents'},[])");
  form.appendChild(view); h.elements.get('settings-fields').appendChild(form);
  h.ctx.form = form; h.run("wireSectionForm(form,'transcode'); refreshDirty()");
  assert.equal(form._saveButton.disabled, true);
  const parts = agentParts(view.children[2].children[0]);
  parts.host.value = 'new:8787'; parts.host.listeners.input();
  parts.generate.listeners.click();
  const token = parts.token.value;
  assert.equal(form._saveButton.disabled, false);
  await form.listeners.submit({preventDefault() {}});
  assert.deepEqual(calls.map(call => call.url), ['/api/v1/settings', '/api/v1/credentials/transcode']);
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body.settings)), {transcode: {agents: ['new:8787']}});
  assert.deepEqual(JSON.parse(JSON.stringify(calls[1].body)), {expected_revision: 2, agent: 'new:8787', token});
  assert.equal(parts.token.value, '');
  assert.equal(parts.token.type, 'password');
  assert.match(parts.status.textContent, /Token is set/);
  assert.equal(parts.help.children[1].textContent, 'TRANSCODE_TOKEN=' + token);   // Still there to copy.
  assert.equal(form._saveButton.disabled, true);
});

test('a token without an agent address is not saved', async () => {
  const h = await harness();
  const calls = [];
  h.ctx.fetch = async url => { calls.push(url); return response({}); };
  h.run("state.settings={desired:{revision:5,settings:{}}}; state.credentials={revision:2,transcode_agents:[]};");
  const form = h.run("node('form', null, 'integration')");
  const view = h.run("agentSettings('transcode.agents',{label:'Remote agents'},[])");
  form.appendChild(view); h.elements.get('settings-fields').appendChild(form);
  h.ctx.form = form; h.run("wireSectionForm(form,'transcode')");
  const parts = agentParts(view.children[2].children[0]);
  parts.token.value = 'pasted-token'; parts.token.listeners.input();
  await form.listeners.submit({preventDefault() {}});
  assert.deepEqual(calls, []);
  assert.match(form.children.at(-1).textContent, /Enter the agent address/);
});


test('transcode shows only the fields the selected mode uses', async () => {
  const h = await harness();
  const shown = JSON.parse(h.run(`
    const definitions = {
      'transcode.mode': {type: 'transcode_mode', label: 'Mode', minimum: null, maximum: null, help: ''},
      'transcode.encoders': {type: 'transcode_encoders', label: 'Local encoders', minimum: null, maximum: null, help: ''},
      'transcode.tmp': {type: 'directory', label: 'Local temp path', minimum: null, maximum: null, help: ''},
      'transcode.vaapi_device': {type: 'path', label: 'VAAPI device', minimum: null, maximum: null, help: ''},
      'transcode.target_mb': {type: 'integer', label: 'Target MB', minimum: 1, maximum: 1000000, help: ''},
      'transcode.retries': {type: 'integer', label: 'Retries per agent', minimum: 0, maximum: 10, help: ''},
      'transcode.retry_seconds': {type: 'integer', label: 'Retry delay', minimum: 1, maximum: 300, help: ''},
      'transcode.agents': {type: 'transcode_agents', label: 'Remote agents'},
    };
    const values = {'transcode.mode': 'off', 'transcode.encoders': 'gpu,cpu', 'transcode.tmp': '/var/tmp',
      'transcode.vaapi_device': '/dev/dri/renderD128', 'transcode.target_mb': 500, 'transcode.retries': 2,
      'transcode.retry_seconds': 10, 'transcode.agents': []};
    const rows = new Map(Object.entries(definitions).map(([path, d]) => [path, settingInput(path, d, values[path])]));
    transcodeModeFields(rows);
    const visible = () => [...rows].filter(([, row]) => !row.hidden).map(([path]) => path);
    const mode = rows.get('transcode.mode')._input;
    const result = {off: visible()};
    for (const value of ['local', 'remote', 'off']) { mode.value = value; mode.listeners.change(); result[value] = visible(); }
    result.tmpKept = rows.get('transcode.tmp')._input.value;
    JSON.stringify(result)`));
  assert.deepEqual(shown.off, ['transcode.mode']);
  assert.deepEqual(shown.local, ['transcode.mode', 'transcode.encoders', 'transcode.tmp', 'transcode.vaapi_device', 'transcode.target_mb']);
  assert.deepEqual(shown.remote, ['transcode.mode', 'transcode.target_mb', 'transcode.retries', 'transcode.retry_seconds', 'transcode.agents']);
  assert.equal(shown.tmpKept, '/var/tmp');   // Hidden fields keep their values.
});

test('console shortcuts navigate, focus search, and respect typing', async () => {
  const h = await harness(); const visited = [];
  h.ctx.location.assign = url => visited.push(url);
  h.ctx.key = {key: '2', target: new Element('body'), preventDefault() {}};
  h.run('keyboardShortcut(key)');
  assert.deepEqual(visited, ['/jobs']);
  for (const [key, url] of [['1', '/'], ['3', '/recognized'], ['4', '/settings']]) {
    h.ctx.key.key = key; h.run('keyboardShortcut(key)'); assert.equal(visited.at(-1), url);
  }
  h.ctx.key.key = '/'; h.run('keyboardShortcut(key)');
  assert.equal(h.elements.get('intake-search').focused, true);
  h.ctx.key.key = '2'; h.ctx.key.target = new Element('input');
  h.run('keyboardShortcut(key)'); assert.equal(visited.length, 4);
  h.ctx.key.target = new Element('textarea'); h.run('keyboardShortcut(key)');
  h.ctx.key.target = new Element('select'); h.run('keyboardShortcut(key)');
  assert.equal(visited.length, 4);
});
test('Escape closes the topmost dialog before the bell', async () => {
  const h = await harness(); const first = new Element('dialog'), top = new Element('dialog');
  first.showModal(); top.showModal();
  h.ctx.document.querySelectorAll = selector => selector === 'dialog[open]' ? [first, top] : [];
  h.ctx.key = {key: 'Escape', target: new Element('body'), preventDefault() {}};
  h.run('keyboardShortcut(key)'); assert.equal(top.open, false); assert.equal(first.open, true);
});


test('keep and each stopped follow-up have separate fixed labels and target jobs', async () => {
  const h = await harness();
  const receipt = {job_id: 8, state: 'NEEDS_REVIEW', status: {message: 'Needs review'},
    download_cleanup: {job_id: 9, state: 'NEEDS_REVIEW', status: {message:'Needs review'}},
    optional_shoko: {job_id: 10, state: 'BLOCKED', status: {message:'Blocked'}}};
  h.ctx.fixture = {...target(), cancellation:{operation_id:3,state:'STAGED'},
    actions:{keep:{enabled:true,execution_available:true,job:receipt}}};
  h.run("state.page='jobs'; state.jobs=[fixture]; renderJobs()");
  let buttons = descendants(h.elements.get('job-rows')).filter(el => el.tagName === 'button');
  assert.deepEqual(buttons.map(b => b.textContent), ['keep','retry cleanup','retry shoko','history']);
  assert.ok(buttons.every(b => b.title && b.title.endsWith('.')));
  const calls = [];
  h.ctx.fetch = async (url, options) => {
    calls.push({url,options});
    if (url === '/api/v1/jobs/9') return response({attempt_count:2,reason:{code:'CLEANUP_REVIEW_REQUIRED'},actions:{retry:{enabled:true}}});
    return response({items:[], next_before_id:null});
  };
  h.ctx.confirm = () => false;
  await buttons[1].listeners.click(); assert.equal(calls.length,0);
  h.ctx.confirm = () => true;
  await buttons[1].listeners.click();
  assert.equal(calls.find(c => c.options.method === 'POST').url, '/api/v1/jobs/9/retry');
  receipt.completion = {message:'Imported'}; receipt.state = 'DONE';
  h.run('state.jobs=[fixture]; renderJobs()');
  buttons = descendants(h.elements.get('job-rows')).filter(el => el.tagName === 'button');
  assert.equal(buttons.some(b => b.textContent === 'keep'),false);
  assert.ok(buttons.some(b => b.textContent === 'retry cleanup'));
});

test('cancel, cleanup, series rejection and bulk actions require confirmation', async () => {
  const h = await harness(); h.ctx.confirm = () => false;
  h.ctx.fixture = target(); h.ctx.fixture.actions.cleanup = {enabled:true};
  const calls = []; h.ctx.fetch = async (url,options) => {calls.push({url,options}); return response({items:[]});};
  for (const name of ['cancel','cleanup']) await h.run(`submitAction(fixture, '${name}')`);
  h.run("state.page='jobs'; state.selected = new Set([1,2,3]);");
  for (const name of ['retry','cancel','cleanup']) await h.run(`submitSelected('${name}')`);
  h.elements.get('bulk-clear').listeners.click();
  assert.equal(h.run('state.selected.size'),3); assert.equal(calls.length,0);
  h.ctx.fetch = async url => response({anidb_id:123,title:'Anime',episodes:[],missing:[],keep_operations:[],reject_operations:[3],rejection:null});
  await h.run('openSeries(123)');
  h.ctx.fetch = async (url,options) => {calls.push({url,options}); return response({});};
  await h.elements.get('series-reject').onclick(); assert.equal(calls.length,0);
});

test('one status select includes canceled on request and hides it by default', async () => {
  const h = await harness(); const calls = [];
  h.ctx.fetch = async url => {calls.push(url); return response({items:[],next_before_id:null});};
  h.run("state.page='jobs'"); await h.run('loadActivity()');
  assert.match(calls.at(-1), /show_completed=0&show_canceled=0/);
  h.elements.get('status-filter').value = 'CANCELED'; await h.run('loadActivity()');
  assert.match(calls.at(-1), /state=CANCELED&show_completed=0&show_canceled=1/);
  h.elements.get('status-filter').value = 'DONE'; await h.run('loadActivity()');
  assert.match(calls.at(-1), /state=DONE&show_completed=1&show_canceled=0/);
  h.elements.get('status-filter').value = 'ALL'; await h.run('loadActivity()');
  assert.doesNotMatch(calls.at(-1), /state=ALL/);
  assert.match(calls.at(-1), /show_completed=1&show_canceled=1/);
  const html = fs.readFileSync(path.join(__dirname,'../dashboard/static/index.html'),'utf8');
  assert.doesNotMatch(html,/id="(?:show-completed|show-canceled|activity-panel|refresh|reload-settings|refresh-recognized|bulk-action|bulk-submit)"/);
});

test('every static button has a hint and help repeats rendered button hints', async () => {
  const html = fs.readFileSync(path.join(__dirname,'../dashboard/static/index.html'),'utf8');
  for (const match of html.matchAll(/<button\b([^>]*)>/g)) assert.match(match[1],/title="[^"]+"/);
  const h = await harness(); h.ctx.fixture = {...target(),state:'FAILED',actions:{retry:{enabled:true}}};
  h.run("state.page='jobs'; state.jobs=[fixture]; renderJobs(); showHelp()");
  const buttons = descendants(h.elements.get('job-rows')).filter(el => el.tagName === 'button');
  const help = h.elements.get('page-help').children.map(el => el.textContent);
  for (const button of buttons) { assert.ok(button.title); assert.ok(help.includes(button.title)); }
  assert.ok(buttons.some(b => b.textContent === 'retry'));
});

test('Names and Settings poll their own endpoints and Settings preserve edits', async () => {
  const h = await harness(); const calls = [];
  h.ctx.fetch = async url => {calls.push(url); return response({items:[]});};
  h.run("state.page='recognized'; state.servicesAt=Date.now()"); await h.run('tick()');
  assert.deepEqual(calls,['/api/v1/recognized']); calls.length=0;
  h.run("state.page='settings'; state.dirty=true"); await h.run('tick()');
  assert.equal(calls.length,0); assert.equal(h.run('state.dirty'),true);
});

test('notification Jobs action navigates to the Jobs page', async () => {
  const h = await harness(); let destination;
  h.ctx.location.assign = url => {destination=url;};
  h.ctx.fetch = async () => response({}); h.ctx.item = {notification_id:12}; h.ctx.button = new Element('button');
  await h.run("bellAction(item,'open_jobs',button)"); assert.equal(destination,'/jobs');
});

test('Settings reads respect nonblocking admission and polling reconnects after refusal', async () => {
  const h = await harness(); let busy = false, refuse = true;
  const settings = {desired:{revision:1,settings:{anime:{title_language:'english'}}},
    fields:{'anime.title_language':{type:'title_language',label:'Title language',help:'Choose the title language.',minimum:null,maximum:null}},
    pending_restart_fields:[]};
  h.ctx.fetch = async url => {
    if (refuse) return response('Waiting for running work to finish.',503);
    if (busy) return response('Waiting for running work to finish.',503);
    busy = true; await new Promise(resolve => setImmediate(resolve)); busy = false;
    return response(url.endsWith('/settings') ? settings : {revision:1});
  };
  h.run("state.page='settings'"); await h.run('loadSettings()');
  assert.equal(h.elements.get('live-indicator').textContent,'paused (reconnecting)');
  refuse = false; await h.run('refreshPage()');
  assert.equal(h.elements.get('live-indicator').textContent,'live');
  assert.equal(h.elements.get('settings-fields').children[0].dataset.section,'anime');
});


test('boolean Settings edits enable save after the native input-before-change sequence', async () => {
  const h = await harness();
  const form = h.run("node('form', null, 'integration')");
  const field = h.run("settingInput('sonarr.backfill_long_running', {type:'boolean',label:'Backfill',help:'Enable backfill.',minimum:null,maximum:null}, false)");
  form.appendChild(field); h.elements.get('settings-fields').appendChild(form);
  h.ctx.form = form; h.run("wireSectionForm(form,'sonarr'); refreshDirty()");
  assert.equal(form._saveButton.disabled,true);
  const input = field.children[1]; input.checked = true;
  form.listeners.input(); input.listeners.change();
  assert.equal(form._saveButton.disabled,false); assert.equal(h.run('state.dirty'),true);
  assert.equal(input.value,'true');
  input.checked = false; form.listeners.input(); input.listeners.change();
  assert.equal(form._saveButton.disabled,true); assert.equal(h.run('state.dirty'),false);
});
