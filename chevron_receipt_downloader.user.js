// ==UserScript==
// @name         Chevron/Texaco Rewards Receipt Downloader (v3)
// @namespace    https://www.chevrontexacorewards.com/
// @version      3.0
// @description  Download Chevron/Texaco Rewards PURCHASE receipts into ONE PDF, one receipt per page, for a custom date range (file named by the actual first/last receipt dates)
// @match        https://www.chevrontexacorewards.com/*loyalty-wallet-page*
// @grant        none
// ==/UserScript==

/*
  USAGE
  -----
  1. Log in to https://www.chevrontexacorewards.com/ and open the Wallet
     (transaction history) page yourself.
  2a. Console: paste this file into DevTools console, then run:
         chevronDownloadReceipts('2025-05-09');              // start -> today
         chevronDownloadReceipts('2025-05-09', '2026-07-16'); // explicit range
  2b. Userscript (Tampermonkey): install; use the floating 'Download
      receipts' button, which prompts for start and end dates.

  BEHAVIOR (v2)
  - Keeps only PURCHASE receipts (rows with a dollar amount); excludes
    'Discount activated' duplicate receipts.
  - Date range is customizable by the user.
  - Produces ONE combined PDF; each receipt on its own single page,
    chronological, labeled with its date.
  - Runs entirely in your browser; sends no data anywhere.
*/

