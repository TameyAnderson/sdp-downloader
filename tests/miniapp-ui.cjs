// Offline UI regression test. Requires Node.js + playwright + Chromium (or Edge).
// Run: node tests/miniapp-ui.cjs
// Optional: PLAYWRIGHT_CHANNEL=msedge, MINIAPP_SCREENSHOT_DIR=<output directory>.
// All HTTP requests are intercepted; no bot, Telegram account or credentials needed.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { chromium } = require('playwright');
const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const materialBundle = fs.readFileSync(path.join(__dirname, '..', 'static', 'material.js'), 'utf8');
const screenshotDir = process.env.MINIAPP_SCREENSHOT_DIR;
let checks = 0;
function check(value, message) { assert.ok(value, message); checks++; }

(async () => {
  const browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHANNEL ? { channel: process.env.PLAYWRIGHT_CHANNEL } : {}) });
  try {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, colorScheme: 'light', reducedMotion: 'reduce' });
    const posts = [], errors = [];
    let forbidden = false, empty = false, failDownload = false, failSettings = false;
    const settings = { lang: 'uk', whitelist: '1', notify_no_access: '0', cache: '1', engine_pref: 'auto', titles_mode: 'private', audio_too: '0',
      flags: { thumbnails: '1', eng_cobalt: '1' }, tunables: { max_height: 1080, cache_max: 200, cookies_warn_days: 7 },
      cache_info: { entries: 12, max: 200, bytes: 12345678 }, cookies_info: { present: true, state: 'ok', days: 21, sites: ['instagram.com'] } };
    await context.addInitScript(() => {
      window.hostEvents = {};
      window.Telegram = { WebApp: { initData: 'offline-test-only', colorScheme: 'light', ready() {}, expand() {},
        isVersionAtLeast: () => true, setHeaderColor() {}, setBackgroundColor() {}, setBottomBarColor() {},
        onEvent: (name, fn) => { window.hostEvents[name] = fn; }, HapticFeedback: { notificationOccurred() {} } } };
    });
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.hostname !== 'sdp.test') return route.fulfill({ body: '', contentType: 'text/plain' });
      if (url.pathname === '/') return route.fulfill({ body: html, contentType: 'text/html; charset=utf-8' });
      if (url.pathname === '/assets/material.js') return route.fulfill({ body: materialBundle, contentType: 'application/javascript' });
      if (forbidden) return route.fulfill({ status: 403, json: { error: 'no_access' } });
      if (request.method() === 'POST') {
        const body = request.postDataJSON(); posts.push({ path: url.pathname, body });
        if (url.pathname === '/api/settings') {
          if (failSettings) return route.abort('failed');
          for (const [key, value] of Object.entries(body)) {
            if (key in settings.flags) settings.flags[key] = value;
            else if (key in settings.tunables) settings.tunables[key] = value;
            else settings[key] = value;
          }
        }
        return route.fulfill({ json: failDownload && url.pathname === '/api/download' ? { ok: false, error: 'no_access' } : { ok: true, queued: 1, cleared: 12, kb: 32 } });
      }
      const data = {
        '/api/lang': { ok: true, lang: settings.lang },
        '/api/settings': { ok: true, settings },
        '/api/access': { ok: true, access: [{ label: 'Family <safe>', ident: '-100123' }] },
        '/api/services': { ok: true, services: [{ id: 'youtube', label: 'YouTube', tier: 'extended', enabled: true }] },
        '/api/events': { ok: true, events: empty ? [] : [{ source: 'youtube', status: 'sent', size: 12345678, chat_title: 'Saved videos', via: 'yt-dlp', ts: Date.now()/1000 - 60, url: 'https://youtu.be/test' }] },
        '/api/progress': { ok: true, jobs: empty ? [] : [{ source: 'instagram', mode: 'video', status: 'downloading', pct: 62, eta: 12, speed: 1450000 }] },
        '/api/stats': { ok: true, stats: { total: 128, ok: 124, fail: 4, by_source: [{ source: 'youtube', c: 84 }, { source: 'instagram', c: 44 }], by_chat: [{ name: 'Saved videos', c: 128 }] } },
        '/api/preview': { ok: true, info: { title: 'A video <safe>', duration: 65, uploader: 'Creator' } },
      };
      return route.fulfill({ json: data[url.pathname] || { ok: false } });
    });
    const page = await context.newPage();
    page.on('pageerror', err => { errors.push(err.message); console.error('Browser:', err.message); });
    await page.goto('http://sdp.test/');
    await page.waitForFunction(() => document.documentElement.dataset.ready === 'true');
    const select = async (selector, value) => {
      const control = page.locator(selector);
      await control.click();
      const closed = control.evaluate(el => new Promise(resolve => el.addEventListener('closed', () => resolve(), { once: true })));
      await control.locator('md-select-option[value="' + value + '"]').click();
      await closed;
      await page.waitForFunction(({ selector, value }) => document.querySelector(selector).value === value, { selector, value });
    };
    const nav = name => page.locator(`.nav[data-page="${name}"]`);
    const language = async lang => {
      const saved = page.waitForResponse(r => new URL(r.url()).pathname === '/api/settings' && r.request().method() === 'POST');
      await select('#lang-sel', lang);
      await saved;
      await page.waitForFunction(value => document.documentElement.lang === value, lang);
    };
    const shot = async name => {
      if (screenshotDir) {
        await page.waitForTimeout(350);
        fs.mkdirSync(screenshotDir, { recursive: true });
        await page.evaluate(() => window.scrollTo(0, 0));
        await page.screenshot({ path: path.join(screenshotDir, name + '.png') });
      }
    };
    check(await page.locator('html').getAttribute('data-theme') === 'light', 'initial Telegram light theme');
    check(await page.locator('#page-home .modes > md-filter-chip').count() === 2, 'mode group contains two Material filter chips');
    check(await page.locator('#page-home #url, #page-home #go').count() === 2, 'download controls stay inside home page');
    check(await page.locator('.hero, .platforms, [data-i18n="hero_title"]').count() === 0, 'promotional block removed');
    check(await page.locator('#url').evaluate(el => !!el.shadowRoot.querySelector('textarea')), 'real Material text field rendered');
    await page.locator('.input-tips summary').click();
    check(await page.locator('#url-hint').isVisible(), 'download tips expand');
    await page.locator('.input-tips summary').click();
    check(!await page.locator('#url-hint').isVisible(), 'tips collapse');
    await shot('material3-home-light');
    await select('#theme-sel', 'dark');
    await page.reload();
    check(await page.locator('html').getAttribute('data-theme') === 'dark', 'theme persists');
    await shot('material3-home-dark');
    await select('#theme-sel', 'auto');
    await page.evaluate(() => { Telegram.WebApp.colorScheme = 'dark'; hostEvents.themeChanged(); });
    check(await page.locator('html').getAttribute('data-theme') === 'dark', 'auto follows host event');
    await select('#theme-sel', 'light');
    await page.evaluate(() => { Telegram.WebApp.colorScheme = 'dark'; hostEvents.themeChanged(); });
    check(await page.locator('html').getAttribute('data-theme') === 'light', 'explicit override beats host');
    await page.click('#go');
    check((await page.locator('#status').textContent()).includes('посилання'), 'empty-link error visible');
    await page.locator('[data-mode="audio"]').focus();
    await page.keyboard.press('Space');
    check(await page.locator('[data-mode="audio"] button').getAttribute('aria-pressed') === 'true', 'keyboard selects mode');
    await page.locator('#url textarea').fill('https://youtu.be/test');
    await page.waitForFunction(() => document.querySelector('#preview-inline').textContent.includes('A video <safe>'));
    await select('#q-abr', '192');
    await page.click('#go');
    await page.locator('#page-activity.active .dl').first().waitFor();
    const download = posts.find(p => p.path === '/api/download');
    check(download.body.mode === 'audio' && download.body.abr === '192' && download.body.initData === 'offline-test-only', 'download contract and auth preserved');
    check(await nav('activity').getAttribute('aria-current') === 'page', 'successful download opens activity');
    check(!await page.locator('#url').isVisible() && !await page.locator('#go').isVisible(), 'home form hidden on other pages');
    await shot('material3-activity');
    await nav('stats').click();
    await page.waitForFunction(() => document.querySelector('#s-total').textContent === '128');
    await shot('material3-stats');
    await nav('settings').click();
    await page.waitForFunction(() => document.querySelector('#ck-box').textContent.includes('21'));
    check(await page.locator('#acc-list').textContent().then(t => t.includes('Family <safe>')), 'external labels escaped');
    check(await page.locator('[data-setting="whitelist"]').evaluate(el => el.selected), 'settings selection accessible');
    const notify = page.locator('[data-setting="notify_no_access"]');
    await notify.click();
    await page.waitForFunction(() => !document.querySelector('[data-setting="notify_no_access"]').disabled);
    check(posts.some(p => p.body.notify_no_access === '1'), 'Material switch saves its new value');
    failSettings = true;
    await notify.click();
    await page.waitForFunction(() => document.querySelector('#settings-status').textContent.length > 0);
    check(await notify.evaluate(el => el.selected && !el.disabled), 'failed switch update restores state and unlocks control');
    failSettings = false;
    await page.locator('#beh-toggle').focus();
    await page.keyboard.press('Enter');
    check(await page.locator('#beh-toggle').getAttribute('aria-expanded') === 'true', 'keyboard opens disclosure');
    check(await page.locator('label[for="t-max_height"] .sub').count() === 1, 'nested supporting text survives translations');
    await page.locator('#t-max_height input').fill('720');
    await page.click('#beh-save');
    await page.waitForFunction(() => document.querySelector('#beh-status').textContent.includes('Збережено'));
    check(posts.some(p => p.path === '/api/settings' && p.body.max_height === 720), 'numeric settings saved');
    await page.click('#svc-toggle');
    await page.locator('[data-svc="youtube"]').first().waitFor();
    check(await page.locator('[data-svc="youtube"]').evaluate(el => el.selected), 'dynamic options accessible');
    await page.locator('[data-svc="youtube"]').click();
    check(posts.some(p => p.body['svc:youtube'] === '0'), 'service action preserved');
    await language('en');
    await page.waitForFunction(() => document.documentElement.lang === 'en');
    check(await page.locator('label[for="t-max_height"] .sub').count() === 1, 'supporting text survives language change');
    await shot('material3-settings');
    // Responsive, translated, dark/light screens: no horizontal overflow or tiny targets.
    for (const lang of ['uk', 'en']) {
      await nav('settings').click();
      await language(lang);
      await page.waitForFunction(value => document.documentElement.lang === value, lang);
      for (const width of [320, 390, 768, 1280]) {
      await page.setViewportSize({ width, height: 900 });
      for (const theme of ['light', 'dark']) {
        await select('#theme-sel', theme);
        for (const screen of ['home', 'activity', 'stats', 'settings']) {
          await nav(screen).click();
          if (screen === 'home') {
            check(await page.evaluate(() => {
              const header = document.querySelector('.header').getBoundingClientRect();
              const cards = [...document.querySelectorAll('#page-home .card')].map(el => el.getBoundingClientRect().top);
              return Math.min(...cards) - header.bottom <= 36;
            }), `no empty hero row: ${width}/${theme}/${lang}`);
          }
          check(await page.locator('.page:visible').count() === 1, `only active page visible: ${screen}`);
          const overflowing = await page.evaluate(() => {
            const scan = root => [...root.querySelectorAll('*')].flatMap(el => [el, ...(el.shadowRoot ? scan(el.shadowRoot) : [])]);
            return scan(document).filter(el => el.getBoundingClientRect().right > innerWidth + 1).map(el => [el.id || el.localName, el.className, el.getBoundingClientRect().right]).slice(0, 12);
          });
          check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `no overflow: ${width}/${theme}/${screen}: ${overflowing}`);
          const small = await page.locator('button.nav:visible, button.disclosure:visible, md-filled-button:visible, md-filled-tonal-button:visible, md-filter-chip:visible, md-text-button:visible, md-outlined-text-field:visible, md-outlined-select:visible, md-switch:visible').evaluateAll(els => els.filter(el => {
            if (!el.isConnected || !el.getClientRects().length) return false;
            const r = el.getBoundingClientRect(); return r.width < 47 || r.height < 47;
          }).map(el => ({ id: el.id || el.className, w: el.getBoundingClientRect().width, h: el.getBoundingClientRect().height, connected: el.isConnected })));
          check(!small.length, `48px targets ${width}/${screen}: ${JSON.stringify(small)}`);
        }
      }
    }
    }
    for (const theme of ['light', 'dark']) {
      await select('#theme-sel', theme);
      const ratios = await page.evaluate(() => {
        const style = getComputedStyle(document.documentElement);
        const color = name => {
          const rgb = style.getPropertyValue(name).trim().replace('#', '').match(/../g).map(n => parseInt(n, 16) / 255);
          const linear = rgb.map(c => c <= .04045 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4);
          return .2126 * linear[0] + .7152 * linear[1] + .0722 * linear[2];
        };
        const pairs = [
          ['on-primary', 'primary'], ['on-primary-container', 'primary-container'],
          ['on-secondary-container', 'secondary-container'], ['on-surface', 'surface'],
          ['on-surface-variant', 'surface-container-low'], ['error', 'surface-container-low'],
          ['on-error-container', 'error-container'],
        ];
        return pairs.map(([fg, bg]) => {
          const a = color('--md-sys-color-' + fg), b = color('--md-sys-color-' + bg);
          return { pair: fg + '/' + bg, ratio: (Math.max(a, b) + .05) / (Math.min(a, b) + .05) };
        });
      });
      for (const r of ratios) check(r.ratio >= 4.5, `text contrast ${theme}/${r.pair}: ${r.ratio}`);
    }
    await page.setViewportSize({ width: 1280, height: 900 });
    await nav('settings').click();
    await language('uk');
    await page.waitForFunction(() => document.documentElement.lang === 'uk');
    await page.reload();
    await nav('home').click();
    await select('#theme-sel', 'light');
    await shot('material3-desktop');
    check(await page.locator('.bottomnav').evaluate(el => el.getBoundingClientRect().width) === 96, 'wide navigation rail');
    failDownload = true;
    await page.locator('#url textarea').fill('https://youtu.be/test');
    await page.click('#go');
    await page.waitForFunction(() => document.querySelector('#status').className === 'err');
    check(await page.locator('#go').isEnabled(), 'download failure re-enables button');
    empty = true;
    await nav('activity').click();
    await page.waitForFunction(() => document.querySelector('#activity').textContent.includes('Поки'));
    check((await page.locator('#activity').textContent()).includes('Поки'), 'empty history');
    forbidden = true;
    await nav('settings').click();
    await page.waitForFunction(() => document.querySelector('#settings-body').textContent.includes('адмін'));
    check((await page.locator('#settings-body').textContent()).includes('адмін'), 'access denied retained');
    check(!errors.length, `no browser errors: ${errors.join(', ')}`);
    // Outside Telegram: use OS theme, still work with storage disabled, never send a download.
    await page.close();
    const standalone = await browser.newContext({ colorScheme: 'dark' });
    await standalone.addInitScript(() => Object.defineProperty(window, 'localStorage', { get() { throw new Error('Blocked storage'); } }));
    await standalone.route('**/*', route => {
      const pathname = new URL(route.request().url()).pathname;
      return route.fulfill({ contentType: pathname === '/' ? 'text/html' : pathname === '/assets/material.js' ? 'application/javascript' : 'application/json',
        body: pathname === '/' ? html : pathname === '/assets/material.js' ? materialBundle : '{"ok":false}' });
    });
    const solo = await standalone.newPage();
    await solo.goto('http://sdp.test/');
    check(await solo.locator('html').getAttribute('data-theme') === 'dark', 'OS theme without Telegram/storage');
    await solo.locator('#url textarea').fill('https://youtu.be/test');
    await solo.click('#go');
    check((await solo.locator('#status').textContent()).includes('бота'), 'standalone cannot bypass Telegram auth');
    await solo.emulateMedia({ colorScheme: 'light' });
    await solo.waitForFunction(() => document.documentElement.dataset.theme === 'light');
    check(true, 'OS theme changes applied');
    const missing = await browser.newContext();
    await missing.route('**/*', route => route.fulfill({
      status: route.request().url() === 'http://sdp.test/' ? 200 : 404,
      contentType: 'text/html', body: route.request().url() === 'http://sdp.test/' ? html : '',
    }));
    const broken = await missing.newPage();
    await broken.goto('http://sdp.test/');
    check((await broken.locator('main [role="alert"]').textContent()).includes('Interface failed'), 'missing bundle has an actionable message, not an inert form');
    // Open the actual file like a user double-clicking it, not a mocked HTTP page.
    const local = await browser.newContext();
    await local.route(/^https?:/, route => route.abort());
    const filePage = await local.newPage();
    const localErrors = [], localApiCalls = [];
    filePage.on('pageerror', e => localErrors.push(e.message));
    filePage.on('request', request => { if (request.url().includes('/api/')) localApiCalls.push(request.url()); });
    await filePage.goto(pathToFileURL(path.join(__dirname, '..', 'index.html')).href);
    await filePage.waitForFunction(() => document.documentElement.dataset.ready === 'true');
    check(await filePage.locator('#url textarea').isVisible(), 'double-click renders real Material controls');
    check(await filePage.locator('#local-preview-note').isVisible(), 'local preview explicitly labels disconnected data');
    await filePage.click('#theme-sel');
    await filePage.locator('#theme-sel md-select-option[value="dark"]').click();
    await filePage.waitForFunction(() => document.documentElement.dataset.theme === 'dark');
    check(true, 'theme works on file URL');
    for (const screen of ['activity', 'stats', 'settings']) {
      await filePage.locator(`.nav[data-page="${screen}"]`).click();
      check(await filePage.locator(`#page-${screen}`).isVisible(), `local navigation: ${screen}`);
    }
    check(await filePage.locator('[data-setting="whitelist"]').evaluate(el => el.disabled), 'local settings cannot be saved accidentally');
    await filePage.click('#lang-sel');
    await filePage.locator('#lang-sel md-select-option[value="en"]').click();
    await filePage.waitForFunction(() => document.documentElement.lang === 'en');
    check((await filePage.locator('#local-preview-note').textContent()).includes('Local design preview'), 'local translation works');
    await filePage.locator('.nav[data-page="home"]').click();
    await filePage.locator('#url textarea').fill('https://youtu.be/test');
    await filePage.click('#go');
    check((await filePage.locator('#status').textContent()).includes('bot'), 'local download still requires Telegram');
    await filePage.waitForTimeout(700);
    check(!localApiCalls.length && !localErrors.length, 'local preview makes no API calls and has no script errors');
    console.log(`PASS: ${checks} UI checks; offline API fixtures, no production calls.`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
