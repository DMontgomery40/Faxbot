const { chromium } = require('playwright');
(async () => {
  const url = 'https://developer.apple.com/documentation/xcode/enabling-enhanced-security-for-your-app';
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await page.goto(url, { waitUntil: 'networkidle' });
  // The content is rendered by JS; grab main text content
  const content = await page.evaluate(() => {
    const sel = document.querySelector('main') || document.body;
    function getText(el){
      let t = '';
      el.querySelectorAll('h1,h2,h3,h4,p,li,code,pre').forEach(n => { t += '\n' + n.innerText; });
      return t;
    }
    return getText(sel);
  });
  console.log(content.slice(0, 8000));
  await browser.close();
})();
