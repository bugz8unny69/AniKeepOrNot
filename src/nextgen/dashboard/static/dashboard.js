/* Reuses the legacy jobs-table DOM rendering and schema-driven settings pattern.
 * Private data and actions travel only through /api/v1/. No legacy endpoints.
 */
'use strict';
const API = '/api/v1';
const MAX_CURSOR = '9223372036854775807';
const COPY = Object.freeze({
  'dashboard.connection': 'The dashboard could not connect. Refresh to try again.',
  'dashboard.expired': 'Please sign in to continue.',
  'dashboard.request_failed': 'The request could not be completed.',
  'dashboard.uncertain': 'The response could not be confirmed. Refresh the job status before trying again.',
  'dashboard.submitting': 'Submitting the job.',
  'dashboard.confirm_cancel': 'Cancel this unfinished acquisition and clean up its owned download files?',
  'dashboard.confirm_cleanup': 'Continue cleanup for this canceled acquisition?',
  'dashboard.not_available': 'This action is not available.',
  'dashboard.settings_saved': 'Settings saved. New operations will use these values.',
  'dashboard.restart': 'Settings saved. A service restart is required for some changes.',
  'dashboard.saved_unconfirmed': 'Changes were saved, but storage durability needs review.',
  'dashboard.credentials_saved': 'Credentials saved.',
  'dashboard.invalid_value': 'Check the setting value before saving.',
  'dashboard.no_history': 'No history has been recorded yet.',
  'dashboard.staged': 'Staged for viewing. Choose Keep when it is available.',
  'dashboard.scope': 'Intake groups releases by anime. Open a series to review staged and missing episodes.',
  'dashboard.page_filter': 'The status filter applies to this page.',
  'dashboard.bulk_limit': 'Select at most 100 jobs at a time.',
  'dashboard.discard': 'Discard unsaved changes and refresh settings?',
});
const $ = id => document.getElementById(id);
const node = (tag, text, cls) => {
  const el = document.createElement(tag);
  if (text !== undefined && text !== null) el.textContent = String(text);
  if (cls) el.className = cls;
  return el;
};
const state = {
  csrf: null, generation: 0, requests: new Set(), jobs: [], pending: new Set(),
  selected: new Set(), bulkSubmitting: false,
  page: location.pathname === '/settings' ? 'settings' : location.pathname === '/jobs' ? 'jobs' : location.pathname === '/recognized' ? 'recognized' : 'intake',
  cursor: MAX_CURSOR, previous: [], next: null, loading: null, timer: null,
  settings: null, credentials: null, dirty: false, settingsVersion: 0,
  historyJob: null, historyCursor: 0, historyNext: null, historyVersion: 0,
  matchingJob: null, matchingVersion: 0,
  seriesBusy: false, manualTarget: null, expandedJobId: null,
  episodeFilter: (() => {
    if (location.pathname !== '/jobs') return null;
    const params = new URLSearchParams((location.hash || '').slice(1));
    const fields = ['anidb_id', 'season', 'episode'].map(name => params.get(name));
    return fields.every(value => value !== null && /^\d{1,18}$/.test(value))
      ? {anidb_id: fields[0], season: fields[1], episode: fields[2]} : null;
  })(),
  search: location.pathname === '/jobs' && /^\d{1,18}$/.test(new URLSearchParams((location.hash || '').slice(1)).get('job') || '')
    ? '#' + new URLSearchParams((location.hash || '').slice(1)).get('job') : '',
  recognizedSearch: '', searchTimer: null,
};
function notice(message = '', error = false) {
  $('notice').textContent = message;
  $('notice').classList.toggle('error', error);
}
function clearPrivate(message = '') {
  state.generation += 1;
  state.csrf = null;
  for (const controller of state.requests) controller.abort();
  state.requests.clear();
  clearTimeout(state.timer);
  state.jobs = []; state.pending.clear(); state.selected.clear(); state.bulkSubmitting = false;
  state.settings = null; state.credentials = null;
  state.previous = []; state.cursor = MAX_CURSOR; state.next = null; state.dirty = false;
  state.settingsVersion += 1;
  state.historyJob = null; state.historyVersion += 1;
  state.matchingJob = null; state.matchingVersion += 1;
  $('job-rows').replaceChildren(); $('settings-fields').replaceChildren();
  $('history-rows').replaceChildren();
  $('matching-body').replaceChildren(); $('matching-summary').textContent = '';
  $('recognized-rows').replaceChildren(); $('recognized-note').textContent = '';
  $('access-key').value = '';
  state.search = ''; state.recognizedSearch = ''; clearTimeout(state.searchTimer);
  $('search').value = ''; $('recognized-search').value = '';
  if ($('history-dialog').open) $('history-dialog').close();
  if ($('matching-dialog').open) $('matching-dialog').close();
  if ($('series-dialog').open) $('series-dialog').close();
  $('workspace').hidden = true; $('sign-out').hidden = true; $('login-panel').hidden = false;
  notice(message);
}
async function request(path, {method = 'GET', body} = {}) {
  // Never accept API-supplied URLs or arbitrary destinations for mutations.
  if (!path.startsWith(API + '/')) throw new Error(COPY['dashboard.request_failed']);
  const generation = state.generation;
  const controller = new AbortController(); state.requests.add(controller);
  const timeout = setTimeout(() => controller.abort(), path.endsWith('/submit') ? 30000 : 15000);
  try {
    const headers = {'Accept': 'application/json'};
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (method !== 'GET' && state.csrf) headers['X-CSRF-Token'] = state.csrf;
    const response = await fetch(path, {method, headers, credentials: 'same-origin', cache: 'no-store',
      redirect: 'error', signal: controller.signal, body: body === undefined ? undefined : JSON.stringify(body)});
    const result = await response.json();
    if (generation !== state.generation) throw new DOMException('Stale response', 'AbortError');
    if (!response.ok) {
      const message = result.error?.message || COPY['dashboard.request_failed'];
      if (response.status === 401 && state.csrf) clearPrivate(COPY['dashboard.expired']);
      const error = new Error(message); error.status = response.status; throw error;
    }
    return result.data;
  } catch (error) {
    if (generation !== state.generation) throw new DOMException('Stale response', 'AbortError');
    if (error.status) throw error;
    throw new Error(method === 'GET' ? COPY['dashboard.connection'] : COPY['dashboard.uncertain']);
  } finally {
    clearTimeout(timeout); state.requests.delete(controller);
  }
}
function displayError(error) {
  if (error.name !== 'AbortError') notice(error.message || COPY['dashboard.request_failed'], true);
}
function date(value) { return value ? new Date(value).toLocaleString() : '—'; }
function statusBadge(job) {
  const classes = {DONE: 'completed', RUNNING: 'running', RETRY_WAIT: 'waiting', ON_HOLD: 'waiting', NEEDS_REVIEW: 'blocked', BLOCKED: 'blocked', CANCELED: 'blocked'};
  return node('span', job.status?.message || 'Unknown status', 'status ' + (classes[job.state] || ''));
}
function actionButton(job, action, label) {
  const capability = job.actions?.[action];
  const submitting = state.bulkSubmitting || state.pending.has(job.cancellation.operation_id);
  const enabled = Boolean(capability?.enabled && capability.execution_available !== false && !submitting);
  const explanation = submitting ? COPY['dashboard.submitting'] :
    capability?.disabled_reason?.message || capability?.availability_reason?.message;
  const wrapper = node('span', null, 'action-tooltip');
  const button = node('button', label, action === 'cancel' ? 'danger' : 'secondary');
  button.type = 'button'; button.disabled = !enabled;
  if (!enabled) {
    wrapper.tabIndex = 0; wrapper.dataset.disabled = 'true';
    wrapper.title = explanation || COPY['dashboard.not_available'];
    wrapper.setAttribute('aria-label', label + ': ' + wrapper.title);
    button.title = wrapper.title;
  }
  button.addEventListener('click', () => submitAction(job, action));
  wrapper.appendChild(button); return wrapper;
}
async function submitAction(job, action) {
  if (state.pending.has(job.cancellation.operation_id)) return;
  const capability = job.actions?.[action];
  if (!capability?.enabled || capability.execution_available === false) return;
  if (action === 'cancel' && !confirm(COPY['dashboard.confirm_cancel'])) return;
  if (action === 'cleanup' && !confirm(COPY['dashboard.confirm_cleanup'])) return;
  state.pending.add(job.cancellation.operation_id); renderJobs();
  try {
    const body = action === 'retry' ? {expected_attempt: job.attempt_count, expected_reason: job.reason?.code} : {};
    const receipt = await request(`${API}/jobs/${job.job_id}/${action}`, {method: 'POST', body});
    notice(receipt.notification?.message || COPY['dashboard.request_failed']);
  } catch (error) { displayError(error); }
  finally {
    // Drain a poll that began before the action, then obtain a fresh snapshot.
    if (state.loading) await state.loading;
    state.pending.delete(job.cancellation.operation_id);
  }
  // A lost response is reconciled by reading status, never by replaying a POST.
  if (state.csrf) await loadActivity();
}
function tvdbLabel(episode) {
  return 'S' + String(episode.season).padStart(2, '0') +
    'E' + String(episode.episode).padStart(2, '0');
}
// Missing episodes are AniDB-numbered; the verified TVDB placement is a label when known.
function missingLabel(item) {
  return 'E' + String(item.anidb_episode).padStart(2, '0') +
    (item.season == null ? '' : ' · TVDB ' + tvdbLabel(item));
}
function navigateEpisodeJobs(anidbId, episode) {
  // Keep the episode in the previous history entry so native browser Back
  // reopens the same series view without another navigation control.
  const seriesUrl = '/#series=' + encodeURIComponent(anidbId) +
    '&episode=' + encodeURIComponent(tvdbLabel(episode));
  if (window.history?.replaceState) window.history.replaceState(null, '', seriesUrl);
  location.assign('/jobs#anidb_id=' + encodeURIComponent(anidbId) +
    '&season=' + encodeURIComponent(episode.season) +
    '&episode=' + encodeURIComponent(episode.episode));
}
function relatedJobsButton(anidbId, episode) {
  const button = node('button', 'Related jobs', 'secondary');
  button.type = 'button';
  button.addEventListener('click', () => navigateEpisodeJobs(anidbId, episode));
  return button;
}
function renderJobs() {
  if (state.page === 'intake') { renderSeries(); return; }
  const tbody = $('job-rows'); tbody.replaceChildren();
  const filter = $('status-filter').value;
  const jobs = state.jobs.filter(job => state.page === 'jobs' || !filter || job.state === filter);
  $('no-jobs').hidden = jobs.length !== 0;
  for (const job of jobs) {
    const tr = node('tr');
    const td = value => { const cell = node('td', value); tr.appendChild(cell); return cell; };
    if (state.page === 'jobs') {
      const cell = td();
      const checkbox = node('input'); checkbox.type = 'checkbox'; checkbox.checked = state.selected.has(job.job_id);
      checkbox.disabled = state.bulkSubmitting;
      checkbox.setAttribute('aria-label', 'Select job #' + job.job_id);
      checkbox.addEventListener('change', () => {
        if (state.bulkSubmitting) return;
        if (checkbox.checked && state.selected.size >= 100) {
          checkbox.checked = false; notice(COPY['dashboard.bulk_limit'], true); return;
        }
        if (checkbox.checked) state.selected.add(job.job_id);
        else state.selected.delete(job.job_id);
        renderBulkControls();
      });
      cell.appendChild(checkbox);
    }
    td(date(job.received_at_ms || job.created_at_ms));
    const title = td();
    title.appendChild(node('strong', job.anime_title || (job.release_name ? 'Anime not identified' : 'Management job'), 'job-anime'));
    const targets = job.episode_targets || [];
    if (targets.length) {
      const shown = targets.slice(0, 3).map(tvdbLabel).join(', ');
      title.appendChild(node('small', 'TVDB ' + shown +
        (targets.length > 3 ? ' +' + (targets.length - 3) + ' more' : ''), 'job-episode'));
    } else if (job.release_name) {
      title.appendChild(node('small', 'Episode not determined', 'muted job-note'));
    }
    if (job.release_name) {
      const release = node('span', job.release_name, 'job-release muted');
      release.title = job.release_name;
      title.appendChild(release);
      const expanded = node('details', null, 'job-details');
      const summary = node('summary', 'Release details');
      expanded.appendChild(summary);
      expanded.appendChild(node('p', job.release_name, 'job-release-full'));
      expanded.appendChild(node('small', targets.length ?
        'Verified TVDB target: ' + targets.map(tvdbLabel).join(', ') :
        'Episode mapping has not been verified for this job.', 'muted'));
      expanded.open = state.expandedJobId === job.job_id;
      expanded.addEventListener('toggle', () => {
        if (expanded.open) state.expandedJobId = job.job_id;
        else if (state.expandedJobId === job.job_id) state.expandedJobId = null;
        if (expanded.open) {
          for (const other of tbody.querySelectorAll('details.job-details')) {
            if (other !== expanded) other.open = false;
          }
        }
      });
      title.appendChild(expanded);
    }
    td().appendChild(node('span', `#${job.job_id} · ${job.label?.message || 'Job'}`));
    const status = td(); status.appendChild(statusBadge(job));
    if (state.page === 'intake') status.appendChild(node('small', job.acquisition_status.message, 'muted job-note'));
    const details = td();
    if (job.reason) {
      details.appendChild(node('p', job.reason.message));
      if (job.reason.action) details.appendChild(node('small', job.reason.action, 'muted'));
    } else if (job.cancellation.state === 'STAGED' && !job.actions?.keep?.job?.completion) details.appendChild(node('p', COPY['dashboard.staged']));
    if (job.cleanup_progress) {
      const phases = {
        PREPARED: 'Preparing cleanup', STOP_UNCERTAIN: 'Confirming torrent stopped',
        REMOVE_UNCERTAIN: 'Confirming torrent removed (no repeat deletion)',
        FILES: 'Cleaning leftover files', COMPLETE: 'Cleanup completed'
      };
      details.appendChild(node('small',
        `Cleanup: ${phases[job.cleanup_progress.phase] || job.cleanup_progress.phase} · Last advanced ${date(job.cleanup_progress.since_ms)}`,
        'muted'));
    }
    if (job.size_detail) details.appendChild(node('p', job.size_detail));
    if (job.failed_detail) details.appendChild(node('small', job.failed_detail + ` · run: sudo adm report job --id ${job.job_id}`, 'muted'));
    const activeAction = ['cleanup', 'keep'].map(name => job.actions?.[name]?.job).find(Boolean);
    if (activeAction) {
      details.appendChild(node('p', `Job #${activeAction.job_id}: ${activeAction.status.message}`));
      if (activeAction.reason) details.appendChild(node('small', activeAction.reason.message, 'muted'));
      if (activeAction.completion) details.appendChild(node('p', activeAction.completion.message));
      if (activeAction.post_keep_cleanup) {
        const cleanup = activeAction.post_keep_cleanup;
        details.appendChild(node('small', cleanup.completed ? cleanup.completion.message : `Staging cleanup: ${cleanup.status.message}`, 'muted'));
        if (cleanup.reason) details.appendChild(node('small', cleanup.reason.message, 'muted'));
      }
      if (activeAction.download_cleanup) {
        const cleanup = activeAction.download_cleanup;
        details.appendChild(node('small', cleanup.completed ? cleanup.completion.message : `Download cleanup: ${cleanup.status.message}`, 'muted'));
        if (cleanup.reason) details.appendChild(node('small', cleanup.reason.message, 'muted'));
      }
      if (activeAction.optional_shoko) {
        const shoko = activeAction.optional_shoko;
        details.appendChild(node('small', `Optional Shoko metadata: ${shoko.status.message}`, 'muted'));
        if (shoko.metadata) details.appendChild(node('small', `${shoko.metadata.title} · ${shoko.metadata.episode_title}`, 'muted'));
        if (shoko.reason) details.appendChild(node('small', shoko.reason.message, 'muted'));
      }
    }
    const actions = td();
    if (job.actions?.keep && 'execution_available' in job.actions.keep) {
      const cleanup = job.actions?.keep?.job?.post_keep_cleanup;
      const retryCleanup = cleanup && ['NEEDS_REVIEW', 'BLOCKED'].includes(cleanup.state);
      const retryDownload = name => ['NEEDS_REVIEW', 'BLOCKED'].includes(job.actions?.[name]?.job?.download_cleanup?.state);
      const retryShoko = ['NEEDS_REVIEW', 'BLOCKED'].includes(job.actions?.keep?.job?.optional_shoko?.state);
      actions.appendChild(actionButton(job, 'keep', retryDownload('keep') ? 'Retry download cleanup' : retryCleanup ? 'Retry staging cleanup' : retryShoko ? 'Retry Shoko metadata' : 'Keep'));
      if (job.cancellation.state !== 'STAGED') actions.appendChild(actionButton(job, 'cancel', 'Cancel'));
      if (job.cancellation.state === 'CANCELED' || job.cancellation.state === 'CANCEL_REQUESTED') actions.appendChild(actionButton(job, 'cleanup', 'Cleanup'));
    }
    if (job.state === 'NEEDS_REVIEW' && job.actions?.retry) {
      actions.appendChild(actionButton(job, 'retry', 'Retry'));
    }
    if (job.event_id && !job.is_management_action) {
      const matching = node('button', 'Matching details', 'secondary');
      matching.type = 'button';
      matching.addEventListener('click', () => openMatching(job.job_id));
      actions.appendChild(matching);
    }
    if (job.kind === 'VERIFY_RELEASE') {
      const coverage = node('button', 'Episode coverage', 'secondary');
      coverage.type = 'button';
      coverage.addEventListener('click', () => openCoverage(job.job_id));
      actions.appendChild(coverage);
    }
    const history = node('button', 'History', 'secondary');
    history.addEventListener('click', () => openHistory(job.job_id)); actions.appendChild(history);
    tbody.appendChild(tr);
  }
  $('previous-page').disabled = state.previous.length === 0;
  $('next-page').disabled = state.next === null;
  if (state.page === 'jobs') renderBulkControls();
}
function renderBulkControls() {
  if (state.page !== 'jobs') return;
  const count = state.selected.size;
  $('bulk-actions').hidden = count === 0;
  $('selection-count').textContent = count + ' selected (max 100)';
  $('bulk-submit').disabled = !count || state.bulkSubmitting;
  $('bulk-action').disabled = state.bulkSubmitting;
  $('bulk-clear').disabled = state.bulkSubmitting;
  const onPage = state.jobs;
  $('select-page').disabled = state.bulkSubmitting || !onPage.length;
  $('select-page').checked = Boolean(onPage.length && onPage.every(job => state.selected.has(job.job_id)));
  $('select-page').indeterminate = onPage.some(job => state.selected.has(job.job_id)) && !$('select-page').checked;
}
async function resetJobsPage() {
  if (!state.csrf || state.page !== 'jobs') return;
  if (state.loading) await state.loading;
  state.previous = []; state.cursor = MAX_CURSOR; state.next = null;
  await loadActivity();
}
async function submitSelected() {
  if (state.bulkSubmitting || !state.csrf || state.page !== 'jobs' || !state.selected.size) return;
  const ids = [...state.selected];
  const action = $('bulk-action').value;
  if (!['retry', 'cancel', 'cleanup'].includes(action)) return;
  if (!confirm(`Apply ${action} to ${ids.length} selected jobs? Existing lifecycle and cleanup checks still apply.`)) return;
  state.bulkSubmitting = true; renderJobs();
  let successful = 0, failed = 0, submitted = null;
  try {
    if (ids.length > 5) {
      submitted = await request(API + '/jobs/bulk', {method: 'POST', body: {action, job_ids: ids}});
      state.selected.clear();
      notice(`Bulk job #${submitted.job_id} submitted for ${ids.length} selected jobs. Follow its history for the result.`);
    } else {
      for (const id of ids) {
        if (!state.csrf) break;
        try {
          let body = {};
          if (action === 'retry') {
            const job = await request(`${API}/jobs/${id}`);
            if (!job.actions?.retry?.enabled) { failed++; continue; }
            body = {expected_attempt: job.attempt_count, expected_reason: job.reason?.code};
          }
          await request(`${API}/jobs/${id}/${action}`, {method: 'POST', body});
          state.selected.delete(id);
          successful++;
        } catch (error) { failed++; if (!state.csrf) break; }
      }
      if (state.csrf) notice(`${successful} job action(s) accepted; ${failed} could not be applied.`, failed > 0);
    }
  } catch (error) { displayError(error); }
  finally {
    if (state.loading) await state.loading;
    state.bulkSubmitting = false;
    if (state.csrf) await loadActivity();
  }
}
const INTAKE_STATUS = Object.freeze({
  REJECTED: 'Rejected', STAGED: 'Ready to Keep', WAITING_FOR_DUB: 'Waiting for dub', KEPT: 'Kept', TRACKING: 'Tracking',
});
function openManualSubmission(anidbId, episode) {
  state.manualTarget = {anidbId, season: episode.season, episode: episode.episode};
  $('series-manual-episode').textContent = tvdbLabel(episode);
  $('series-manual-form').reset();
  $('series-manual-source').value = 'magnet';
  $('series-manual-magnet-label').hidden = false;
  $('series-manual-torrent-label').hidden = true;
  $('series-manual-panel').hidden = false;
  $('series-manual-panel').scrollIntoView?.({block: 'nearest'});
  $('series-manual-title').focus?.();
}
function manualSubmissionButton(anidbId, episode) {
  const button = node('button', 'Submit release', 'secondary');
  button.type = 'button';
  button.addEventListener('click', () => openManualSubmission(anidbId, episode));
  return button;
}
function renderSeries() {
  const tbody = $('job-rows'); tbody.replaceChildren();
  $('no-jobs').hidden = state.jobs.length !== 0;
  for (const series of state.jobs) {
    const tr = node('tr');
    const td = value => { const cell = node('td', value); tr.appendChild(cell); return cell; };
    td('—');
    td().appendChild(node('strong', series.title));
    td(series.staged_count + ' staged');
    td().appendChild(node('span', INTAKE_STATUS[series.status] || (series.rejection ? 'Rejected' :
      series.staged_count ? 'Ready to Keep' : series.waiting_for_dub ? 'Waiting for dub' :
      series.kept_count ? 'Kept' : 'Tracking'),
      'status ' + (series.rejection ? 'blocked' :
        series.staged_count || (series.kept_count && !series.waiting_for_dub) ? 'completed' : 'waiting')));
    td(series.missing_count + ' not staged');
    const actions = td();
    const open = node('button', 'View series', 'secondary');
    open.type = 'button';
    open.addEventListener('click', () => openSeries(series.anidb_id));
    actions.appendChild(open);
    tbody.appendChild(tr);
  }
  $('previous-page').disabled = state.previous.length === 0;
  $('next-page').disabled = state.next === null;
}
async function openSeries(anidbId, recordLocation = true, focusEpisode = null) {
  if (state.seriesBusy) return;
  try {
    const series = await request(API + '/intake/series/' + anidbId);
    const dialog = $('series-dialog');
    $('series-title').textContent = series.title;
    const alternative = series.title === series.english_title ? series.romaji_title : series.english_title;
    const description = [alternative, series.type, series.year].filter(Boolean).join(' · ');
    $('series-description').textContent = description ||
      'No series synopsis is stored in the verified metadata yet.';
    const list = $('series-episodes'); list.replaceChildren();
    for (const item of series.episodes) {
      const tr = node('tr');
      tr.dataset.episode = tvdbLabel(item);
      tr.appendChild(node('td', tvdbLabel(item)));
      const status = node('td', item.status);
      if (item.reservation_count) status.appendChild(node('small',
        item.reservation_count + ' reservation(s)', 'muted job-note'));
      if (item.manual_submission_count) status.appendChild(node('small',
        item.manual_submission_count + ' manual candidate(s)', 'muted job-note'));
      tr.appendChild(status);
      const actions = node('td');
      actions.appendChild(relatedJobsButton(anidbId, item));
      tr.appendChild(actions);
      list.appendChild(tr);
    }
    $('series-episodes-empty').hidden = series.episodes.length !== 0;
    state.manualTarget = null;
    $('series-manual-panel').hidden = true;
    const missing = $('series-missing'); missing.replaceChildren();
    for (const item of series.missing) {
      const tr = node('tr');
      const placed = item.season != null;
      tr.dataset.episode = placed ? tvdbLabel(item) : 'E' + item.anidb_episode;
      tr.appendChild(node('td', missingLabel(item)));
      const result = node('td', item.status.replaceAll('_', ' ').toLowerCase());
      if (item.reservation_count) result.appendChild(node('small',
        item.reservation_count + ' reservation(s)', 'muted job-note'));
      if (item.manual_submission_count) result.appendChild(node('small',
        item.manual_submission_count + ' manual candidate(s)', 'muted job-note'));
      if (item.reason_code) {
        result.appendChild(node('small', item.reason_code === 'TORRENT_METADATA_TIMEOUT'
          ? 'Torrent metadata timed out' : item.reason_code.replaceAll('_', ' ').toLowerCase(), 'muted job-note'));
      }
      if (item.waiting_job_ids?.length) {
        result.appendChild(node('small', item.waiting_job_ids.length + ' competing release(s) on hold', 'muted job-note'));
      }
      tr.appendChild(result);
      const action = node('td');
      if (placed) action.appendChild(relatedJobsButton(anidbId, item));
      if (placed && !series.rejection) action.appendChild(manualSubmissionButton(anidbId, item));
      if (item.job_id) {
        const view = node('button', 'View job', 'secondary');
        view.type = 'button';
        view.addEventListener('click', () => { location.assign('/jobs#job=' + item.job_id); });
        action.appendChild(view);
        if (item.can_retry_job || item.can_try_next) {
          for (const [name, label] of [
            ['retry', 'Retry acquisition'], ['cancel', 'Try next release']
          ]) {
            if (name === 'retry' && !item.can_retry_job ||
                name === 'cancel' && !item.can_try_next) continue;
            const button = node('button', label, 'secondary');
            button.type = 'button';
            button.addEventListener('click', async () => {
              if (state.seriesBusy) return;
              state.seriesBusy = true; button.disabled = true;
              try {
                // The Jobs API is authoritative for the current, guarded action.
                const job = await request(API + '/jobs/' + item.job_id);
                if (!job.actions?.[name]?.enabled ||
                    job.actions[name].execution_available === false) {
                  throw new Error('This job action is no longer available. Refresh Intake.');
                }
                if (name === 'cancel' && !confirm(
                  'Cancel this failed release and safely clean up its resources? ' +
                  'After reservation release, one waiting release will be tried.')) return;
                const body = name === 'retry'
                  ? {expected_attempt: job.attempt_count, expected_reason: job.reason?.code} : {};
                const receipt = await request(API + '/jobs/' + item.job_id + '/' + name,
                  {method: 'POST', body});
                notice(receipt.notification?.message || 'Episode recovery submitted.');
                dialog.close(); await loadActivity();
              } catch (error) { displayError(error); }
              finally { state.seriesBusy = false; button.disabled = false; }
            });
            action.appendChild(button);
          }
        }
      }
      if (item.can_retry) {
        const retry = node('button', item.status === 'NOT_SEARCHED' ? 'Search' :
          item.status === 'WAITING_FOR_DUB' ? 'Search again' : 'Retry search', 'secondary');
        retry.type = 'button';
        retry.addEventListener('click', async () => {
          if (state.seriesBusy) return;
          const held = item.status === 'WAITING_FOR_DUB';
          let deleteCache = true;
          if (held) {
            $('search-again-episode').textContent = missingLabel(item);
            $('search-again-delete').checked = false;
            const choice = $('search-again-dialog');
            choice.showModal();
            const confirmed = await new Promise(resolve => {
              choice.addEventListener('close', () => resolve(choice.returnValue === 'search'), {once: true});
            });
            if (!confirmed) return;
            deleteCache = $('search-again-delete').checked;
          }
          state.seriesBusy = true; retry.disabled = true;
          try {
            const data = await request(API + '/intake/series/' + anidbId +
              '/backfill/' + item.anidb_episode + '/retry',
              {method: 'POST', body: held ? {delete_cache: deleteCache} : {}});
            notice('Backfill ' + (data.reused ? 'already queued' : 'submitted') + '.');
            dialog.close(); await loadActivity();
          } catch (error) { displayError(error); }
          finally { state.seriesBusy = false; retry.disabled = false; }
        });
        action.appendChild(retry);
      }
      tr.appendChild(action); missing.appendChild(tr);
    }
    $('series-missing-empty').hidden = series.missing.length !== 0;
    const rejected = $('series-rejected');
    const controls = $('series-reject-controls');
    const reject = $('series-reject');
    const rejectHistory = $('series-reject-history');
    rejected.hidden = !series.rejection;
    controls.hidden = !!series.rejection;
    reject.hidden = !!series.rejection;
    rejectHistory.hidden = !series.rejection;
    if (series.rejection) {
      rejected.textContent = 'Rejected: ' + (series.rejection.reason || 'No reason recorded') +
        '. Cleanup: ' + series.rejection.cleanup_state.toLowerCase().replaceAll('_', ' ') +
        '. Future Intake is blocked.';
      rejectHistory.onclick = () => { dialog.close(); openHistory(series.rejection.job_id); };
    }
    reject.onclick = async () => {
      if (state.seriesBusy || series.rejection) return;
      const reason = $('series-reject-reason').value;
      const note = $('series-reject-note').value;
      const count = series.reject_operations.length;
      if (!confirm('Reject ' + series.title + '?\n\nThis blocks future Intake, cancels pending ' +
        'acquisitions, and removes ' + count + ' staged acquisition(s) and their owned torrents. ' +
        'Permanent library media is untouched.')) return;
      state.seriesBusy = true; reject.disabled = true;
      try {
        const result = await request(API + '/intake/series/' + anidbId + '/reject', {
          method: 'POST', body: {operation_ids: series.reject_operations, reason, note}
        });
        notice(result.message + ' Follow job #' + result.job_id + ' for cleanup status.');
        dialog.close(); await loadActivity();
      } catch (error) { displayError(error); }
      finally { state.seriesBusy = false; reject.disabled = false; }
    };
    const keep = $('series-keep');
    keep.disabled = !!series.rejection || series.keep_operations.length === 0;
    keep.hidden = !!series.rejection;
    keep.textContent = 'Keep all staged (' + series.keep_operations.length + ' acquisition' +
      (series.keep_operations.length === 1 ? '' : 's') + ')';
    keep.onclick = async () => {
      if (state.seriesBusy || !series.keep_operations.length) return;
      state.seriesBusy = true; keep.disabled = true;
      try {
        const result = await request(API + '/intake/series/' + anidbId + '/keep',
          {method: 'POST', body: {operation_ids: series.keep_operations}});
        notice(result.message + (result.failed.length ?
          ' ' + result.failed.length + ' acquisition(s) require another attempt.' : ''));
        dialog.close(); await loadActivity();
      } catch (error) { displayError(error); }
      finally { state.seriesBusy = false; }
    };
    if (!dialog.open) dialog.showModal();
    if (focusEpisode) {
      const row = [...list.children, ...missing.children].find(row => row.dataset.episode === focusEpisode);
      row?.scrollIntoView?.({block: 'nearest'});
    }
    if (recordLocation && window.history?.pushState) {
      window.history.pushState(null, '', '/#series=' + encodeURIComponent(anidbId));
    }
  } catch (error) { displayError(error); }
}
async function loadActivity() {
  if (state.loading) return state.loading;
  const generation = state.generation;
  state.loading = (async () => {
    try {
      // One search route; the page decides the scope and keeps its own filters.
      let path = state.search
        ? `${API}/search?${searchQuery(state.page === 'intake' ? 'intake' : 'jobs', state.search)}&before_id=${state.cursor}&limit=30`
        : `${API}/${state.page === 'intake' ? 'intake/series' : 'jobs'}?before_id=${state.cursor}&limit=30`;
      if (state.page === 'intake' && $('intake-filter').value) {
        path += '&status=' + encodeURIComponent($('intake-filter').value);
      }
      if (state.page === 'jobs') {
        if (state.episodeFilter) {
          for (const [key, value] of Object.entries(state.episodeFilter)) {
            path += '&' + key + '=' + encodeURIComponent(value);
          }
        }
        const filter = $('status-filter').value;
        if (filter) path += '&state=' + encodeURIComponent(filter);
        path += '&show_completed=' + Number($('show-completed').checked);
        path += '&show_canceled=' + Number($('show-canceled').checked);
      }
      const data = await request(path);
      if (generation !== state.generation || !state.csrf) return;
      state.jobs = data.items; state.next = data.next_before_id ?? null; renderJobs();
      $('no-jobs').textContent = state.search || (state.page === 'intake' && $('intake-filter').value)
        ? 'No matches.' : 'No items on this page.';
      $('live-indicator').textContent = '● Live'; $('live-indicator').className = 'status running';
    } catch (error) {
      if (generation === state.generation) {
        $('live-indicator').textContent = 'Updates paused'; $('live-indicator').className = 'status waiting';
        displayError(error);
      }
    }
  })();
  try { await state.loading; } finally { state.loading = null; }
}
async function tick() {
  if (state.csrf && state.page !== 'settings' && !document.hidden) await loadActivity();
  if (state.csrf) state.timer = setTimeout(tick, 5000);
}
async function openHistory(jobId) {
  state.historyJob = jobId; state.historyCursor = 0; state.historyNext = null; state.historyVersion += 1;
  $('history-title').textContent = `Job #${jobId} history`; $('history-rows').replaceChildren();
  if (!$('history-dialog').open) $('history-dialog').showModal();
  await loadHistory();
}
const ATTEMPT_STATES = Object.freeze({
  STAGED: 'Staged', FAILED: 'Failed', IN_PROGRESS: 'In progress', NOT_TRIED: 'Not tried yet',
});
// Every ranked release tried for this backfill episode, so a rejected
// release can be explained without querying the database.
function renderAttempts(found) {
  const section = node('section', null, 'trace-section attempts');
  section.appendChild(node('h3', `Releases tried for episode ${found.episode}`));
  for (const item of found.attempts) {
    const line = node('article', null, 'history-entry');
    const name = item.release_name || `Ranked release #${item.rank} (not sent yet)`;
    line.appendChild(node('p', `${item.rank}. ${name}`));
    const status = [ATTEMPT_STATES[item.state] || item.state];
    if (item.reason) status.push(item.reason.message);
    if (item.trace_id) status.push(item.trace_id);
    line.appendChild(node('small', status.join(' · '), item.state === 'FAILED' ? 'attempt-failed' : 'muted'));
    if (item.detail) line.appendChild(node('small', item.detail, 'muted'));
    section.appendChild(line);
  }
  $('history-rows').appendChild(section);
}
async function loadHistory() {
  const version = state.historyVersion; const job = state.historyJob;
  if (!job) return;
  $('history-more').disabled = true;
  try {
    const data = await request(`${API}/jobs/${job}/history?after_id=${state.historyCursor}&limit=50`);
    if (version !== state.historyVersion || !state.csrf) return;
    // Every line names the job that did the work (its *_job.py module).
    const worker = data.job?.module || data.job?.kind || `Job #${job}`;
    if (data.job) $('history-title').textContent = `Job #${job} · ${data.job.label?.message || data.job.kind} · ${worker}`;
    if (data.job && !state.historyCursor && Number.isInteger(data.job.poll_check_count)) {
      $('history-rows').appendChild(node('p',
        `${data.job.poll_check_count} deferred checks · ${data.job.work_attempt_count} other runs · ${data.job.scheduler_claim_count} total claims`,
        'muted'));
      if (data.job.cleanup_progress) {
        const p = data.job.cleanup_progress;
        $('history-rows').appendChild(node('p', `Cleanup checkpoint: ${p.phase} · Last advanced ${date(p.since_ms)}`, 'muted'));
      }
    }
    if (data.job?.kind === 'REJECT_SERIES' && !state.historyCursor) {
      $('history-rows').appendChild(node('p',
        'Rejected: ' + (data.job.rejection_reason || 'Reason not recorded') +
        ' · Cleanup: ' + (data.job.cleanup_state || 'Unknown'), 'muted'));
    }
    if (data.job?.target_job_id && !state.historyCursor) {
      const line = node('p', `Works on Job #${data.job.target_job_id}. Its history: `, 'muted');
      const open = node('button', `Job #${data.job.target_job_id}`, 'secondary'); open.type = 'button';
      open.addEventListener('click', () => openHistory(data.job.target_job_id));
      line.appendChild(open); $('history-rows').appendChild(line);
    }
    if (data.backfill_attempts?.attempts?.length) renderAttempts(data.backfill_attempts);
    for (const entry of data.items) {
      const article = node('article', null, 'history-entry');
      article.appendChild(node('small', date(entry.occurred_at_ms), 'muted'));
      article.appendChild(node('p', `${worker}: ${entry.event.message}`));
      if (entry.reason) { article.appendChild(node('p', entry.reason.message)); article.appendChild(node('small', entry.reason.action, 'muted')); }
      if (entry.detail) article.appendChild(node('p', entry.detail));
      $('history-rows').appendChild(article);
    }
    if (!data.items.length && !state.historyCursor) $('history-rows').appendChild(node('p', COPY['dashboard.no_history']));
    state.historyNext = data.next_after_id;
    $('history-more').hidden = state.historyNext === null;
  } catch (error) { displayError(error); }
  finally { if (version === state.historyVersion) $('history-more').disabled = false; }
}
const MATCHING_STEPS = Object.freeze({
  release_title: 'Release title parsed',
  recognized_season: 'Previously verified season',
  season_scope: 'Release season compatibility',
  anidb_candidates: 'AniDB exact title candidates',
  anidb_record: 'AniDB anime record',
  anime_lists: 'Anime-Lists identity mapping',
  tvdb_series: 'TVDB series verification',
  tvdb_animation: 'TVDB animation classification',
  provider_link: 'AniDB ↔ TVDB cross-reference',
  provider_titles: 'Provider title agreement',
  identity_verified: 'Anime identity verified',
});
const MATCHING_NOTES = Object.freeze({
  invalid_release_title: 'The incoming release title could not be parsed.',
  multiple_seasons: 'The release contains more than one explicit season.',
  not_previously_recognized: 'No previously verified mapping matches this title and season.',
  recognized_season_reused: 'Reused an exact, previously verified season mapping.',
  unverified_sequel: 'This unfamiliar later season cannot safely inherit Season 1’s AniDB identity. Season-specific proof is required.',
  base_series_season_lookup: 'No sequel title in the release; the base series will be identified first.',
  season_entry_selected: 'anime-lists selected the AniDB entry for this TVDB season.',
  no_title_aliases: 'No usable anime title could be extracted.',
  no_unique_anidb_candidate: 'AniDB returned zero or multiple exact title candidates.',
  no_anidb_candidates: 'AniDB returned no title candidates for the searched aliases.',
  multiple_anidb_candidates: 'AniDB returned multiple ambiguous candidates.',
  anidb_titles_disagree: 'The AniDB record does not corroborate the catalog titles.',
  no_anime_lists_mapping: 'No unique AniDB ↔ TVDB mapping was verified.',
  tvdb_live_action: 'TVDB identifies this work as live action.',
  tvdb_not_animation: 'TVDB did not confirm an animation classification.',
  tvdb_anidb_conflict: 'The TVDB series identifies a different AniDB work.',
  provider_titles_disagree: 'The independently fetched provider titles did not agree.',
  missing_verification: 'Required independent verification evidence was unavailable.',
  verified: 'Provider-verified identity established.',
  upstream_deferred: 'The provider call is waiting for a permitted retry.',
  upstream_unavailable: 'A provider could not be reached; the job may retry.',
  invalid_provider_response: 'The provider supplied an unusable response.',
  provider_auth_failed: 'The provider did not authorize this request.',
  identity_conflict: 'Independent identity facts disagreed.',
  canceled: 'Processing was canceled before verification completed.',
});
function matchingSection(root, title) {
  const section = node('section', null, 'trace-section');
  section.appendChild(node('h3', title)); root.appendChild(section);
  return section;
}
function matchingCard(root, label, value) {
  const card = node('div', null, 'trace-card');
  card.appendChild(node('span', label, 'trace-label'));
  card.appendChild(node('strong', value ?? 'Not recorded'));
  root.appendChild(card);
}
async function openCoverage(jobId) {
  const version = ++state.matchingVersion;
  state.matchingJob = jobId;
  $('matching-title').textContent = `Job #${jobId} · Episode coverage`;
  const summary = $('matching-summary'), body = $('matching-body');
  body.replaceChildren(); summary.textContent = 'Loading saved episode verification…';
  if (!$('matching-dialog').open) $('matching-dialog').showModal();
  try {
    const result = await request(`${API}/jobs/${jobId}/coverage`);
    if (version !== state.matchingVersion || state.matchingJob !== jobId || !state.csrf) return;
    summary.textContent = `Attempt ${result.attempts} · ${result.state.replaceAll('_', ' ').toLowerCase()}`;
    if (result.reason?.message) summary.textContent += ` · ${result.reason.message}`;
    if (!result.available || !result.evidence) {
      body.appendChild(node('p', 'No verified episode coverage was saved for this job.', 'muted'));
      return;
    }
    const ev = result.evidence;
    const heading = matchingSection(body, ev.kind === 'batch' ? 'Verified regular episodes (batch)' : 'Verified episode');
    heading.appendChild(node('p', `${ev.episode_count} episode(s) verified in TVDB season ${ev.tvdb_season}. This does not mean media was downloaded or staged.`, 'muted'));
    if (ev.kind === 'batch') {
      const message = ev.trial_plan_ready
        ? `${ev.planned_episode_count} episode(s) planned for the trial. Planned size: ${ev.planned_size_bytes} bytes. No downloads are authorized by this plan.`
        : 'No trial plan has been committed for this pack.';
      heading.appendChild(node('p', message, 'muted'));
    }
    const grid = node('div', null, 'trace-grid'); heading.appendChild(grid);
    for (const ep of ev.episodes || []) {
      const label = `TVDB S${String(ev.tvdb_season).padStart(2, '0')}E${String(ep.tvdb_episode).padStart(2, '0')}`;
      const detail = `AniDB episode ${ep.anidb_episode}${ep.file_index == null ? '' : ' · File #' + ep.file_index}`
        + (ev.kind === 'batch' && ev.trial_plan_ready ? (ep.planned ? ' · Trial planned' : ' · Outside trial') : '');
      matchingCard(grid, label, detail);
      if (ep.path) grid.appendChild(node('small', ep.path, 'muted'));
    }
    if (ev.truncated) body.appendChild(node('p', 'Showing the first 100 verified files.', 'muted'));
    if (ev.kind === 'batch') body.appendChild(node('p',
      'Extra files and actual download selection are not proved by this record. See the acquisition job for transfer status.',
      'muted'));
  } catch (error) {
    if (version === state.matchingVersion && state.matchingJob === jobId) {
      body.replaceChildren();
      summary.textContent = 'Episode coverage could not be loaded.';
      body.appendChild(node('p', error.message || 'Request failed.', 'muted'));
    }
  }
}
function manualMatchForm(root, result) {
  if (result.state !== 'NEEDS_REVIEW') return;
  const section = matchingSection(root, 'Correct anime identification');
  section.appendChild(node('p', 'Enter the series identity for this season. The same alias and season will reuse it on later retries. Episode placement remains independently verified. Leave season empty for unnumbered specials.', 'muted'));
  const form = document.createElement('form');
  const inputs = [
    ['title', 'Anime title', 'text', result.evidence?.tvdb_titles?.[0] || result.evidence?.queries?.[0] || ''],
    ['anidb_id', 'AniDB ID', 'number', result.evidence?.selected_anidb_id || ''],
    ['tvdb_series_id', 'TVDB series ID', 'number', result.evidence?.selected_tvdb_id || ''],
    ['season', 'Season (optional; leave blank for OVA/special)', 'number', result.evidence?.season ?? '']
  ];
  for (const [name, label, type, initial] of inputs) {
    const wrap = node('label', label);
    const field = document.createElement('input');
    field.name = name; field.type = type; field.required = name !== 'season';
    field.value = String(initial);
    if (type === 'number') { field.min = name === 'season' ? '0' : '1'; field.max = name === 'season' ? '999' : '9223372036854775807'; }
    wrap.appendChild(field); form.appendChild(wrap);
  }
  const submit = node('button', 'Save match and retry');
  submit.type = 'submit'; form.appendChild(submit);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (!confirm('Save this anime identity for other unresolved releases with the same name and season?')) return;
    const values = Object.fromEntries(new FormData(form));
    const body = {
      title: values.title.trim(), anidb_id: Number(values.anidb_id),
      tvdb_series_id: Number(values.tvdb_series_id), season: values.season.trim() === '' ? null : Number(values.season)
    };
    if (!Number.isSafeInteger(body.anidb_id) || !Number.isSafeInteger(body.tvdb_series_id)
        || (body.season !== null && !Number.isSafeInteger(body.season))) { notice('Invalid identity number.', true); return; }
    submit.disabled = true;
    try {
      await request(`${API}/jobs/${result.job_id}/manual-match`, {method: 'POST', body});
      await request(`${API}/jobs/${result.job_id}/retry`, {method: 'POST',
        body: {expected_attempt: result.attempts, expected_reason: result.reason?.code}});
      $('matching-dialog').close();
      notice('Match saved; identification queued for retry.');
      await loadActivity();
    } catch (error) {
      displayError(error);
    } finally { submit.disabled = false; }
  });
  section.appendChild(form);
}

