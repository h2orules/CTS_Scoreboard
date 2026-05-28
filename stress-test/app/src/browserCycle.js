import { chromium } from 'playwright';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const randInt = (min, max) => Math.floor(Math.random() * (max - min + 1)) + min;

const LAUNCH_ARGS = [
  '--no-sandbox',
  '--disable-dev-shm-usage',
  '--disable-gpu',
  '--no-zygote',
  '--mute-audio',
];

// Wire WebSocket observers and resolve once we see the Socket.IO `/scoreboard`
// namespace handshake frame ("40/scoreboard,..."). Reject after timeoutMs.
function waitForScoreboardWs(page, timeoutMs) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error(`ws-timeout after ${timeoutMs}ms`)),
      timeoutMs,
    );
    page.on('websocket', (ws) => {
      ws.on('framereceived', ({ payload }) => {
        const s = typeof payload === 'string' ? payload : payload.toString('utf8');
        // Engine.IO open packet ("0{...}") then Socket.IO namespace ack ("40/scoreboard,").
        if (s.startsWith('40/scoreboard') || s === '40') {
          clearTimeout(timer);
          resolve(ws.url());
        }
      });
      ws.on('socketerror', () => { /* surfaced via metrics */ });
    });
  });
}

export async function browserCycle({
  id,
  targetUrl,
  mode,
  minHold,
  maxHold,
  minDelay,
  maxDelay,
  deadline,
  log,
  metrics,
}) {
  let cycle = 0;
  let persistent;

  while (Date.now() < deadline) {
    cycle++;
    const useFresh = mode === 'cold' || (mode === 'mixed' && cycle % 2 === 1);
    let browser, context, page;
    const t0 = Date.now();

    try {
      if (useFresh) {
        browser = await chromium.launch({ args: LAUNCH_ARGS, headless: true });
        context = await browser.newContext();
      } else {
        if (!persistent) {
          persistent = await chromium.launchPersistentContext(`/tmp/pw-${id}`, {
            args: LAUNCH_ARGS,
            headless: true,
          });
        }
        context = persistent;
      }

      page = await context.newPage();
      page.setDefaultTimeout(45000);

      const wsPromise = waitForScoreboardWs(page, 45000);
      await page.goto(targetUrl, { waitUntil: 'domcontentloaded', timeout: 45000 });
      const wsUrl = await wsPromise;
      const ttws = Date.now() - t0;

      metrics.connects++;
      metrics.ttwsSumMs += ttws;
      log.info({ id, cycle, kind: useFresh ? 'cold' : 'hot', ttws, wsUrl }, 'connected');

      const holdMs = randInt(minHold, maxHold) * 1000;
      // Don't hold past the deadline.
      const remaining = Math.max(0, deadline - Date.now());
      await sleep(Math.min(holdMs, remaining));

      await page.close();
    } catch (err) {
      metrics.failures++;
      log.warn({ id, cycle, err: err.message }, 'cycle-failed');
    } finally {
      if (page && !page.isClosed()) {
        await page.close().catch(() => {});
      }
      if (useFresh && browser) {
        await browser.close().catch(() => {});
      }
    }

    if (Date.now() >= deadline) break;
    const delayMs = randInt(minDelay, maxDelay) * 1000;
    if (Date.now() + delayMs >= deadline) break;
    await sleep(delayMs);
  }

  if (persistent) {
    await persistent.close().catch(() => {});
  }
}
