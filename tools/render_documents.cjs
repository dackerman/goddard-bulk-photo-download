// Render archived HTML with its local images into stable, printable PDFs.
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { chromium } = require('playwright');

(async () => {
  const jobs = JSON.parse(fs.readFileSync(0, 'utf8'));
  const browser = await chromium.launch({
    executablePath: process.env.GODDARD_CHROMIUM || undefined,
    headless: true,
  });
  try {
    const page = await browser.newPage({ viewport: { width: 1000, height: 1400 } });
    // PDFs depend only on the archived local resources, not a live login/site.
    await page.route(/^https?:/, route => route.abort());
    for (let i = 0; i < jobs.length; i++) {
      const job = jobs[i];
      await page.goto(pathToFileURL(job.source).href, { waitUntil: 'load' });
      await page.evaluate(async () => {
        await document.fonts.ready;
        await Promise.all(Array.from(document.images, img => img.decode().catch(() => {})));
        if (Array.from(document.images).some(img => !img.complete || !img.naturalWidth)) {
          throw new Error('A local image failed to load');
        }
      });
      await page.addStyleTag({ content: `
        @page { size: Letter; margin: 12mm; }
        @media print {
          body { margin: 0 !important; padding: 0 !important; background: white !important; }
          #bodyTable, .email-container { height: auto !important; }
          .centeringLeftCol, .centeringRightCol { display: none !important; }
          .centeringMidCol { width: 100% !important; }
          #sidr { display: none !important; }
          tr.lesson-row, .block, img { break-inside: avoid; }
        }
      ` });
      fs.mkdirSync(path.dirname(job.destination), { recursive: true });
      const tmp = job.destination + '.part';
      await page.pdf({ path: tmp, format: 'Letter', printBackground: true,
                       preferCSSPageSize: true, displayHeaderFooter: false });
      fs.chmodSync(tmp, 0o600);
      fs.renameSync(tmp, job.destination);
      fs.writeFileSync(job.stamp + '.part', job.fingerprint, { mode: 0o600 });
      fs.renameSync(job.stamp + '.part', job.stamp);
      if ((i + 1) % 25 === 0 || i + 1 === jobs.length) console.log(`PDFs: ${i + 1}/${jobs.length}`);
    }
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(`PDF conversion failed: ${error.message}`); process.exitCode = 1; });
