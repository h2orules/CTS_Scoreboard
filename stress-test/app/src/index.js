import pino from 'pino';
import { browserCycle } from './browserCycle.js';

const log = pino({ level: process.env.LOG_LEVEL || 'info' });

function envInt(name, dflt) {
  const v = process.env[name];
  if (v === undefined || v === '') return dflt;
  const n = Number.parseInt(v, 10);
  if (Number.isNaN(n)) throw new Error(`env ${name} is not an integer: ${v}`);
  return n;
}

function envEnum(name, allowed, dflt) {
  const v = process.env[name] || dflt;
  if (!allowed.includes(v)) throw new Error(`env ${name}=${v} not in ${allowed.join(',')}`);
  return v;
}

const TARGET_URL = process.env.TARGET_URL;
if (!TARGET_URL) {
  log.error('TARGET_URL is required');
  process.exit(2);
}

const N = envInt('BROWSERS_PER_REPLICA', 10);
const MIN_HOLD = envInt('MIN_HOLD_SECONDS', 30);
const MAX_HOLD = envInt('MAX_HOLD_SECONDS', 120);
const MIN_DELAY = envInt('MIN_DELAY_SECONDS', 2);
const MAX_DELAY = envInt('MAX_DELAY_SECONDS', 10);
const MODE = envEnum('CACHE_MODE', ['hot', 'cold', 'mixed'], 'mixed');
const TOTAL_DURATION = envInt('TOTAL_DURATION_SECONDS', 600);

if (MIN_HOLD > MAX_HOLD || MIN_DELAY > MAX_DELAY) {
  log.error('MIN_* values must be <= MAX_* values');
  process.exit(2);
}

// ACA exposes the replica name, not an index. Parse the trailing token for log
// correlation; full name pattern: <job>--<execName>-<replicaSuffix>.
const replicaName = process.env.CONTAINER_APP_REPLICA_NAME || 'local';
const replicaTag = replicaName.split('--').pop();

const metrics = { connects: 0, failures: 0, ttwsSumMs: 0 };
const deadline = Date.now() + TOTAL_DURATION * 1000;

log.info(
  {
    replica: replicaTag,
    targetUrl: TARGET_URL,
    browsers: N,
    mode: MODE,
    minHold: MIN_HOLD,
    maxHold: MAX_HOLD,
    minDelay: MIN_DELAY,
    maxDelay: MAX_DELAY,
    durationSec: TOTAL_DURATION,
  },
  'replica-start',
);

// Stagger startup so all N browsers don't fire cold opens in the same instant.
const startGapMs = N > 1 ? Math.floor(10000 / N) : 0;
const startedAt = Date.now();

const tasks = [];
for (let i = 0; i < N; i++) {
  const id = `${replicaTag}-${i.toString().padStart(3, '0')}`;
  const child = log.child({ vu: id });
  // Schedule each VU with its own offset; they share `metrics` and `deadline`.
  tasks.push(
    new Promise((resolve) => {
      setTimeout(() => {
        browserCycle({
          id,
          targetUrl: TARGET_URL,
          mode: MODE,
          minHold: MIN_HOLD,
          maxHold: MAX_HOLD,
          minDelay: MIN_DELAY,
          maxDelay: MAX_DELAY,
          deadline,
          log: child,
          metrics,
        }).then(resolve, (err) => {
          log.error({ id, err: err.message }, 'vu-crashed');
          resolve();
        });
      }, i * startGapMs);
    }),
  );
}

const heartbeat = setInterval(() => {
  const elapsed = Math.round((Date.now() - startedAt) / 1000);
  const avgTtws = metrics.connects > 0 ? Math.round(metrics.ttwsSumMs / metrics.connects) : 0;
  log.info(
    {
      replica: replicaTag,
      elapsedSec: elapsed,
      connects: metrics.connects,
      failures: metrics.failures,
      avgTtwsMs: avgTtws,
    },
    'heartbeat',
  );
}, 10000);

// Best-effort SIGTERM handling so `az containerapp job execution stop` exits cleanly.
let stopping = false;
const handleStop = (sig) => {
  if (stopping) return;
  stopping = true;
  log.warn({ sig }, 'stop-signal');
  // Force the deadline to "now" so each VU breaks out of its loop.
  // We can't easily mutate the closed-over const, so just let the process exit
  // after a short grace period; ACA SIGKILLs after 30s anyway.
  setTimeout(() => process.exit(0), 5000);
};
process.on('SIGTERM', () => handleStop('SIGTERM'));
process.on('SIGINT', () => handleStop('SIGINT'));

await Promise.allSettled(tasks);
clearInterval(heartbeat);

const elapsed = Math.round((Date.now() - startedAt) / 1000);
const avgTtws = metrics.connects > 0 ? Math.round(metrics.ttwsSumMs / metrics.connects) : 0;
log.info(
  {
    replica: replicaTag,
    elapsedSec: elapsed,
    connects: metrics.connects,
    failures: metrics.failures,
    avgTtwsMs: avgTtws,
  },
  'replica-done',
);
process.exit(0);