async function openMatching(jobId) {
  const version = ++state.matchingVersion;
  state.matchingJob = jobId;
  $('matching-title').textContent = `Job #${jobId} · Matching details`;
  const summary = $('matching-summary'), body = $('matching-body');
  body.replaceChildren(); summary.textContent = 'Loading recorded identity evidence…';
  if (!$('matching-dialog').open) $('matching-dialog').showModal();
  try {
    const result = await request(`${API}/jobs/${jobId}/matching`);
    if (version !== state.matchingVersion || state.matchingJob !== jobId || !state.csrf) return;
    summary.textContent = `Attempt ${result.attempts} · ${result.state.replaceAll('_',' ').toLowerCase()}`;
    if (result.reason?.message) summary.textContent += ` · ${result.reason.message}`;
    manualMatchForm(body, result);
    if (!result.available || !result.evidence) {
      body.appendChild(node('p',
        result.attempts === 0 ? 'Identification has not started yet.' :
          'No detailed evidence was saved for this attempt. Older jobs cannot be reconstructed retroactively.',
        'muted'));
      return;
    }
    const ev = result.evidence;
    const lookup = matchingSection(body, 'Release and lookup');
    const lookupGrid = node('div', null, 'trace-grid'); lookup.appendChild(lookupGrid);
    matchingCard(lookupGrid, 'Release season', ev.season == null ? 'Not specified' : `Season ${ev.season}`);
    matchingCard(lookupGrid, 'Episode candidate', ev.episode == null ? 'Not parsed' : `Episode ${ev.episode}`);
    matchingCard(lookupGrid, 'AniDB search queries', ev.queries?.length ? ev.queries.join(' · ') : 'Not searched');
    const checks = matchingSection(body, 'Verification checks');
    if (!ev.checks?.length) checks.appendChild(node('p', 'No checks were recorded.', 'muted'));
    for (const check of ev.checks || []) {
      const line = node('div', null, 'trace-check');
      const symbols = {passed: '✓', failed: '✕', waiting: '…', skipped: '—'};
      const css = {passed: 'trace-pass', failed: 'trace-fail', waiting: 'trace-unknown', skipped: 'trace-unknown'};
      line.appendChild(node('span', symbols[check.status] || '?', 'trace-state ' + (css[check.status] || 'trace-unknown')));
      const content = node('div');
      content.appendChild(node('strong', MATCHING_STEPS[check.step] || 'Unknown check'));
      if (check.note) content.appendChild(node('small', MATCHING_NOTES[check.note] || 'No additional details recorded.', 'muted'));
      line.appendChild(content); checks.appendChild(line);
    }
    const candidates = matchingSection(body, 'AniDB candidates');
    if (!ev.anidb_candidates?.length)
      candidates.appendChild(node('p', 'No AniDB candidates recorded. The worker may have stopped before querying AniDB.', 'muted'));
    for (const candidate of ev.anidb_candidates || []) {
      const card = node('div', null, 'trace-card');
      card.appendChild(node('strong', `AniDB #${candidate.anidb_id}`));
      card.appendChild(node('p', candidate.titles?.length ? candidate.titles.join(' · ') : 'No provider titles recorded', 'muted'));
      candidates.appendChild(card);
    }
    const mapping = matchingSection(body, 'Verified provider identities');
    const mappingGrid = node('div', null, 'trace-grid'); mapping.appendChild(mappingGrid);
    matchingCard(mappingGrid, 'Selected AniDB work', ev.selected_anidb_id ? `AniDB #${ev.selected_anidb_id}` : 'Not verified');
    matchingCard(mappingGrid, 'Selected TVDB series', ev.selected_tvdb_id ? `TVDB #${ev.selected_tvdb_id}` : 'Not verified');
    matchingCard(mappingGrid, 'TVDB titles', ev.tvdb_titles?.length ? ev.tvdb_titles.join(' · ') : 'Not recorded');
    mapping.appendChild(node('p', 'Title agreement alone never authorizes an episode download. Exact season and episode mapping is verified separately.', 'muted'));
  } catch (error) {
    if (version === state.matchingVersion) {
      summary.textContent = 'Unable to load matching details.';
      body.replaceChildren(node('p', error.message || COPY['dashboard.request_failed'], 'muted'));
    }
  }
}