(function () {
  'use strict';

  function todayISO() {
    const d = new Date();
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') +
           '-' + String(d.getDate()).padStart(2, '0');
  }

  function getJsPDF() {
    if (window.jspdf && window.jspdf.jsPDF) return window.jspdf.jsPDF;
    if (window.jsPDF) return window.jsPDF;
    return null;
  }
  async function ensureJsPDF() {
    let JS = getJsPDF();
    if (JS) return JS;
    await new Promise((res, rej) => {
      const s = document.createElement('script');
      s.src = 'https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js';
      s.onload = res; s.onerror = rej;
      document.head.appendChild(s);
    });
    return getJsPDF();
  }

  function parseReceiptDate(text) {
    let m = text.match(/(\d{1,2})\/(\d{1,2})\/(\d{4})/);
    if (m) return normalize(+m[3], +m[1], +m[2]);
    m = text.match(/(\d{1,2})\/(\d{1,2})\/(\d{2})\b/);
    if (m) return normalize(2000 + (+m[3]), +m[1], +m[2]);
    return null;
  }
  function normalize(y, mo, d) {
    return y + '-' + String(mo).padStart(2, '0') + '-' + String(d).padStart(2, '0');
  }

  // Walk up from a receipt trigger to find its row's transaction type.
  function rowType(t) {
    let el = t;
    for (let i = 0; i < 10 && el; i++) {
      const txt = el.innerText || '';
      if (/Purchase/.test(txt)) return 'Purchase';
      if (/Discount activated/.test(txt)) return 'Discount activated';
      if (/Discount credited/.test(txt)) return 'Discount credited';
      el = el.parentElement;
    }
    return '?';
  }

  async function loadHistoryUntil(startISO, statusCb) {
    const maxRounds = 120;
    let stable = 0, lastCount = 0;
    for (let i = 0; i < maxRounds; i++) {
      const triggers = document.querySelectorAll('a.receipt[data-bs-target]');
      let oldest = null;
      triggers.forEach(t => {
        const modal = document.querySelector(t.getAttribute('data-bs-target'));
        if (!modal) return;
        const raw = modal.querySelector('input.rawReceipt');
        const txt = raw ? raw.value : ((modal.querySelector('.modal-body') || {}).innerText || '');
        const iso = parseReceiptDate(txt);
        if (iso && (!oldest || iso < oldest)) oldest = iso;
      });
      if (statusCb) statusCb('Loaded ' + triggers.length + ' receipts, oldest ' + (oldest || '?'));
      if (oldest && oldest <= startISO) break;
      if (triggers.length === lastCount) { stable++; if (stable >= 6) break; }
      else { stable = 0; lastCount = triggers.length; }
      window.scrollTo(0, document.body.scrollHeight);
      await new Promise(r => setTimeout(r, 700));
      window.scrollBy(0, -400);
      await new Promise(r => setTimeout(r, 700));
    }
  }

  function collectPurchaseReceipts(startISO, endISO) {
    const triggers = [...document.querySelectorAll('a.receipt[data-bs-target]')];
    const list = [];
    for (const t of triggers) {
      const modal = document.querySelector(t.getAttribute('data-bs-target'));
      if (!modal) continue;
      const raw = modal.querySelector('input.rawReceipt');
      const text = raw ? raw.value : ((modal.querySelector('.modal-body') || {}).innerText || '');
      const iso = parseReceiptDate(text);
      if (!iso || iso < startISO || iso > endISO) continue;
      if (rowType(t) !== 'Purchase') continue;   // Purchase-only (skip Discount duplicates)
      list.push({ iso, text });
    }
    list.sort((a, b) => a.iso.localeCompare(b.iso));  // chronological
    return list;
  }

  // Build ONE PDF, each receipt on its own single page.
  // File is named by the ACTUAL first/last receipt dates present (not the requested bounds).
  function buildCombinedPDF(JS, items) {
    const doc = new JS({ unit: 'pt', format: 'letter' });
    for (let i = 0; i < items.length; i++) {
      if (i > 0) doc.addPage();
      doc.setFont('courier', 'normal');
      doc.setFontSize(11);
      doc.text('Receipt \u2014 ' + items[i].iso, 40, 28);
      doc.setFontSize(9);
      const lines = items[i].text.split('\n');
      let y = 48; const lh = 11; const maxY = 760;
      for (let ln of lines) {
        if (y > maxY) break;              // keep each receipt to one page
        doc.text(ln.replace(/\t/g, '    '), 40, y);
        y += lh;
      }
    }
    const actualStart = items[0].iso;
    const actualEnd = items[items.length - 1].iso;
    doc.save('chevron-receipts_' + actualStart + '_to_' + actualEnd + '.pdf');
  }

  async function chevronDownloadReceipts(startISO, endISO) {
    if (!startISO) { alert('Provide a start date, e.g. chevronDownloadReceipts("2025-05-09")'); return; }
    endISO = endISO || todayISO();
    const JS = await ensureJsPDF();
    if (!JS) { alert('jsPDF not available.'); return; }
    console.log('[Chevron] Loading history until ' + startISO + ' ...');
    await loadHistoryUntil(startISO, m => console.log('[Chevron] ' + m));
    const items = collectPurchaseReceipts(startISO, endISO);
    console.log('[Chevron] ' + items.length + ' PURCHASE receipts in ' + startISO + ' .. ' + endISO);
    if (!items.length) { alert('No purchase receipts found in that range.'); return; }
    buildCombinedPDF(JS, items);
    console.log('[Chevron] Done. One PDF with ' + items.length + ' pages.');
    return items.map(i => i.iso);
  }

  window.chevronDownloadReceipts = chevronDownloadReceipts;

  try {
    const addBtn = () => {
      if (document.getElementById('chevronDlBtn')) return;
      const btn = document.createElement('button');
      btn.id = 'chevronDlBtn';
      btn.textContent = 'Download receipts';
      btn.style.cssText = 'position:fixed;z-index:99999;right:16px;bottom:16px;padding:10px 14px;background:#0b57d0;color:#fff;border:none;border-radius:8px;font-weight:bold;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.3)';
      btn.onclick = () => {
        const start = prompt('Start date (YYYY-MM-DD):', '2025-05-09');
        if (!start) return;
        const end = prompt('End date (YYYY-MM-DD), blank = today:', '') || todayISO();
        chevronDownloadReceipts(start, end);
      };
      document.body.appendChild(btn);
    };
    if (document.readyState === 'complete') addBtn();
    else window.addEventListener('load', addBtn);
  } catch (e) { /* console mode */ }
})();