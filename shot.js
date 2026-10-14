const { chromium } = require('/usr/local/lib/node_modules/playwright');
const BASE = 'http://127.0.0.1:61007';
const OUT  = 'screenshots';
const SHOTS = [
  ['01-real-full',         '/index.html',                          1280, 'full'],
  ['02-real-modal',        '/index.html',                          1280, 'modal'],
  ['03-real-mobile',       '/index.html',                           375, 'full'],
  ['04-sample-normal',     '/index.html?src=sample&date=2026-09-20',1280,'full'],
  ['05-sample-normal-modal','/index.html?src=sample&date=2026-09-20',1280,'modal'],
  ['06-sample-degraded',   '/index.html?src=sample&date=2026-09-21',1280,'full'],
  ['07-sample-failed',     '/index.html?src=sample&date=2026-09-22',1280,'full'],
  ['08-sample-empty',      '/index.html?src=sample&date=2026-09-23',1280,'full'],
  ['09-sample-missing',    '/index.html?src=sample&date=2026-09-19',1280,'full'],
];
(async () => {
  const browser = await chromium.launch({ executablePath: '/usr/bin/chromium',
    args: ['--no-sandbox','--font-render-hinting=none'] });
  const report = {};
  for (const [name, path, width, kind] of SHOTS) {
    const ctx = await browser.newContext({ viewport: { width, height: 900 },
      deviceScaleFactor: 2, locale: 'ja-JP' });
    const page = await ctx.newPage();
    const logs = [];
    page.on('console', m => { if (m.type()==='error'||m.type()==='warning') logs.push('CONSOLE '+m.type()+': '+m.text()); });
    page.on('pageerror', e => logs.push('PAGEERROR: '+e.message));
    page.on('requestfailed', r => logs.push('REQFAIL '+r.url()));
    page.on('response', r => { if (r.status()>=400) logs.push('HTTP'+r.status()+' '+r.url()); });
    await page.goto(BASE+path, { waitUntil:'networkidle' });
    await page.waitForTimeout(500);
    if (kind === 'modal') {
      const c = await page.$('#listWorld .tcard');
      if (c) { await c.click(); await page.waitForTimeout(600); }
    }
    const v = await page.evaluate(() => {
      const T = s => (document.querySelector(s)||{}).innerText?.trim() || '';
      const A = (s,f) => Array.from(document.querySelectorAll(s)).map(f);
      const seq = s => A(s, c => { const e=c.querySelector('.score-row .tot'); return e?parseInt(e.innerText,10):null; });
      const bodyText = document.body.innerText;
      return {
        hdrDate: T('#hdrDate'), hdrUpdated: T('#hdrUpdated'),
        badges: A('.badges .badge', e=>e.innerText.trim()),
        degraded: T('#degradedBanner'), loadError: T('#loadError'), dayWarning: T('#dayWarning'),
        summary: T('#summaryBar').replace(/\s+/g,' '),
        worldCount: document.querySelectorAll('#listWorld .tcard').length,
        japanCount: document.querySelectorAll('#listJapan .tcard').length,
        jpNoneCount: document.querySelectorAll('#jpNoneGrid .jn-card').length,
        worldTop: A('#listWorld .tcard', c=>({
          rank:(c.querySelector('.rank')||{}).innerText?.trim(),
          t:(c.querySelector('h3')||{}).innerText?.trim(),
          sc:(c.querySelector('.score-row')||{}).innerText?.trim().replace(/\s+/g,' ')})).slice(0,5),
        japanTop: A('#listJapan .tcard', c=>({
          rank:(c.querySelector('.rank')||{}).innerText?.trim(),
          t:(c.querySelector('h3')||{}).innerText?.trim(),
          rows:A2(c)})).slice(0,5),
        jpNoneTop: A('#jpNoneGrid .jn-card', c=>({
          id:(c.querySelector('.idchip')||{}).innerText?.trim(),
          t:(c.querySelector('h3')||{}).innerText?.trim(),
          meta:(c.querySelector('.jn-meta')||{}).innerText?.trim().replace(/\s+/g,' ')})).slice(0,6),
        worldSeq: seq('#listWorld .tcard'), japanSeq: seq('#listJapan .tcard'),
        japanJpMedia: A('#listJapan .tcard', c=>{const r=c.querySelectorAll('.score-row .tot');
          return r.length>1?parseInt(r[1].innerText,10):null;}),
        jpNoneSeq: A('#jpNoneGrid .jn-card', c=>{const m=c.innerText.match(/スコア\s*([0-9]+)/);return m?+m[1]:null;}),
        emptyMsgs: A('.empty-list', e=>e.innerText.trim().replace(/\s+/g,' ')),
        modalOpen: !!document.querySelector('#overlay.open'),
        mdTitle: T('#mdTitle'), mdChips: T('#mdChips').replace(/\s+/g,' '),
        mdSections: A('#modalBody .msec-h', e=>e.innerText.trim()),
        mdLinks: A('#modalBody a[href^="http"]', a=>a.getAttribute('href')).slice(0,4),
        tofu: (bodyText.match(/[\uFFFD\u25A1]/g)||[]).length,
        gdelt: /gdelt/i.test(bodyText),
      };
      function A2(c){return Array.from(c.querySelectorAll('.score-row')).map(r=>r.innerText.trim().replace(/\s+/g,' '));}
    });
    await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: true });
    report[name] = { url: path, width, seen: v, logs };
    console.log(`\n===== ${name}  (${width}px) ${path}`);
    console.log(' hdrDate:', v.hdrDate, '| updated:', v.hdrUpdated, '| badges:', JSON.stringify(v.badges));
    if (v.loadError)  console.log(' LOADERROR:', v.loadError);
    if (v.degraded)   console.log(' DEGRADED:', v.degraded.replace(/\s+/g,' '));
    if (v.dayWarning) console.log(' DAYWARN:', v.dayWarning.replace(/\s+/g,' '));
    console.log(' summary:', v.summary);
    console.log(` counts world=${v.worldCount} japan=${v.japanCount} jpNone=${v.jpNoneCount}`);
    v.worldTop.forEach(c=>console.log(`   W#${c.rank} [${c.sc}] ${c.t}`));
    v.japanTop.forEach(c=>console.log(`   J#${c.rank} ${JSON.stringify(c.rows)} ${c.t}`));
    v.jpNoneTop.forEach(c=>console.log(`   N ${c.id} [${c.meta}] ${c.t}`));
    if (v.emptyMsgs.length) console.log(' EMPTY:', JSON.stringify(v.emptyMsgs));
    console.log(' SEQ world:', JSON.stringify(v.worldSeq));
    console.log(' SEQ japan(score):', JSON.stringify(v.japanSeq));
    console.log(' SEQ japan(jp_media):', JSON.stringify(v.japanJpMedia));
    console.log(' SEQ jpNone:', JSON.stringify(v.jpNoneSeq));
    const d=a=>a.filter(x=>x!=null).every((x,i,arr)=>i===0||arr[i-1]>=x);
    console.log(` DESC world=${d(v.worldSeq)} japanByJpMedia=${d(v.japanJpMedia)} jpNone=${d(v.jpNoneSeq)}`);
    if (v.modalOpen) {
      console.log(' MODAL:', v.mdTitle);
      console.log('   chips:', v.mdChips);
      console.log('   sections:', JSON.stringify(v.mdSections));
      console.log('   links:', JSON.stringify(v.mdLinks));
    }
    console.log(` TOFU=${v.tofu}  GDELT_on_page=${v.gdelt}`);
    logs.forEach(l=>console.log('  !', l));
    await ctx.close();
  }
  await browser.close();
  require('fs').writeFileSync('/tmp/report.json', JSON.stringify(report,null,1));
})();