function settingInput(path, definition, value) {
  const label = node('label', null, 'setting-field'); label.appendChild(node('span', definition.label));
  const input = node(definition.type === 'title_language' ? 'select' :
    definition.type === 'levels' ? 'textarea' : 'input');
  input.name = path; input.dataset.kind = definition.type;
  if (definition.type === 'title_language') {
    for (const [code, text] of [['english', 'English'], ['japanese', 'Japanese (Romaji)']]) {
      const option = node('option', text); option.value = code; input.appendChild(option);
    }
    input.value = value;
  } else if (definition.type === 'levels') input.value = JSON.stringify(value, null, 2);
  else {
    input.type = definition.type === 'integer' ? 'number' : 'text'; input.value = String(value);
    if (definition.minimum !== null) input.min = String(definition.minimum);
    if (definition.maximum !== null) input.max = String(definition.maximum);
    if (definition.type === 'integer') { input.step = '1'; input.required = true; }
  }
  input.dataset.original = input.value;
  label.appendChild(input);
  label.appendChild(node('small', definition.help + (definition.apply_mode === 'restart' ? ' Requires a restart.' : ''), 'muted'));
  return label;
}
// Credentials are loaded when their integration is opened. Until then, no
// stored API key, token or download-client password is sent to the browser.
const CREDENTIAL_SPECS = [
  ['prowlarr', 'Prowlarr', [['api_key', 'API key']]],
  ['shoko', 'Shoko (optional)', [['api_key', 'API key']]],
  ['jellyfin', 'Jellyfin', [['api_key', 'API key']]],
  ['sonarr', 'Sonarr', [['api_key', 'API key']]],
  ['tvdb', 'TVDB', [['api_key', 'API key']]],
  ['qbittorrent', 'qBittorrent', [['username', 'Username'], ['password', 'Password']]],
  ['access', 'Autobrr webhook', [['webhook_token', 'Webhook token']]],
];
function settingChanged(input) { return input.value !== input.dataset.original; }
function credentialChanged(input) { return input.dataset.loaded === 'true' && input.value !== input._original; }
function refreshDirty() {
  const forms = [...$('settings-fields').querySelectorAll('form.integration')];
  state.dirty = forms.some(form => [...form.querySelectorAll('[data-kind]')].some(settingChanged) ||
    [...form.querySelectorAll('[data-secret]')].some(credentialChanged));
  for (const form of forms) {
    if (form._saveButton && !form._saving) {
      form._saveButton.disabled = ![...form.querySelectorAll('[data-kind]')].some(settingChanged) &&
        ![...form.querySelectorAll('[data-secret]')].some(credentialChanged);
    }
  }
}
function credentialEye(button, visible) {
  button.replaceChildren();
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', '20');
  svg.setAttribute('height', '20');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '2');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  const oval = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  oval.setAttribute('d', 'M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z');
  const pupil = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
  pupil.setAttribute('cx', '12'); pupil.setAttribute('cy', '12'); pupil.setAttribute('r', '3');
  svg.appendChild(oval); svg.appendChild(pupil);
  if (visible) {
    const strike = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    strike.setAttribute('d', 'M3 3l18 18');
    svg.appendChild(strike);
  }
  button.appendChild(svg);
  button.setAttribute('aria-label', visible ? 'Hide value' : 'Show value');
  button.setAttribute('title', visible ? 'Hide value' : 'Show value');
  button.setAttribute('aria-pressed', String(visible));
}
function wireSectionForm(form, section) {
  if (form._saveButton) return;
  const save = node('button', 'Save changes');
  save.type = 'submit'; save.disabled = true; form._saveButton = save;
  const result = node('p', '', 'form-status');
  result.setAttribute('role', 'status');
  form.appendChild(save); form.appendChild(result);
  form.addEventListener('input', () => { result.textContent = ''; refreshDirty(); });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (form._saving || save.disabled) return;
    const settingsInputs = [...form.querySelectorAll('[data-kind]')];
    const secretInputs = [...form.querySelectorAll('[data-secret]')];
    const changedSettings = settingsInputs.filter(settingChanged);
    const changedSecrets = secretInputs.filter(credentialChanged);
    if (!changedSettings.length && !changedSecrets.length) return;
    form._saving = true;
    save.disabled = true;
    result.textContent = '';
    for (const input of [...settingsInputs, ...secretInputs]) input.disabled = true;
    let settingsSaved = false;
    try {
      if (changedSettings.length) {
        const values = {};
        for (const input of changedSettings) {
          let value;
          try {
            value = input.dataset.kind === 'levels' ? JSON.parse(input.value) :
              input.dataset.kind === 'integer' ? Number(input.value) : input.value;
          } catch { throw new Error(COPY['dashboard.invalid_value']); }
          values[input.name.split('.')[1]] = value;
        }
        const saved = await request(API + '/settings', {method: 'PATCH',
          body: {expected_revision: state.settings.desired.revision, settings: {[section]: values}}});
        state.settings.desired = saved.configuration;
        for (const input of changedSettings) input.dataset.original = input.value;
        settingsSaved = true;
        result.textContent = !saved.durability_confirmed ? COPY['dashboard.saved_unconfirmed'] :
          saved.restart_fields.length ? COPY['dashboard.restart'] : COPY['dashboard.settings_saved'];
      }
      if (changedSecrets.length) {
        if (!secretInputs.every(input => input.dataset.loaded === 'true'))
          throw new Error('Wait for saved credentials to load.');
        const payload = {expected_revision: state.credentials.revision};
        for (const input of secretInputs) payload[input.name] = input.value;
        const saved = await request(API + '/credentials/' + section, {method: 'PATCH', body: payload});
        state.credentials = saved.status;
        for (const input of secretInputs) input._original = input.value;
        result.textContent = saved.durability_confirmed ? COPY['dashboard.settings_saved'] :
          COPY['dashboard.saved_unconfirmed'];
      }
    } catch (error) {
      result.textContent = (settingsSaved ? 'Settings saved, but credentials were not saved: ' : '') + error.message;
    } finally {
      form._saving = false;
      for (const input of settingsInputs) input.disabled = false;
      for (const input of secretInputs) input.disabled = input.dataset.loaded !== 'true';
      refreshDirty();
    }
  });
}
async function loadSettings() {
  const view = ++state.settingsVersion;
  try {
    const [settings, credentials] = await Promise.all([request(API + '/settings'), request(API + '/credentials')]);
    if (!state.csrf || view !== state.settingsVersion) return;
    state.settings = settings; state.credentials = credentials; state.dirty = false;
    $('restart-notice').textContent = settings.pending_restart_fields.length ? COPY['dashboard.restart'] : '';
    const container = $('settings-fields'); container.replaceChildren();
    for (const section of Object.keys(settings.desired.settings)) {
      const panel = node('details', null, 'panel'); panel.dataset.section = section;
      panel.appendChild(node('summary', section === 'trial' ? 'Intake selection' : section));
      const form = node('form', null, 'integration'); form.autocomplete = 'off';
      for (const [path, definition] of Object.entries(settings.fields)) {
        if (!path.startsWith(section + '.')) continue;
        form.appendChild(settingInput(path, definition, settings.desired.settings[section][path.split('.')[1]]));
      }
      panel.appendChild(form); container.appendChild(panel);
    }
    renderCredentials(view);
    refreshDirty();
  } catch (error) { displayError(error); }
}
function renderCredentials(view = state.settingsVersion) {
  const container = $('settings-fields');
  const panels = new Map([...container.children].map(panel => [panel.dataset.section, panel]));
  for (const [service, title, fields] of CREDENTIAL_SPECS) {
    let panel = panels.get(service);
    if (!panel) {
      panel = node('details', null, 'panel'); panel.dataset.section = service;
      panel.appendChild(node('summary', title)); container.appendChild(panel);
      panels.set(service, panel);
    }
    let form = [...panel.children].find(child => child.tagName?.toLowerCase() === 'form');
    if (!form) {
      form = node('form', null, 'integration'); form.autocomplete = 'off'; panel.appendChild(form);
    }
    const secretInputs = [];
    const toggles = [];
    for (const [name, label] of fields) {
      const wrapper = node('div', null, 'setting-field');
      const caption = node('label', label);
      const id = 'credential-' + service + '-' + name;
      caption.htmlFor = id; wrapper.appendChild(caption);
      const control = node('div', null, 'credential-input');
      const input = node('input');
      input.id = id; input.name = name; input.dataset.secret = name;
      input.type = name === 'username' ? 'text' : 'password';
      input.autocomplete = 'off'; input.maxLength = 4096;
      input.disabled = true; input.dataset.loaded = 'false'; input._original = '';
      control.appendChild(input); secretInputs.push(input);
      if (name !== 'username') {
        const toggle = node('button', null, 'credential-eye secondary');
        toggle.type = 'button'; credentialEye(toggle, false);
        toggle.addEventListener('click', () => {
          const showing = input.type === 'password';
          input.type = showing ? 'text' : 'password';
          credentialEye(toggle, showing);
        });
        control.appendChild(toggle); toggles.push([input, toggle]);
      }
      wrapper.appendChild(control); form.appendChild(wrapper);
    }
    let loaded = false, loading = null;
    const loadCredentials = async () => {
      if (loaded || loading || !state.csrf) return loading;
      loading = (async () => {
        try {
          const values = await request(API + '/credentials/' + service + '/reveal', {method: 'POST', body: {}});
          if (view !== state.settingsVersion || !state.csrf) return;
          for (const input of secretInputs) {
            const value = values[input.name];
            if (typeof value !== 'string') throw new Error('Invalid saved credentials response.');
          }
          for (const input of secretInputs) {
            input.value = values[input.name]; input._original = input.value;
            input.disabled = false; input.dataset.loaded = 'true';
          }
          loaded = true;
          refreshDirty();
        } catch (error) {
          if (view === state.settingsVersion) displayError(error);
        } finally { loading = null; }
      })();
      return loading;
    };
    panel.addEventListener('toggle', () => {
      if (panel.open) void loadCredentials();
      else for (const [input, toggle] of toggles) {
        input.type = 'password'; credentialEye(toggle, false);
      }
    });
    if (panel.open) void loadCredentials();
  }
  for (const panel of container.children) {
    const form = [...panel.children].find(child => child.tagName?.toLowerCase() === 'form');
    if (form) wireSectionForm(form, panel.dataset.section);
  }
}
async function loadRecognized() {
  const generation = state.generation;
  const table = $('recognized-rows');
  try {
    const result = await request(state.recognizedSearch
      ? `${API}/search?${searchQuery('recognized', state.recognizedSearch)}` : `${API}/recognized`);
    if (generation !== state.generation || !state.csrf) return;
    table.replaceChildren();
    for (const item of result.items) {
      const tr = node('tr');
      for (const value of [item.title, item.season, item.anidb_id, item.tvdb_series_id,
        item.alias_count, item.verified_episode_count]) {
        tr.appendChild(node('td', value));
      }
      const action = node('td');
      const button = node('button', 'Episodes', 'secondary');
      button.type = 'button';
      let detail = null;
      button.addEventListener('click', async () => {
        if (detail) { detail.remove(); detail = null; button.textContent = 'Episodes'; return; }
        button.disabled = true;
        detail = node('tr');
        const cell = node('td');
        cell.colSpan = 7;
        cell.textContent = 'Loading verified episode placements…';
        detail.appendChild(cell);
        tr.after(detail);
        try {
          const data = await request(`${API}/recognized/${item.tvdb_series_id}/${item.season}/${item.anidb_id}/episodes`);
          if (generation !== state.generation || !state.csrf) return;
          cell.replaceChildren();
          const summary = node('p', `${data.items.length} verified episode placements`);
          cell.appendChild(summary);
          for (const episode of data.items) {
            cell.appendChild(node('p',
              `AniDB #${episode.anidb_episode_id} (episode ${episode.anidb_episode_number}) → TVDB S${String(item.season).padStart(2,'0')}E${String(episode.tvdb_episode).padStart(2,'0')} (#${episode.tvdb_episode_id})`));
          }
          button.textContent = 'Hide episodes';
          button.disabled = false;
        } catch (error) {
          cell.textContent = error.message;
          button.textContent = 'Hide episodes';
          button.disabled = false;
        }
      });
      action.appendChild(button);
      tr.appendChild(action);
      table.appendChild(tr);
    }
    $('recognized-note').textContent = result.items.length
      ? `${result.items.length} verified series/season identities`
      : state.recognizedSearch ? 'No matches.' : 'No verified series/season identities yet.';
  } catch (error) {
    $('recognized-note').textContent = error.message;
  }
}
function searchQuery(scope, text) {
  return 'scope=' + encodeURIComponent(scope) + '&q=' + encodeURIComponent(text);
}
// Fewer than two characters means no search; the normal list returns.
function searchText(value) {
  const text = String(value || '').trim().slice(0, 100);
  return text.length >= 2 ? text : '';
}
async function applySearch() {
  if (!state.csrf) return;
  if (state.page === 'recognized') {
    const text = searchText($('recognized-search').value);
    if (text === state.recognizedSearch) return;
    state.recognizedSearch = text; await loadRecognized(); return;
  }
  const text = searchText($('search').value);
  if (text === state.search) return;
  if (state.loading) await state.loading;
  state.search = text; state.previous = []; state.cursor = MAX_CURSOR; state.next = null;
  await loadActivity();
}
function queueSearch() {
  clearTimeout(state.searchTimer);
  state.searchTimer = setTimeout(applySearch, 250);
}
async function showWorkspace() {
  $('login-panel').hidden = true; $('workspace').hidden = false; $('sign-out').hidden = false;
  $('activity-panel').hidden = state.page === 'settings' || state.page === 'recognized';
  $('settings-panel').hidden = state.page !== 'settings';
  $('recognized-panel').hidden = state.page !== 'recognized';
  for (const link of document.querySelectorAll('nav a')) link.classList.toggle('selected', new URL(link.href).pathname === location.pathname);
  if (state.page === 'settings') { await loadSettings(); return; }
  if (state.page === 'recognized') { await loadRecognized(); return; }
  if (state.page === 'jobs' && state.search && $('search').value !== state.search) {
    $('search').value = state.search;
  }
  $('page-title').textContent = state.page === 'intake' ? 'Intake' : 'Jobs';
  $('activity-title').textContent = state.page === 'intake' ? 'Incoming anime' : 'Job activity';
  $('page-description').textContent = state.page === 'intake' ? 'Follow incoming anime through processing and staging.' : 'Track jobs and their recorded history. Status updates every five seconds.';
  $('page-note').textContent = state.page === 'intake' ?
    COPY['dashboard.scope'] : state.episodeFilter ?
    'Related jobs for AniDB #' + state.episodeFilter.anidb_id + ' · TVDB ' +
      tvdbLabel({season: state.episodeFilter.season, episode: state.episodeFilter.episode}) +
      '. Includes released reservations and completed jobs.' :
    'Filters apply across all jobs. Up to 100 jobs can be selected across pages; up to five use individual actions, larger selections use a queued batch.';
  $('bulk-heading').hidden = state.page !== 'jobs';
  $('completed-toggle-label').hidden = state.page !== 'jobs';
  $('canceled-toggle-label').hidden = state.page !== 'jobs';
  $('status-filter-label').hidden = state.page === 'intake';
  $('intake-filter-label').hidden = state.page !== 'intake';
  $('search').placeholder = state.page === 'intake' ? 'Search anime' : 'Search jobs: release, anime, activity or #id';
  if (state.episodeFilter) {
    $('show-completed').checked = true;
    $('show-canceled').checked = true;
  }
  if (state.page === 'intake') {
    const titles = ['Received', 'Series', 'Staged', 'Status', 'Backfill', 'Actions'];
    const names = ['received', 'series', 'staged', 'status', 'backfill', 'actions'];
    for (let index = 0; index < titles.length; index++) $('activity-col-' + names[index]).textContent = titles[index];
  }
  await loadActivity();
  if (state.page === 'intake') {
    const params = new URLSearchParams((location.hash || '').slice(1));
    const id = params.get('series');
    if (id && /^[1-9]\d{0,17}$/.test(id)) {
      void openSeries(id, false, params.get('episode'));
    }
  }
  clearTimeout(state.timer); if (state.csrf) state.timer = setTimeout(tick, 5000);
}
$('login-form').addEventListener('submit', async event => {
  event.preventDefault(); const button = event.currentTarget.querySelector('button'); button.disabled = true;
  try {
    const result = await request(`${API}/auth/session`, {method: 'POST', body: {password: $('access-key').value}});
    $('access-key').value = ''; state.csrf = result.csrf_token; notice(); await showWorkspace();
  } catch (error) { $('access-key').value = ''; displayError(error); }
  finally { button.disabled = false; }
});
$('password-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button');
  button.disabled = true;
  try {
    if (form.elements.namedItem('new_password').value !== form.elements.namedItem('confirm_password').value) {
      throw new Error('New passwords do not match.');
    }
    await request(`${API}/auth/password`, {method: 'POST', body: {
      current_password: form.elements.namedItem('current_password').value,
      new_password: form.elements.namedItem('new_password').value
    }});
    form.reset();
    clearPrivate('Password changed. Sign in again.');
  } catch (error) { form.reset(); displayError(error); }
  finally { button.disabled = false; }
});
$('sign-out').addEventListener('click', async () => {
  try { await request(`${API}/auth/session`, {method: 'DELETE', body: {}}); clearPrivate(); }
  catch (error) { displayError(error); }
});
$('refresh').addEventListener('click', () => loadActivity());
for (const id of ['search', 'recognized-search']) {
  $(id).addEventListener('input', queueSearch);
  $(id).addEventListener('keydown', event => {
    if (event.key === 'Escape') { $(id).value = ''; clearTimeout(state.searchTimer); applySearch(); }
    if (event.key === 'Enter') { clearTimeout(state.searchTimer); applySearch(); }
  });
}
$('refresh-recognized').addEventListener('click', () => loadRecognized());
$('reload-settings').addEventListener('click', async () => {
  if (state.dirty && !confirm(COPY['dashboard.discard'])) return;
  await loadSettings();
});
$('status-filter').addEventListener('change', () => {
  if (state.page !== 'jobs') { renderJobs(); return; }
  if ($('status-filter').value === 'DONE') $('show-completed').checked = true;
  if ($('status-filter').value === 'CANCELED') $('show-canceled').checked = true;
  resetJobsPage();
});
$('show-completed').addEventListener('change', resetJobsPage);
$('intake-filter').addEventListener('change', async () => {
  if (!state.csrf || state.page !== 'intake') return;
  if (state.loading) await state.loading;
  state.previous = []; state.cursor = MAX_CURSOR; state.next = null;
  await loadActivity();
});
$('show-canceled').addEventListener('change', resetJobsPage);
$('select-page').addEventListener('change', () => {
  if (state.bulkSubmitting || state.page !== 'jobs') return;
  if ($('select-page').checked) {
    for (const job of state.jobs) {
      if (state.selected.size >= 100) { notice(COPY['dashboard.bulk_limit'], true); break; }
      state.selected.add(job.job_id);
    }
  } else {
    for (const job of state.jobs) state.selected.delete(job.job_id);
  }
  renderJobs();
});
$('bulk-clear').addEventListener('click', () => {
  if (state.bulkSubmitting) return;
  state.selected.clear();
  renderJobs();
});
$('bulk-submit').addEventListener('click', submitSelected);
$('next-page').addEventListener('click', async () => {
  if (state.next === null || state.loading) return;
  state.previous.push(state.cursor); state.cursor = String(state.next); await loadActivity();
});
$('previous-page').addEventListener('click', async () => {
  if (!state.previous.length || state.loading) return;
  state.cursor = state.previous.pop(); await loadActivity();
});
$('history-close').addEventListener('click', () => $('history-dialog').close());
$('matching-close').addEventListener('click', () => $('matching-dialog').close());
$('series-manual-source').addEventListener('change', () => {
  const file = $('series-manual-source').value === 'torrent';
  $('series-manual-magnet-label').hidden = file;
  $('series-manual-torrent-label').hidden = !file;
});
$('series-manual-dismiss').addEventListener('click', () => {
  $('series-manual-panel').hidden = true;
  state.manualTarget = null;
});
$('series-manual-form').addEventListener('submit', async event => {
  event.preventDefault();
  const target = state.manualTarget;
  if (!target || state.seriesBusy) return;
  state.seriesBusy = true;
  const submit = $('series-manual-submit'); submit.disabled = true;
  try {
    const body = {release_name: $('series-manual-title').value.trim()};
    if ($('series-manual-source').value === 'magnet') {
      body.magnet_uri = $('series-manual-magnet').value.trim();
    } else {
      const file = $('series-manual-file').files?.[0];
      if (!file || !file.name.toLowerCase().endsWith('.torrent') ||
          file.size < 1 || file.size > 4194304) {
        throw new Error('Choose a .torrent file no larger than 4 MiB.');
      }
      const bytes = new Uint8Array(await file.arrayBuffer());
      let binary = '';
      for (let index = 0; index < bytes.length; index += 16384) {
        binary += String.fromCharCode(...bytes.subarray(index, index + 16384));
      }
      body.torrent_b64 = btoa(binary);
    }
    const result = await request(API + '/intake/series/' + target.anidbId +
      '/episodes/' + target.season + '/' + target.episode + '/submit',
      {method: 'POST', body});
    notice(result.message + ' Job #' + result.job_id + ' · Open Related jobs to track it.');
    $('series-manual-panel').hidden = true;
    state.manualTarget = null;
    $('series-dialog').close();
    await loadActivity();
  } catch (error) { displayError(error); }
  finally { state.seriesBusy = false; submit.disabled = false; }
});
$('series-close').addEventListener('click', () => $('series-dialog').close());
$('series-dialog').addEventListener('close', () => {
  if (state.page === 'intake' && new URLSearchParams((location.hash || '').slice(1)).has('series')) {
    window.history?.replaceState?.(null, '', '/');
  }
});
window.addEventListener('popstate', () => {
  if (state.page !== 'intake' || !state.csrf) return;
  const params = new URLSearchParams((location.hash || '').slice(1));
  const id = params.get('series');
  if (!id && $('series-dialog').open) $('series-dialog').close();
  else if (id && /^[1-9]\d{0,17}$/.test(id)) void openSeries(id, false, params.get('episode'));
});
$('matching-dialog').addEventListener('close', () => {
  state.matchingJob = null; state.matchingVersion += 1;
  $('matching-body').replaceChildren();
});
$('history-dialog').addEventListener('close', () => { state.historyJob = null; state.historyVersion += 1; });
$('history-more').addEventListener('click', async () => {
  if (state.historyNext === null) return; state.historyCursor = state.historyNext; await loadHistory();
});
window.addEventListener('beforeunload', event => { if (state.dirty) { event.preventDefault(); event.returnValue = ''; } });
document.addEventListener('visibilitychange', () => { if (!document.hidden && state.csrf && state.page !== 'settings') loadActivity(); });
(async () => {
  try { const session = await request(`${API}/auth/session`); state.csrf = session.csrf_token; await showWorkspace(); }
  catch (error) { clearPrivate(error.status === 401 ? '' : error.message); }
})();
