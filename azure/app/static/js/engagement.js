/*
 * Viewer engagement emitter for the Azure-served scoreboard.
 *
 * Sends batched JSON to /m/{meet_id}/api/telemetry (proxied through the
 * server so no Azure credentials touch the browser). Designed to be cheap:
 * single sessionStorage UUID, page-visible+focused 30s heartbeat, batch
 * flushes every 5s or 10 events or on visibility hide. Self-disables when
 * window.__ENGAGEMENT is missing.
 */
(function () {
  'use strict';
  var CFG = window.__ENGAGEMENT;
  if (!CFG || !CFG.telemetry_endpoint || !CFG.meet_id) {
    return;
  }

  var ENDPOINT = CFG.telemetry_endpoint;
  var FLUSH_MS = 5000;
  var FLUSH_MAX = 10;
  var HEARTBEAT_MS = 30000;

  // Stable per-tab id; survives navigation within the tab but not a new
  // tab/window. That's deliberate: we want concurrent-viewer counting to
  // treat tabs as distinct.
  var viewerId = '';
  try {
    viewerId = sessionStorage.getItem('cts.viewer_id') || '';
    if (!viewerId) {
      viewerId = (crypto && crypto.randomUUID) ? crypto.randomUUID()
        : ('v-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10));
      sessionStorage.setItem('cts.viewer_id', viewerId);
    }
  } catch (e) {
    viewerId = 'v-anon-' + Math.random().toString(36).slice(2, 10);
  }

  var queue = [];
  var flushTimer = null;

  function isActive() {
    return document.visibilityState === 'visible' && document.hasFocus();
  }

  function enqueue(name, props) {
    queue.push({
      name: name,
      props: Object.assign({
        viewer_id: viewerId,
        ts_ms: Date.now(),
      }, props || {}),
    });
    if (queue.length >= FLUSH_MAX) {
      flush();
    } else if (!flushTimer) {
      flushTimer = setTimeout(flush, FLUSH_MS);
    }
  }

  function flush(useBeacon) {
    if (flushTimer) { clearTimeout(flushTimer); flushTimer = null; }
    if (!queue.length) return;
    var body = JSON.stringify({ events: queue });
    queue = [];
    try {
      if (useBeacon && navigator.sendBeacon) {
        navigator.sendBeacon(ENDPOINT, new Blob([body], { type: 'application/json' }));
        return;
      }
      fetch(ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: body,
        keepalive: true,
        credentials: 'omit',
      }).catch(function () { /* swallow; telemetry must never break the page */ });
    } catch (e) { /* same */ }
  }

  // --- page_load with LCP + connection info ----------------------------------
  var loadedAt = Date.now();
  var navEntry = (performance.getEntriesByType && performance.getEntriesByType('navigation')[0]) || null;
  var conn = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
  enqueue('viewer_page_load', {
    referrer: document.referrer || '',
    ua: navigator.userAgent,
    tz: (Intl.DateTimeFormat().resolvedOptions().timeZone) || '',
    screen_w: window.screen ? window.screen.width : 0,
    screen_h: window.screen ? window.screen.height : 0,
    viewport_w: window.innerWidth,
    viewport_h: window.innerHeight,
    effective_type: conn ? (conn.effectiveType || '') : '',
    downlink: conn ? (conn.downlink || 0) : 0,
    nav_type: navEntry ? navEntry.type : '',
  });

  // LCP, reported once when observation finishes (visibility hide or unload).
  var lcpValue = 0;
  try {
    if (window.PerformanceObserver && PerformanceObserver.supportedEntryTypes
        && PerformanceObserver.supportedEntryTypes.indexOf('largest-contentful-paint') >= 0) {
      var lcpObs = new PerformanceObserver(function (list) {
        var entries = list.getEntries();
        if (entries.length) lcpValue = entries[entries.length - 1].startTime;
      });
      lcpObs.observe({ type: 'largest-contentful-paint', buffered: true });
    }
  } catch (e) { /* no LCP support */ }

  // --- heartbeats while visible+focused --------------------------------------
  var lastHeartbeat = 0;
  function heartbeatTick() {
    if (isActive()) {
      enqueue('viewer_heartbeat', {
        tenure_ms: Date.now() - loadedAt,
      });
      lastHeartbeat = Date.now();
    }
  }
  setInterval(heartbeatTick, HEARTBEAT_MS);

  // --- scoreboard event changes ---------------------------------------------
  window.addEventListener('scoreboard:event-changed', function (ev) {
    var d = (ev && ev.detail) || {};
    enqueue('viewer_event_view', {
      current_event: d.current_event || '',
      current_heat: d.current_heat || '',
      event_name: d.event_name || '',
      distance: d.dims ? (d.dims.distance || 0) : 0,
      stroke_code: d.dims ? (d.dims.stroke_code || 0) : 0,
      stroke_name: d.dims ? (d.dims.stroke_name || '') : '',
      age_min: d.dims ? (d.dims.age_min || 0) : 0,
      age_max: d.dims ? (d.dims.age_max || 0) : 0,
      age_group: d.dims ? (d.dims.age_group_label || '') : '',
      gender: d.dims ? (d.dims.gender_agnostic || '') : '',
      relay: d.dims ? !!d.dims.relay : false,
    });
  });

  // --- visibility / unload ---------------------------------------------------
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden') {
      enqueue('viewer_hidden', {
        tenure_ms: Date.now() - loadedAt,
        lcp_ms: Math.round(lcpValue),
      });
      flush(true);
    } else {
      enqueue('viewer_visible', { tenure_ms: Date.now() - loadedAt });
    }
  });
  window.addEventListener('pagehide', function () {
    enqueue('viewer_unload', {
      tenure_ms: Date.now() - loadedAt,
      lcp_ms: Math.round(lcpValue),
    });
    flush(true);
  });
})();
