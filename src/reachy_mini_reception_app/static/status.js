/* Display server facts only; official app lifecycle owns Start and Stop. */
const phases = {starting: 'Starting', ready: 'Ready', stopping: 'Stopping', stopped: 'Stopped', faulted: 'Needs attention'};
const reasons = {
  native_stop: 'Stopped from the control app', runtime_completed: 'Run completed',
  runtime_failed: 'Runtime fault; operator review required', stop_timeout: 'Cleanup not confirmed; operator review required',
  control_disconnected: 'Control connection closed', control_heartbeat_timeout: 'Control heartbeat expired',
  control_connection_failed: 'Service connection lost', service_shutdown: 'Service stopped',
  cleanup_pending: 'Previous run is still cleaning up; retry Start after it finishes',
  operator_review_required: 'Previous run cleanup failed or is unverified; operator review required',
  service_closing: 'Reception service is shutting down',
  control_protocol_error: 'Reception service rejected the control request',
  stopped_before_connect: 'Stopped before connecting', stopped_before_start: 'Stopped before starting'
};
const text = (id, value) => { document.getElementById(id).textContent = value ?? '--'; };
function reading(id, value, tone = '') {
  text(id, value);
  document.getElementById(id).dataset.tone = tone;
}
function elapsed(value) {
  if (!Number.isFinite(value)) return '--:--:--';
  const seconds = Math.max(0, Math.floor(value));
  return [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60].map(v => String(v).padStart(2, '0')).join(':');
}
function render(status) {
  if (!status || !phases[status.phase]) throw new Error('Invalid status');
  const stale = status.control_age_s != null && status.control_age_s > 4 && !['stopped', 'faulted'].includes(status.phase);
  text('phase', stale ? 'Status stale' : phases[status.phase]);
  document.getElementById('phase').dataset.phase = stale ? 'unavailable' : status.phase;
  reading('connection', stale ? 'Status stale' : 'Connected', stale ? 'warn' : 'good');
  text('reason', reasons[status.reason] || (status.reason ? 'Operator review required' : ''));
  text('elapsed_s', elapsed(status.elapsed_s));
  for (const key of ['config_id', 'robot_id', 'run_id']) text(key, status[key]);
  const config = status.configuration || {};
  for (const key of ['profile', 'tools', 'vision_policy', 'vision_runtime']) text(key, config[key]);
  text('duration', 'duration_s' in config ? (config.duration_s == null ? 'Until stopped' : `${elapsed(config.duration_s)} scheduled`) : null);
  for (const key of ['record_audio', 'record_video', 'capture_vision']) {
    text(key, typeof config[key] === 'boolean' ? (config[key] ? 'Enabled' : 'Disabled') : null);
  }
  const health = status.health || {};
  text('runtime_phase', health.runtime_phase?.replaceAll('_', ' '));
  const active = status.phase === 'ready' && !stale;
  for (const name of ['audio', 'video']) {
    const source = health[name];
    const age = source?.age_s;
    const observed = Number.isFinite(age) && source?.sequence > 0;
    const fresh = observed && age <= 8;
    const value = !source ? '--' : !source.expected ? 'Not required' : !active ? 'Not live' : !observed ? 'Waiting' : fresh ? 'Receiving' : 'Stale';
    reading(name, value, active && source?.expected ? (fresh ? 'good' : 'warn') : '');
    text(`${name}_detail`, observed ? `${source.sequence.toLocaleString()} frames; ${age.toFixed(1)}s ago` : null);
  }
  const age = health.event_loop_age_s;
  const known = Number.isFinite(age);
  reading('loop', !active ? 'Not live' : !known ? 'Waiting' : age <= 8 ? 'Advancing' : 'Stale', active ? (known && age <= 8 ? 'good' : 'warn') : '');
  text('loop_detail', known ? `Last pulse ${age.toFixed(1)}s ago` : null);
  text('updated', `Status received ${new Date().toLocaleTimeString()}`);
}
async function refresh() {
  try {
    const response = await fetch('/api/reception/status', {cache: 'no-store', signal: AbortSignal.timeout(3000)});
    if (!response.ok) throw new Error('Status unavailable');
    render(await response.json());
  } catch {
    text('phase', 'Status unavailable');
    document.getElementById('phase').dataset.phase = 'unavailable';
    reading('connection', 'Disconnected', 'bad');
    text('reason', 'Last received configuration and run details');
    for (const name of ['audio', 'video', 'loop']) {
      reading(name, 'Unknown');
      text(`${name}_detail`, null);
    }
  } finally {
    setTimeout(refresh, 1000);
  }
}
refresh();
