"use strict";

// ---------- helpers ----------

const b64u = {
  dec(s) {
    s = s.replace(/-/g, "+").replace(/_/g, "/");
    while (s.length % 4) s += "=";
    return Uint8Array.from(atob(s), (c) => c.charCodeAt(0)).buffer;
  },
  enc(buf) {
    const bytes = new Uint8Array(buf);
    let s = "";
    for (const b of bytes) s += String.fromCharCode(b);
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  },
};

// Search remembers itself while you move between tabs: the Search tab goes back to the last search
// (filters and sort) at the place you left in the results. Clear, a reload, or reopening the app
// starts over (start.js forgets it).
const SEARCH_KEY = "finance-search";
function savedSearch() {
  try { return JSON.parse(sessionStorage.getItem(SEARCH_KEY) || "null"); } catch { return null; }
}
function saveSearch(state) {
  try { sessionStorage.setItem(SEARCH_KEY, JSON.stringify(state)); } catch { /* storage blocked */ }
}
document.addEventListener("DOMContentLoaded", () => {
  const tab = document.querySelector('.tabbar a[href^="/search"]');
  const saved = savedSearch();
  if (tab && saved && saved.qs) tab.setAttribute("href", "/search" + saved.qs);
  if (document.body.dataset.page !== "search") return;
  const qs = location.search;
  const y = saved && saved.qs === qs ? saved.y || 0 : 0;
  const state = { qs, y };
  saveSearch(state);
  let pending = false;
  const remember = () => { state.y = Math.round(window.scrollY); saveSearch(state); };
  window.addEventListener("scroll", () => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => { pending = false; remember(); });
  }, { passive: true });
  window.addEventListener("pagehide", remember);
  if (!y) return;
  // Back where you were: load more of the list first if the spot was further down than the page.
  (async () => {
    for (let i = 0; i < 20 && document.documentElement.scrollHeight < y + innerHeight; i++) {
      const more = document.querySelector(".load-more:not([disabled])");
      if (!more) break;
      more.click();
      for (let t = 0; t < 100 && more.isConnected; t++) await new Promise((r) => setTimeout(r, 50));
      if (more.isConnected) break;
    }
    window.scrollTo(0, y);
  })();
});

// Reload the current page after a save. Marked first, so start.js doesn't send it to the Inbox
// the way it does for a reload you start yourself.
function reloadHere() {
  try { sessionStorage.setItem("finance-keep", "1"); } catch { /* storage blocked */ }
  location.reload();
}

async function api(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Finance": "1" },
    body: JSON.stringify(body || {}),
    credentials: "same-origin",
  });
  const text = await r.text();
  let data;
  try { data = JSON.parse(text); } catch { data = { detail: text }; }
  if (!r.ok) throw new Error(data.detail || data.error || r.statusText);
  return data;
}

function say(text, isError) {
  const el = document.getElementById("msg");
  if (!el) return;
  el.textContent = text || "";
  el.classList.toggle("error", !!isError);
}

function credentialToJSON(cred) {
  const r = cred.response;
  const out = {
    id: cred.id,
    rawId: b64u.enc(cred.rawId),
    type: cred.type,
    response: { clientDataJSON: b64u.enc(r.clientDataJSON) },
    clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
  };
  if (cred.authenticatorAttachment) out.authenticatorAttachment = cred.authenticatorAttachment;
  if (r.attestationObject) {
    out.response.attestationObject = b64u.enc(r.attestationObject);
    if (r.getTransports) out.response.transports = r.getTransports();
  }
  if (r.authenticatorData) {
    out.response.authenticatorData = b64u.enc(r.authenticatorData);
    out.response.signature = b64u.enc(r.signature);
    out.response.userHandle = r.userHandle ? b64u.enc(r.userHandle) : null;
  }
  return out;
}

async function createPasskey(code, label) {
  const o = await api("/api/register/options", { code });
  o.challenge = b64u.dec(o.challenge);
  o.user.id = b64u.dec(o.user.id);
  (o.excludeCredentials || []).forEach((c) => { c.id = b64u.dec(c.id); });
  const cred = await navigator.credentials.create({ publicKey: o });
  await api("/api/register/verify", { credential: credentialToJSON(cred), label });
}

async function usePasskey() {
  const o = await api("/api/auth/options");
  o.challenge = b64u.dec(o.challenge);
  (o.allowCredentials || []).forEach((c) => { c.id = b64u.dec(c.id); });
  const cred = await navigator.credentials.get({ publicKey: o });
  await api("/api/auth/verify", { credential: credentialToJSON(cred) });
}

// ---------- Plaid Link ----------

const LINK_KEY = "finance_plaid_link";

function plaidHandler(token, extra) {
  return Plaid.create({
    token,
    ...extra,
    onSuccess: async (publicToken, metadata) => {
      const state = JSON.parse(localStorage.getItem(LINK_KEY) || "{}");
      localStorage.removeItem(LINK_KEY);
      try {
        if (state.itemId) {
          const r = await api("/api/plaid/updated",
                              { item_id: state.itemId, selected: (metadata.accounts || []).length,
                                investments: !!state.investments });
          if (state.investments) {
            say("Investments added. Holdings and activity load with the next sync, in a minute or two.");
            setTimeout(() => { location.href = "/accounts"; }, 2500);
            return;
          }
          if (state.chooseAccounts && !r.added) {
            say("Plaid did not share any new accounts. For banks that sign in on their own site, like Chase, " +
                "the bank decides what is shared: allow the account in the bank's linked apps settings, then try again.", true);
            setTimeout(() => { location.href = "/accounts"; }, 9000);
            return;
          }
          say(r.added ? `Added ${r.added} account${r.added > 1 ? "s" : ""}. Transactions follow in a few minutes.`
                      : "Updated.");
          setTimeout(() => { location.href = "/accounts"; }, 1800);
          return;
        } else {
          await api("/api/plaid/exchange", { public_token: publicToken, institution: metadata.institution,
                                             investments: !!state.investments });
        }
        say(state.itemId ? "Updated. New accounts are listed now; their transactions follow in a few minutes."
                         : "Connected. Accounts are listed now; the first transactions can take a few minutes.");
        setTimeout(() => { location.href = "/accounts"; }, 1500);
      } catch (e) { say(e.message, true); }
    },
    onExit: (err) => {
      if (err) say(err.display_message || err.error_message || "Bank connection was not completed.", true);
      if (document.body.dataset.page === "oauth") setTimeout(() => { location.href = "/"; }, 2500);
    },
  });
}

// investments: on an existing connection, ask for Investments consent; on a new one, connect a
// retirement plan site for Investments only (no bank transactions).
async function openLink(itemId, chooseAccounts, investments) {
  say("");
  try {
    const { link_token } = await api("/api/plaid/link-token",
                                     { item_id: itemId || undefined, accounts: !!chooseAccounts, investments: !!investments });
    // Saved so the OAuth return page can resume the same Link session.
    localStorage.setItem(LINK_KEY, JSON.stringify({ token: link_token, itemId: itemId || null,
                                                    chooseAccounts: !!chooseAccounts, investments: !!investments }));
    plaidHandler(link_token).open();
  } catch (e) { say(e.message, true); }
}

// ---------- shared UI ----------

function toast(text, isError) {
  const el = document.getElementById("toast");
  if (!el) { say(text, isError); return; }
  el.textContent = text;
  el.style.color = isError ? "var(--red)" : "";
  el.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove("show"), 2600);
}

async function syncNow(ev) {
  const btn = ev.currentTarget;
  btn.disabled = true;
  btn.classList.add("spin");
  try {
    await api("/api/sync");
    btn.classList.remove("spin");
    btn.classList.add("done-pop");
    toast("Synced");
    setTimeout(() => reloadHere(), 650);
  } catch (e) {
    btn.classList.remove("spin");
    btn.disabled = false;
    toast(e.message, true);
  }
}

function fold(li, delay = 0) {
  return new Promise((resolve) => {
    setTimeout(() => {
      li.style.maxHeight = li.scrollHeight + "px";
      requestAnimationFrame(() => {
        li.classList.add("leaving");
        setTimeout(() => { li.remove(); resolve(); }, 360);
      });
    }, delay);
  });
}

// ---------- sheets ----------

function openSheet(el) {
  el.hidden = false;
  el.scrollTop = 0;  // every pop-up opens at its top, not where it was last left
  document.documentElement.classList.add("sheet-open");
  requestAnimationFrame(() => {
    el.classList.add("open");
    document.getElementById("scrim").classList.add("open");
  });
}

function closeSheet(el) {
  el.classList.remove("open");
  if (!document.querySelector(".sheet.open")) {
    document.getElementById("scrim").classList.remove("open");
    document.documentElement.classList.remove("sheet-open");
  }
  setTimeout(() => { if (!el.classList.contains("open")) el.hidden = true; }, 450);
}

function closeAllSheets() {
  document.querySelectorAll(".sheet.open").forEach(closeSheet);
}

// Drag a sheet down to dismiss it: from the handle anytime, or from the content when it is
// scrolled to the top. While dragging, the page and the sheet do not scroll.
function swipeToClose(el) {
  let startY = null, dy = 0, dragging = false, fromHandle = false;
  const end = () => {
    if (startY === null) return;
    el.style.transition = "";
    el.style.transform = "";
    if (dragging && dy > 110) closeSheet(el);
    startY = null; dragging = false;
  };
  el.addEventListener("touchstart", (e) => {
    startY = e.touches[0].clientY; dy = 0; dragging = false;
    fromHandle = !!e.target.closest(".grabber, .sheet-head, .sheet-title");
  }, { passive: true });
  el.addEventListener("touchmove", (e) => {
    if (startY === null) return;
    dy = e.touches[0].clientY - startY;
    if (!dragging) {
      if (dy > 6 && (fromHandle || el.scrollTop <= 0)) dragging = true;
      else if (dy < -6 && !fromHandle) { startY = null; return; }
      else if (!fromHandle) return;
    }
    if (e.cancelable) e.preventDefault();
    el.style.transition = "none";
    el.style.transform = `translate(-50%, ${Math.max(0, dy)}px)`;
  }, { passive: false });
  el.addEventListener("touchend", end);
  el.addEventListener("touchcancel", end);
}

// ---------- keep pop-up fields above the on-screen keyboard ----------

if (window.visualViewport) {
  const vv = window.visualViewport;
  const fit = () => {
    const kb = Math.max(0, Math.round(window.innerHeight - vv.height - vv.offsetTop));
    document.documentElement.style.setProperty("--kb", kb + "px");
    document.documentElement.style.setProperty("--vvh", Math.round(vv.height) + "px");
  };
  vv.addEventListener("resize", fit);
  vv.addEventListener("scroll", fit);
  fit();
}
document.addEventListener("focusin", (ev) => {
  const el = ev.target;
  if (!el.matches || !el.matches("input, textarea") || !el.closest(".sheet")) return;
  // Wait for the keyboard to finish opening, then bring the field into view inside the pop-up.
  setTimeout(() => el.scrollIntoView({ block: "center", behavior: "smooth" }), 350);
});

// ---------- refresh without reloading (keeps the scroll position) ----------

// The home list's filters live only in the page, so a reload starts from All accounts.
const view = { account: "", pending: false };
function viewQuery() {
  const q = new URLSearchParams();
  if (view.account) q.set("account", view.account);
  if (view.pending) q.set("pending", "1");
  return q.toString();
}

// Swap in fresh copies of sections of the home page.
async function swapSections(ids) {
  const q = viewQuery();
  const r = await fetch(q ? `/?${q}` : "/", { credentials: "same-origin" });
  const doc = new DOMParser().parseFromString(await r.text(), "text/html");
  const wasOpen = document.getElementById("scheduled")?.open;
  for (const id of ids) {
    const fresh = doc.getElementById(id);
    const cur = document.getElementById(id);
    if (fresh && cur) {
      cur.replaceWith(fresh);
      fresh.animate([{ opacity: 0.4 }, { opacity: 1 }], { duration: 220 });
    }
  }
  const sched = document.getElementById("scheduled");
  if (sched && wasOpen) sched.open = true;
}

// Re-render every transaction row on the page in place (a merchant rename can change many).
async function refreshRows() {
  const items = [...document.querySelectorAll(".txn-item")];
  if (!items.length) return;
  const rows = items.map((li) => ({ id: li.dataset.id, review: !!li.closest("#inbox"), dated: !!li.closest(".dated-rows") }));
  const r = await api("/api/rows", { rows });
  for (const li of items) {
    const html = r.rows[li.dataset.id];
    if (!html) continue;
    const tpl = document.createElement("template");
    tpl.innerHTML = html.trim();
    const fresh = tpl.content.firstElementChild;
    if (fresh && fresh.outerHTML !== li.outerHTML) {
      li.replaceWith(fresh);
      fresh.querySelector(".txn")?.animate([{ opacity: 0.5 }, { opacity: 1 }], { duration: 260 });
    }
  }
}

// Bill and category tag colors come from /colors.css; fetch it again so a new bill's color shows.
function refreshColors() {
  const link = document.querySelector('link[href^="/colors.css"]');
  if (link) link.href = `/colors.css?v=${Date.now()}`;
}

// After an edit: pages with transaction lists update in place; other pages reload.
async function refreshInPlace() {
  const page = document.body.dataset.page;
  if (page !== "home" && page !== "search" && page !== "insights") { setTimeout(() => reloadHere(), 600); return; }
  try {
    refreshColors();
    await refreshRows();
    if (page === "home") await swapSections(["coverage", "scheduled-wrap"]);
  } catch (e) { reloadHere(); }
}

// ---------- name suggestions while typing in the transaction editor ----------

const suggest = {
  names: null,
  active: -1,
  list() { return document.getElementById("name-suggest"); },
  input() { return document.getElementById("sheet-merchant"); },
  async load() {
    if (this.names) return this.names;
    try {
      const r = await fetch("/api/merchant-names", { credentials: "same-origin" });
      this.names = (await r.json()).names || [];
    } catch (e) { this.names = []; }
    return this.names;
  },
  hide() {
    const ul = this.list();
    if (!ul) return;
    ul.hidden = true;
    ul.innerHTML = "";
    this.active = -1;
    this.input()?.setAttribute("aria-expanded", "false");
  },
  async show() {
    const input = this.input();
    const q = input.value.trim().toLowerCase();
    const seq = (this.seq = (this.seq || 0) + 1);
    if (!q) { this.hide(); return; }
    const names = await this.load();
    if (seq !== this.seq) return;  // a newer keystroke already ran
    const starts = [], has = [];
    for (const n of names) {
      const l = n.toLowerCase();
      if (l === q) continue;
      if (l.startsWith(q)) starts.push(n);
      else if (l.includes(q)) has.push(n);
      if (starts.length >= 6) break;
    }
    const pick = [...starts, ...has].slice(0, 6);
    const ul = this.list();
    ul.innerHTML = "";
    this.active = -1;
    if (!pick.length) { this.hide(); return; }
    for (const n of pick) {
      const li = document.createElement("li");
      li.setAttribute("role", "option");
      li.textContent = n;
      ul.appendChild(li);
    }
    ul.hidden = false;
    input.setAttribute("aria-expanded", "true");
  },
  choose(text) {
    this.input().value = text;
    this.hide();
  },
  move(step) {
    const items = [...this.list().children];
    if (!items.length) return;
    this.active = (this.active + step + items.length) % items.length;
    items.forEach((li, i) => li.classList.toggle("active", i === this.active));
  },
  init() {
    const input = this.input();
    if (!input) return;
    input.addEventListener("input", () => this.show());
    input.addEventListener("focus", () => this.load());
    input.addEventListener("blur", () => setTimeout(() => this.hide(), 250));
    input.addEventListener("keydown", (ev) => {
      if (this.list().hidden) return;
      if (ev.key === "ArrowDown") { ev.preventDefault(); this.move(1); }
      else if (ev.key === "ArrowUp") { ev.preventDefault(); this.move(-1); }
      else if (ev.key === "Enter" && this.active >= 0) { ev.preventDefault(); this.choose(this.list().children[this.active].textContent); }
      else if (ev.key === "Escape") { ev.preventDefault(); this.hide(); }
    });
    // Pick on a completed tap (click), never on touch start, so scrolling or dismissing the
    // keyboard over the list cannot choose a name. mousedown is cancelled to keep focus on desktop.
    this.list().addEventListener("mousedown", (ev) => ev.preventDefault());
    this.list().addEventListener("click", (ev) => {
      const li = ev.target.closest("li");
      if (li) this.choose(li.textContent);
    });
  },
};

// Pop-up editors: any element with data-editor="<template id>" opens that template in a sheet.
const editor = {
  open(id) {
    const tpl = document.getElementById(id);
    const sheetEl = document.getElementById("edit-sheet");
    if (!tpl || !sheetEl) return;
    const body = document.getElementById("edit-body");
    body.innerHTML = "";
    body.appendChild(tpl.content.cloneNode(true));
    sheetEl.scrollTop = 0;
    openSheet(sheetEl);
  },
  openHTML(html) {
    const sheetEl = document.getElementById("edit-sheet");
    const body = document.getElementById("edit-body");
    if (!sheetEl || !body) return;
    body.innerHTML = html;
    sheetEl.scrollTop = 0;
    openSheet(sheetEl);
  },
  init() {
    const sheetEl = document.getElementById("edit-sheet");
    if (!sheetEl) return;
    swipeToClose(sheetEl);
    document.addEventListener("click", (ev) => {
      const trigger = ev.target.closest("[data-editor]");
      if (trigger && !ev.target.closest(".sheet")) { this.open(trigger.dataset.editor); return; }
      if (ev.target.closest(".sheet-close")) closeSheet(ev.target.closest(".sheet"));
    });
    document.addEventListener("keydown", (ev) => {
      const trigger = ev.target.closest && ev.target.closest(".edit-row[data-editor]");
      if (trigger && (ev.key === "Enter" || ev.key === " ")) { ev.preventDefault(); this.open(trigger.dataset.editor); }
    });
  },
};

// ---------- money fields: digits fill in from the cents, like a register (150000 -> 1,500.00) ----------

const moneyText = (n) => n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const moneyValue = (s) => parseFloat(String(s || "").replace(/[,$]/g, ""));

function formatMoneyInput(input) {
  const digits = input.value.replace(/\D/g, "").replace(/^0+/, "").slice(0, 11);
  input.value = digits ? moneyText(Number(digits) / 100) : "";
}

document.addEventListener("input", (ev) => {
  if (ev.target.matches("input.money")) formatMoneyInput(ev.target);
});

document.addEventListener("input", (ev) => {
  if (ev.target.id === "sheet-tip") sheet.syncTip("amount");
  if (ev.target.id === "sheet-tip-pct") {
    ev.target.value = ev.target.value.replace(/[^0-9.]/g, "").replace(/(\..*)\./g, "$1");
    sheet.syncTip("pct");
  }
});

// ---------- transaction editor sheet ----------

const sheet = {
  current: null,
  cats: null,

  async open(li) {
    const el = document.getElementById("sheet");
    if (!el) return;
    const { source, id } = li.dataset;
    try {
      const [t, cats] = await Promise.all([
        fetch(`/api/transactions/${source}/${encodeURIComponent(id)}`, { credentials: "same-origin" }).then((r) => r.json()),
        this.cats ? Promise.resolve(this.cats) : fetch("/api/categories", { credentials: "same-origin" }).then((r) => r.json()),
      ]);
      this.cats = cats;
      this.current = t;
      const amt = -t.amount;
      document.getElementById("sheet-amount").textContent =
        (amt < 0 ? "-" : "") + "$" + Math.abs(amt).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      document.getElementById("sheet-amount").style.color = amt > 0 ? "var(--green)" : "";
      const d = new Date(t.date + "T12:00:00");
      document.getElementById("sheet-sub").textContent =
        [d.toLocaleDateString("en-US", { weekday: "short", month: "long", day: "numeric", year: "numeric" }),
         t.account, t.pending ? "Pending" : null].filter(Boolean).join(" · ");
      const form = document.getElementById("sheet-form");
      form.merchant.value = t.display_name;
      form.note.value = t.note;
      form.tip.value = t.tip ? moneyText(t.tip) : "";
      this.tipStart = form.tip.value;
      this.syncTip("amount");
      const tipHint = document.getElementById("sheet-tip-hint");
      tipHint.hidden = !(t.tip && !t.tip_is_manual);
      tipHint.textContent = "Tip worked out from the pending amount. Change it to correct it, or clear it to remove it.";
      const sel = form.category_id;
      sel.innerHTML = "";
      for (const c of cats.categories) {
        const o = document.createElement("option");
        o.value = c.id; o.textContent = c.name;
        if (c.id === t.category_id) o.selected = true;
        sel.appendChild(o);
      }
      // Bill or income: automatic (by the bill's rule), none, a specific one, or a new one.
      const bill = form.bill_id;
      bill.innerHTML = "";
      const auto = t.bill_choice === null && t.bill ? `Automatic (↻ ${t.bill})` : "Automatic";
      const opts = [["", t.bill_choice === null && !t.bill ? "None" : auto], ["0", amt > 0 ? "Not recurring income" : "Not a bill"],
                    ...t.bills.map((b) => [String(b.id), `↻ ${b.name}`]), ["new", amt > 0 ? "＋ Make it recurring income…" : "＋ Make it a bill…"]];
      for (const [v, label] of opts) {
        const o = document.createElement("option");
        o.value = v; o.textContent = label;
        bill.appendChild(o);
      }
      bill.value = t.bill_choice === null ? "" : String(t.bill_choice);
      // Edits apply to just this transaction unless "All from this merchant" is picked.
      form.scope.value = "this";
      // Similar merchants: each one is its own choice, and none is included unless checked.
      const list = document.getElementById("sheet-similar-list");
      list.innerHTML = "";
      for (const s of t.similar) {
        const row = document.createElement("label");
        row.className = "similar-row";
        const box = document.createElement("input");
        box.type = "checkbox"; box.name = "similar"; box.value = s.key;
        const text = document.createElement("span");
        text.className = "grow";
        const name = document.createElement("strong");
        name.textContent = s.name;
        const meta = document.createElement("span");
        meta.className = "muted small";
        meta.textContent = `${s.count} transaction${s.count > 1 ? "s" : ""}${s.sample !== s.name ? ` · bank: ${s.sample}` : ""}`;
        text.append(name, meta);
        row.append(box, text);
        list.appendChild(row);
      }
      this.updateHint();
      document.getElementById("sheet-bank").textContent =
        t.bank_name && t.bank_name !== t.display_name ? `Bank description: ${t.bank_name}` : "";
      this.showDetails(t);
      suggest.hide();
      openSheet(el);
      this.li = li;
    } catch (e) { toast("Could not open this transaction", true); }
  },

  // Plaid's extra facts (time, place, website, payment details), only the ones that exist.
  showDetails(t) {
    const d = t.details || {};
    const logo = document.getElementById("sheet-logo");
    logo.hidden = !d.logo;
    if (d.logo) { logo.src = d.logo; logo.onerror = () => { logo.hidden = true; }; } else logo.removeAttribute("src");
    const fmt = (iso) => new Date(iso + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
    const rows = [];
    if (d.time) rows.push(["Time", d.time]);
    if (d.authorized_date) rows.push(["Purchased", fmt(d.authorized_date)]);
    if (d.authorized_date) rows.push(["Posted", fmt(d.posted_date)]);
    if (d.street || d.place) {
      rows.push(["Where", [d.street, d.place, d.country].filter(Boolean).join("\n") + (d.store ? `\nStore ${d.store}` : "")]);
    }
    if (d.website) rows.push(["Website", d.website, `https://${d.website.replace(/^https?:\/\//, "")}`]);
    for (const c of d.counterparties || []) rows.push([c.type ? c.type[0].toUpperCase() + c.type.slice(1) : "Via", c.name]);
    if (d.payee) rows.push(["Payee", d.payee]);
    if (d.payer) rows.push(["Payer", d.payer]);
    if (d.processor) rows.push(["Processor", d.processor]);
    if (d.reference) rows.push(["Reference", d.reference]);
    if (d.check) rows.push(["Check", `#${d.check}`]);
    if (d.original && d.original !== t.bank_name) rows.push(["Original description", d.original]);
    const list = document.getElementById("sheet-details-list");
    list.innerHTML = "";
    for (const [label, value, href] of rows) {
      const row = document.createElement("div");
      row.className = "row detail";
      const l = document.createElement("span");
      l.className = "detail-label"; l.textContent = label;
      const v = document.createElement(href ? "a" : "span");
      v.className = "detail-value"; v.textContent = value;
      if (href) { v.href = href; v.target = "_blank"; v.rel = "noopener noreferrer"; }
      row.append(l, v);
      list.appendChild(row);
    }
    if (d.shop) {
      const a = document.createElement("a");
      a.className = "maps-btn"; a.href = d.shop.url; a.target = "_blank"; a.rel = "noopener noreferrer";
      a.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg><span></span>';
      a.querySelector("span").textContent = d.shop.label;
      list.appendChild(a);
    }
    if (d.maps) {
      const a = document.createElement("a");
      a.className = "maps-btn"; a.href = d.maps; a.target = "_blank"; a.rel = "noopener noreferrer";
      a.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 21s-7-6.2-7-11.5A7 7 0 0 1 19 9.5C19 14.8 12 21 12 21z"/><circle cx="12" cy="9.5" r="2.5"/></svg><span>Show in Maps</span>';
      list.appendChild(a);
    }
    document.getElementById("sheet-details").hidden = !list.children.length;
  },

  // The tip in dollars and as a percent of the price before the tip stay in step as either is typed.
  syncTip(from) {
    const t = this.current;
    if (!t) return;
    const form = document.getElementById("sheet-form");
    const total = Math.abs(t.amount);
    const round1 = (x) => String(Math.round(x * 10) / 10);
    if (from === "amount") {
      const tip = moneyValue(form.tip.value);
      form.tip_pct.value = tip > 0 && tip < total ? round1(100 * tip / (total - tip)) : "";
    } else {
      const pct = parseFloat(form.tip_pct.value);
      form.tip.value = pct > 0 ? moneyText(total * pct / (100 + pct)) : "";
    }
  },

  updateHint() {
    const t = this.current;
    if (!t) return;
    const form = document.getElementById("sheet-form");
    const merchant = form.scope.value === "merchant";
    document.getElementById("sheet-hint").textContent = merchant
      ? `Changes apply to ${t.merchant_count > 1 ? `all ${t.merchant_count} transactions` : "every transaction"} from this merchant, past and future.`
      : "Only this transaction changes.";
    document.getElementById("sheet-similar").hidden = !merchant || !t.similar.length;
    const picked = [...form.querySelectorAll('input[name="similar"]:checked')].map((b) => b.value);
    const n = t.similar.filter((s) => picked.includes(s.key)).reduce((a, s) => a + s.count, 0);
    document.getElementById("sheet-similar-sum").textContent = picked.length
      ? `${picked.length} more merchant${picked.length > 1 ? "s" : ""} (${n} transaction${n > 1 ? "s" : ""}) will get the same name and category.`
      : "Only this merchant changes. Check others to include them.";
  },

  close() {
    closeSheet(document.getElementById("sheet"));
  },

  async save(ev) {
    ev.preventDefault();
    const t = this.current;
    const form = ev.target;
    const body = {};
    const scope = form.scope.value;
    const wasThis = t.category_is_override || t.name_is_override;
    // Only a change the person made counts: opening and saving with the default "Just this one"
    // doesn't pin a merchant-wide category onto this transaction.
    const scopeChanged = scope === "merchant" && wasThis;
    // A one-off name becomes the merchant's name when "All from this merchant" is chosen.
    if (form.merchant.value.trim() !== t.display_name || (scope === "merchant" && t.name_is_override)) {
      body.display_name = form.merchant.value;
    }
    const cid = Number(form.category_id.value);
    if (cid !== t.category_id || scopeChanged) body.category_id = cid;
    if (form.note.value.trim() !== t.note) body.note = form.note.value;
    if (form.tip.value !== this.tipStart) body.tip = form.tip.value || null;
    const bill = form.bill_id.value;
    if (bill !== "new" && bill !== (t.bill_choice === null ? "" : String(t.bill_choice))) body.bill_id = bill;
    const picked = [...form.querySelectorAll('input[name="similar"]:checked')].map((b) => b.value);
    if (scope === "merchant" && picked.length) body.similar = picked;
    if (Object.keys(body).length) body.scope = scope;
    try {
      if (Object.keys(body).length) await api(`/api/transactions/${t.source}/${encodeURIComponent(t.id)}`, body);
      if (bill === "new") {
        // Open the bill form filled in from this transaction.
        const r = await fetch(`/api/bills/form?source=${t.source}&id=${encodeURIComponent(t.id)}`, { credentials: "same-origin" });
        if (!r.ok) throw new Error("Could not open the bill form");
        this.close();
        editor.openHTML(await r.text());
        return;
      }
      this.close();
      if (!Object.keys(body).length) return;
      const wide = scope === "merchant" && (body.display_name !== undefined || body.category_id !== undefined);
      toast(wide ? "Saved for every transaction from this merchant" : "Saved");
      await refreshInPlace();
    } catch (e) { toast(e.message, true); }
  },

  init() {
    const el = document.getElementById("sheet");
    if (!el) return;
    document.addEventListener("click", (ev) => {
      if (ev.target.closest(".tick, .load-more, a, button:not(.txn), .sheet")) return;
      const row = ev.target.closest(".txn");
      if (row) this.open(row.closest(".txn-item"));
    });
    document.addEventListener("keydown", (ev) => {
      if ((ev.key === "Enter" || ev.key === " ") && ev.target.classList.contains("txn")) {
        ev.preventDefault();
        this.open(ev.target.closest(".txn-item"));
      }
      if (ev.key === "Escape") closeAllSheets();
    });
    document.getElementById("scrim").addEventListener("click", closeAllSheets);
    document.getElementById("sheet-cancel").addEventListener("click", () => this.close());
    document.getElementById("sheet-form").addEventListener("submit", (ev) => this.save(ev));
    swipeToClose(el);
  },
};

// The day header pinned at the top right now gets .stuck (drawn as frosted glass up to the top edge).
(() => {
  let pending = false;
  const mark = () => {
    pending = false;
    const heads = document.querySelectorAll(".day-group > .day");
    if (!heads.length) return;
    const stick = parseFloat(getComputedStyle(heads[0]).top) || 0;
    document.documentElement.style.setProperty("--stick", stick + "px");
    heads.forEach((h) => {
      const group = h.parentElement.getBoundingClientRect();
      h.classList.toggle("stuck", group.top < stick - 0.5 && h.getBoundingClientRect().top <= stick + 0.5);
    });
  };
  const queue = () => { if (!pending) { pending = true; requestAnimationFrame(mark); } };
  window.addEventListener("scroll", queue, { passive: true });
  window.addEventListener("resize", queue);
  document.addEventListener("DOMContentLoaded", queue);
})();

// A day split across two pages of results becomes one panel again.
function mergeDays(root) {
  root.querySelectorAll(".day-group + .day-group").forEach((g) => {
    const prev = g.previousElementSibling;
    if (prev.dataset.day !== g.dataset.day) return;
    prev.querySelector(".list").append(...g.querySelector(".list").children);
    g.remove();
  });
}

function bindLoadMore(container) {
  container.addEventListener("click", async (ev) => {
    const btn = ev.target.closest(".load-more");
    if (!btn) return;
    btn.disabled = true;
    btn.textContent = "Loading…";
    try {
      const q = viewQuery();
      const r = await fetch(`/api/transactions/page?before=${encodeURIComponent(btn.dataset.before)}${q ? "&" + q : ""}`, { credentials: "same-origin" });
      const html = await r.text();
      const tmp = document.createElement("div");
      tmp.innerHTML = html;
      btn.replaceWith(...tmp.childNodes);
      mergeDays(container);
    } catch (e) { btn.disabled = false; btn.textContent = "Show more"; toast("Could not load more", true); }
  });
}

// ---------- color picker ----------

function colorValue(form) {
  const picked = form.querySelector('input[name="color"]:checked');
  if (!picked) return null;
  if (picked.value !== "custom") return picked.value;
  let hex = form.elements.custom_hex.value.trim();
  if (!hex.startsWith("#")) hex = "#" + hex;
  return hex;
}

document.addEventListener("change", (ev) => {
  const t = ev.target;
  if (t.matches('.color-picker input[name="color"]')) {
    t.closest(".color-picker").querySelector(".hex-row").hidden = t.value !== "custom";
  }
  if (t.matches('.bill-form input[name="kind"]')) {
    const income = t.value === "income";
    t.form.querySelectorAll(".kind-bill").forEach((el) => { el.hidden = income; });
    t.form.querySelectorAll(".kind-income").forEach((el) => { el.hidden = !income; });
  }
  if (t.matches('#sheet-form input[name="scope"], #sheet-form input[name="similar"]')) sheet.updateHint();
  if (t.matches(".bill-form .cadence-pick")) {
    const every = t.form.querySelector(".every-field");
    const custom = t.value === "months" || t.value === "years";
    every.hidden = !custom;
    every.firstChild.textContent = t.value === "years" ? "Years between" : "Months between";
  }
  if (t.matches(".bill-form .date-varies")) {
    t.form.querySelector(".window-fields").disabled = !t.checked;
    t.form.querySelectorAll(".date-fixed").forEach((el) => { el.hidden = t.checked; });
    t.form.querySelectorAll(".date-window").forEach((el) => { el.hidden = !t.checked; });
  }
  if (t.matches('.bill-form input[name="varies"]')) {
    const amount = t.form.elements.amount;
    amount.disabled = t.checked;
    amount.placeholder = t.checked ? "Average of recent charges" : "0.00";
  }
});

document.addEventListener("input", (ev) => {
  const t = ev.target;
  if (t.matches('input[name="custom_picker"]')) {
    t.closest(".hex-row").querySelector('input[name="custom_hex"]').value = t.value.toUpperCase();
  }
  if (t.matches('input[name="custom_hex"]')) {
    const v = t.value.trim().replace(/^#?/, "#");
    if (/^#[0-9a-fA-F]{6}$/.test(v)) t.closest(".hex-row").querySelector('input[name="custom_picker"]').value = v.toLowerCase();
  }
});

// Forms inside pop-up editors are created on demand, so submits and removals are delegated.
const forms = {
  async "bill-form"(form) {
    const f = form.elements;
    await api("/api/bills", {
      id: form.dataset.id || null, name: f.title.value, amount: f.amount.value,
      cadence: f.cadence.value, due_day: f.due_day.value, match: f.match.value,
      next_due: f.next_due.value, active: f.active.checked, color: colorValue(form),
      varies: f.varies.checked, account_id: f.account_id.value, kind: f.kind.value,
      is_payday: f.is_payday.checked,
      every: f.every.value, date_varies: f.date_varies.checked, window_months: f.window_months.value,
      from_txn: form.dataset.fromTxn || null,
    });
  },
  async "cat-form"(form) {
    await api("/api/categories", { id: form.dataset.id || null, name: form.elements.title.value,
                                   color: colorValue(form), kind: form.elements.kind.value });
  },
  async "balance-form"(form) {
    const f = form.elements;
    await api("/api/balances", { account_id: form.dataset.account, balance: f.balance.value, day: f.day.value,
                                 flow: f.flow ? f.flow.value : undefined });
  },
  async "loan-form"(form) {
    const f = form.elements;
    await api("/api/loans", {
      account_id: form.dataset.account || undefined, name: f.name ? f.name.value : undefined,
      balance: f.balance ? f.balance.value : undefined, day: f.day ? f.day.value : undefined,
      rate: f.rate.value, payment: f.payment.value, bill_id: f.bill_id.value, recert: f.recert.value,
      original: f.original.value,
      pslf: f.pslf.checked, pslf_count: f.pslf_count.value, pslf_as_of: f.pslf_as_of.value,
      months: Object.fromEntries([...form.querySelectorAll(".pslf-month")].map((s) => [s.dataset.month, s.value === "1"])),
    });
  },
  async "plan-form"(form) {
    await api("/api/investments/plan", { year: form.dataset.year, plan: form.dataset.plan, amount: form.elements.amount.value });
  },
  async "manual-form"(form) {
    const f = form.elements;
    await api("/api/manual-accounts", { name: f.name.value, kind: f.kind.value, balance: f.balance.value, day: f.day.value });
  },
  async "acct-form"(form) {
    const f = form.elements;
    await api(`/api/accounts/${encodeURIComponent(form.dataset.account)}/rename`,
              { name: f.title.value, short_name: f.short.value, visibility: f.visibility.value });
  },
  async "payday-form"(form) {
    const f = form.elements;
    const kind = f.kind.value;
    await api("/api/paydays", kind === "biweekly"
      ? { kind, anchor: f.anchor.value }
      : { kind, days: f.days.value.split(/[^0-9]+/).filter(Boolean), start: f.start.value });
  },
};

document.addEventListener("submit", async (ev) => {
  const form = ev.target;
  const key = Object.keys(forms).find((k) => form.classList.contains(k) || form.id === k);
  if (!key) return;
  ev.preventDefault();
  const btn = form.querySelector('button[type="submit"]');
  if (btn) btn.disabled = true;
  try {
    await forms[key](form);
    closeAllSheets();
    toast("Saved");
    if (key === "bill-form") await refreshInPlace();
    else setTimeout(() => reloadHere(), 600);
  } catch (e) { if (btn) btn.disabled = false; toast(e.message, true); }
});

document.addEventListener("click", async (ev) => {
  const btn = ev.target.closest(".archive-bill");
  if (!btn) return;
  const restore = !!btn.dataset.restore;
  try {
    await api(`/api/bills/${btn.dataset.id}/archive`, { restore });
    closeAllSheets();
    toast(restore ? "Restored" : "Archived");
    await refreshInPlace();
  } catch (e) { toast(e.message, true); }
});

// Search: changing the sort runs the search again in that order.
document.addEventListener("change", (ev) => {
  if (ev.target.id === "search-sort") document.getElementById("search-form")?.requestSubmit();
});

// Insights: leave an account out of net worth, or include it again.
document.addEventListener("click", async (ev) => {
  const btn = ev.target.closest(".net-worth-toggle");
  if (!btn) return;
  try {
    await api(`/api/accounts/${encodeURIComponent(btn.dataset.account)}/net-worth`, { include: !!btn.dataset.include });
    closeAllSheets();
    toast(btn.dataset.include ? "Included" : "Left out");
    setTimeout(() => reloadHere(), 500);
  } catch (e) { toast(e.message, true); }
});

// Insights: balances entered by hand, and accounts added by hand.
document.addEventListener("click", async (ev) => {
  const btn = ev.target.closest(".delete-balance, .delete-manual, .rename-manual");
  if (!btn) return;
  try {
    if (btn.classList.contains("delete-balance")) {
      await api("/api/balances/delete", { account_id: btn.dataset.account, day: btn.dataset.day });
      await fold(btn.closest("li"), 0);
      toast("Removed");
      return;
    }
    if (btn.classList.contains("delete-manual")) {
      if (!confirm("Remove this account and every balance entered for it?")) return;
      await api("/api/manual-accounts/delete", { id: btn.dataset.id });
    } else {
      const form = btn.closest("form");
      await api("/api/manual-accounts", { id: btn.dataset.id, name: form.elements.name.value, kind: btn.dataset.kind });
    }
    closeAllSheets();
    toast("Saved");
    setTimeout(() => reloadHere(), 600);
  } catch (e) { toast(e.message, true); }
});

document.addEventListener("click", async (ev) => {
  const undo = ev.target.closest(".undo-rule");
  if (!undo) return;
  if (!confirm("Undo this rename? Its transactions go back to the bank's name and default category.")) return;
  try {
    await api("/api/rules/delete", { key: undo.dataset.key });
    await fold(undo.closest("li"), 0);
    toast("Undone");
  } catch (e) { toast(e.message, true); }
});

document.addEventListener("click", async (ev) => {
  const del = ev.target.closest(".delete-bill, .delete-cat");
  if (!del) return;
  const isBill = del.classList.contains("delete-bill");
  if (!confirm(isBill ? "Delete this permanently? Its tag comes off every past transaction. (Archive keeps the tags.)" : "Remove this category? Its transactions go back to their default category.")) return;
  try {
    await api(isBill ? `/api/bills/${del.dataset.id}/delete` : `/api/categories/${del.dataset.id}/delete`);
    closeAllSheets();
    toast("Removed");
    if (isBill) await refreshInPlace();
    else setTimeout(() => reloadHere(), 600);
  } catch (e) { toast(e.message, true); }
});

// ---------- pages ----------

const pages = {
  setup() {
    const form = document.getElementById("setup-form");
    if (!form) return;
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      say("Waiting for passkey");
      try {
        await createPasskey(form.code.value, form.label.value || "Passkey");
        location.href = "/";
      } catch (e) { say(e.message, true); }
    });
  },

  login() {
    const btn = document.getElementById("signin");
    btn.addEventListener("click", async () => {
      say("");
      try { await usePasskey(); location.href = "/"; } catch (e) { say(e.message, true); }
    });
    const form = document.getElementById("code-form");
    form?.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const msg = document.getElementById("code-msg");
      msg.textContent = "";
      try { await api("/api/signin-code/use", { code: form.code.value }); location.href = "/"; }
      catch (e) { msg.textContent = e.message; }
    });
  },

  oauth() {
    const state = JSON.parse(localStorage.getItem(LINK_KEY) || "{}");
    if (!state.token) { say("This bank connection expired. Start again from the Accounts page.", true); return; }
    plaidHandler(state.token, { receivedRedirectUri: window.location.href }).open();
  },

  home() {
    const sync = document.getElementById("sync");
    if (sync) sync.addEventListener("click", syncNow);
    let lastRefresh = Date.now();

    const updateCount = () => {
      const list = document.getElementById("inbox");
      const count = document.getElementById("inbox-count");
      const n = list.querySelectorAll(".txn-item:not(.leaving)").length;
      count.textContent = n;
      count.classList.toggle("zero", !n);
      const badge = document.getElementById("tab-badge");
      if (badge) { badge.textContent = n; badge.hidden = !n; badge.setAttribute("aria-label", `${n} to review`); }
      appBadge();
      count.classList.remove("done-pop"); void count.offsetWidth; count.classList.add("done-pop");
      if (!n) {
        const empty = document.getElementById("inbox-empty");
        empty.hidden = false;
        empty.classList.add("done-pop");
        const all = document.getElementById("review-all");
        if (all) all.hidden = true;
      }
    };

    // Swap in fresh copies of page sections without a reload.
    const refresh = async (ids) => {
      await swapSections(ids);
      lastRefresh = Date.now();
    };

    document.addEventListener("click", async (ev) => {
      const btn = ev.target.closest("#inbox .tick");
      if (!btn) return;
      ev.stopPropagation();
      const li = btn.closest(".txn-item");
      btn.classList.add("checked");
      btn.disabled = true;
      try {
        await api(`/api/transactions/${li.dataset.source}/${encodeURIComponent(li.dataset.id)}/review`, { reviewed: true });
        // The other side of a transfer is reviewed with it.
        const peer = li.dataset.peer && document.querySelector(`#inbox .txn-item[data-id="${CSS.escape(li.dataset.peer)}"]`);
        if (peer) peer.querySelector(".tick")?.classList.add("checked");
        await Promise.all([fold(li, 250), peer ? fold(peer, 250) : null]);
        updateCount();
        refresh(["all-head", "all"]);  // the reviewed transaction now belongs in All transactions
      } catch (e) { btn.classList.remove("checked"); btn.disabled = false; toast(e.message, true); }
    });

    document.addEventListener("click", async (ev) => {
      const all = ev.target.closest("#review-all");
      if (!all) return;
      const items = [...document.querySelectorAll("#inbox .txn-item")];
      if (!items.length || !confirm(`Mark all ${items.length} as reviewed?`)) return;
      all.disabled = true;
      try {
        await api("/api/inbox/review-all", { ids: items.map((li) => li.dataset.id) });
        items.forEach((li) => li.querySelector(".tick")?.classList.add("checked"));
        await Promise.all(items.map((li, i) => fold(li, 120 + Math.min(i, 20) * 45)));
        updateCount();
        toast(`${items.length} reviewed`);
        refresh(["all"]);
      } catch (e) { all.disabled = false; toast(e.message, true); }
    });

    // Returning to the app (home screen icon or tab) picks up anything the background sync added.
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible" && Date.now() - lastRefresh > 60000 &&
          !document.querySelector(".sheet.open")) {
        refresh(["coverage", "review-card", "scheduled-wrap", "all-head", "all"]).catch(() => {});
      }
    });

    document.addEventListener("click", async (ev) => {
      const more = ev.target.closest("#more-filters");
      if (more) {
        const open = more.getAttribute("aria-expanded") !== "true";
        more.setAttribute("aria-expanded", open ? "true" : "false");
        document.querySelectorAll("#filters .less-used").forEach((b) => { b.hidden = !open; });
        if (!more.dataset.label) more.dataset.label = more.textContent;
        more.textContent = open ? "Fewer" : more.dataset.label;
        return;
      }
      const btn = ev.target.closest("#filters .filter");
      if (!btn || btn.classList.contains("on")) return;
      const filters = btn.closest("#filters");
      filters.querySelectorAll(".filter").forEach((b) => {
        const on = b === btn;
        b.classList.toggle("on", on);
        b.setAttribute("aria-checked", on ? "true" : "false");
      });
      const dim = ["all", "scheduled-wrap"].map((id) => document.getElementById(id));
      dim.forEach((el) => { el.style.transition = "opacity .15s"; el.style.opacity = "0.25"; });
      try {
        view.account = btn.dataset.account || "";
        await refresh(["all-head", "scheduled-wrap", "all"]);
      } catch (e) { dim.forEach((el) => { el.style.opacity = "1"; }); toast(e.message, true); }
    });

    document.addEventListener("click", async (ev) => {
      const btn = ev.target.closest("#pending-filter");
      if (!btn) return;
      const on = !btn.classList.contains("on");
      btn.classList.toggle("on", on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
      const cur = document.getElementById("all");
      cur.style.transition = "opacity .15s";
      cur.style.opacity = "0.25";
      try {
        view.pending = on;
        await refresh(["all-head", "all"]);
      } catch (e) { cur.style.opacity = "1"; toast(e.message, true); }
    });
    bindLoadMore(document.body);
  },

  accounts() {
    const on = (id, fn) => { const el = document.getElementById(id); if (el) el.addEventListener("click", fn); };
    on("connect", () => openLink(null));
    on("connect-invest", (ev) => { ev.preventDefault(); openLink(null, false, true); });
    document.querySelectorAll(".add-investments").forEach((b) =>
      b.addEventListener("click", () => openLink(b.dataset.item, false, true)));
    on("logout", async (ev) => { ev.preventDefault(); await api("/api/logout"); location.href = "/login"; });
    let codeTimer;
    on("signin-code-new", async () => {
      try {
        const { code, expires_in } = await api("/api/signin-code");
        const until = Date.now() + expires_in * 1000;
        document.getElementById("signin-code-value").textContent = code;
        document.getElementById("signin-code-show").hidden = false;
        document.getElementById("signin-code-new").textContent = "Show a new code";
        const left = document.getElementById("signin-code-left");
        clearInterval(codeTimer);
        const tick = () => {
          const s = Math.max(0, Math.round((until - Date.now()) / 1000));
          left.textContent = s ? `Expires in ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : "Expired";
          if (!s) clearInterval(codeTimer);
        };
        tick();
        codeTimer = setInterval(tick, 1000);
      } catch (e) { toast(e.message, true); }
    });
    on("backup-download", async () => {
      // POST with the app header (not a plain link), so only this page can ask for the file.
      try {
        const r = await fetch("/api/backup", { method: "POST", headers: { "X-Finance": "1" }, credentials: "same-origin" });
        if (!r.ok) throw new Error("Could not make the backup");
        const name = (r.headers.get("Content-Disposition") || "").match(/filename="([^"]+)"/)?.[1] || "finance-backup.db";
        const url = URL.createObjectURL(await r.blob());
        const a = Object.assign(document.createElement("a"), { href: url, download: name });
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 10000);
        toast("Backup downloaded");
      } catch (e) { toast(e.message, true); }
    });
    on("sync", syncNow);
    document.querySelectorAll(".reconnect").forEach((b) =>
      b.addEventListener("click", () => openLink(b.dataset.item)));
    document.querySelectorAll(".choose-accounts").forEach((b) =>
      b.addEventListener("click", () => openLink(b.dataset.item, true)));
  },

  search() {
    bindLoadMore(document.body);
    const form = document.getElementById("search-form");
    if (!form) return;
    // Date fields stay plain text ("Any") until tapped, then become date pickers.
    // Date fields stay plain text until tapped (handled for every page below).
    // Quick picks: a month, a year, or both (that month of that year). A month alone means its
    // most recent occurrence. Tapping a chosen chip again clears it. The dates fill in and it searches.
    form.addEventListener("click", (ev) => {
      const quick = ev.target.closest(".quick-month, .quick-year");
      if (quick) {
        const group = quick.classList.contains("quick-month") ? ".quick-month" : ".quick-year";
        const wasOn = quick.classList.contains("on");
        form.querySelectorAll(group).forEach((b) => b.classList.remove("on"));
        if (!wasOn) quick.classList.add("on");
        const m = Number(form.querySelector(".quick-month.on")?.dataset.month || 0);
        let y = Number(form.querySelector(".quick-year.on")?.dataset.year || 0);
        const pad = (n) => String(n).padStart(2, "0");
        let start = "", end = "";
        if (m) {
          if (!y) {
            const now = new Date();
            y = m > now.getMonth() + 1 ? now.getFullYear() - 1 : now.getFullYear();
          }
          const last = new Date(y, m, 0).getDate();
          start = `${y}-${pad(m)}-01`; end = `${y}-${pad(m)}-${pad(last)}`;
        } else if (y) {
          start = `${y}-01-01`; end = `${y}-12-31`;
        }
        for (const [name, value] of [["start", start], ["end", end]]) {
          const el = form.elements[name];
          el.type = value ? "date" : "text";
          el.value = value;
        }
        form.requestSubmit();
        return;
      }
      const more = ev.target.closest(".more-chips");
      if (more) {
        more.closest(".chips").querySelectorAll(".less-used").forEach((c) => { c.hidden = false; });
        more.remove();
      }
    });
    // Leave empty fields out of the address.
    form.addEventListener("submit", () => {
      form.querySelectorAll("input[name], select[name]").forEach((el) => {
        if ((el.type === "text" || el.type === "search" || el.type === "date") && !el.value) el.disabled = true;
        if (el.type === "radio" && el.checked && !el.value) el.disabled = true;
      });
    });
  },

  compare() {
    const form = document.getElementById("links-form");
    if (!form) return;
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const links = {};
      form.querySelectorAll("select[data-plaid]").forEach((sel) => {
        const list = (links[sel.dataset.plaid] ||= []);
        if (sel.value) list.push(sel.value);
      });
      try { await api("/api/links", { links }); reloadHere(); } catch (e) { say(e.message, true); }
    });
    const names = document.getElementById("ynab-names");
    if (names) names.addEventListener("click", async () => {
      names.disabled = true;
      try {
        const r = await api("/api/ynab-names");
        toast(`${r.merchants} merchants renamed, ${r.one_off} one-off names, ${r.notes} notes`);
        setTimeout(() => reloadHere(), 1200);
      } catch (e) { toast(e.message, true); names.disabled = false; }
    });
  },

  bills() {
    const detect = document.getElementById("detect");
    if (detect) detect.addEventListener("click", async () => {
      detect.classList.add("spin");
      try {
        const r = await api("/api/bills/detect");
        if (r.count) editor.openHTML(r.html);
        else toast("No new recurring charges found");
      } catch (e) { toast(e.message, true); }
      detect.classList.remove("spin");
    });
  },
};

// "Find new bills" confirmation: add the ticked charges, or set one aside for good with "Not a bill".
const ignoredBills = new Set();
document.addEventListener("click", (ev) => {
  const btn = ev.target.closest(".not-bill");
  if (!btn) return;
  const row = btn.closest(".candidate");
  ignoredBills.add(row.dataset.match);
  row.remove();
});
document.addEventListener("submit", async (ev) => {
  const form = ev.target.closest(".detect-form");
  if (!form) return;
  ev.preventDefault();
  const add = [...form.querySelectorAll('input[name="add"]:checked')].map((i) => i.value);
  if (!add.length && !ignoredBills.size) { closeSheet(form.closest(".sheet")); return; }
  try {
    const r = await api("/api/bills/add-detected", { add, ignore: [...ignoredBills] });
    ignoredBills.clear();
    closeSheet(form.closest(".sheet"));
    toast(r.added ? `Added ${r.added} bill${r.added > 1 ? "s" : ""}` : "Saved");
    setTimeout(() => reloadHere(), 700);
  } catch (e) { toast(e.message, true); }
});

// Investments: show or hide an account, and which year an early-year IRA contribution counts for.
document.addEventListener("click", async (ev) => {
  const btn = ev.target.closest(".track-toggle");
  if (!btn) return;
  try {
    await api(`/api/accounts/${encodeURIComponent(btn.dataset.account)}/track`, { track: !!btn.dataset.track });
    closeAllSheets();
    toast(btn.dataset.track ? "Now in Investments" : "Hidden from Investments");
    setTimeout(() => reloadHere(), 600);
  } catch (e) { toast(e.message, true); }
});
document.addEventListener("change", async (ev) => {
  const sel = ev.target.closest(".contrib-bill");
  if (!sel) return;
  try {
    await api("/api/investments/contribution-bill", { account_id: sel.dataset.account, bill_id: sel.value });
    closeAllSheets();
    toast("Saved");
    setTimeout(() => reloadHere(), 600);
  } catch (e) { toast(e.message, true); }
});
document.addEventListener("change", async (ev) => {
  const sel = ev.target.closest(".tax-year");
  if (!sel) return;
  try {
    await api("/api/investments/tax-year", { id: sel.dataset.id, year: Number(sel.value) });
    closeAllSheets();
    toast(sel.value === "0" ? "Left out" : `Counts for ${sel.value}`);
    setTimeout(() => reloadHere(), 600);
  } catch (e) { toast(e.message, true); }
});

// Optional dates show their placeholder ("Any", "None") until tapped, then become date pickers,
// instead of a browser showing today's date in an empty field.
document.addEventListener("focusin", (ev) => {
  const el = ev.target;
  if (!el.classList || !el.classList.contains("lazy-date") || el.type === "date") return;
  el.type = "date";
  try { el.showPicker(); } catch (e) { /* not supported or not allowed */ }
});
document.addEventListener("focusout", (ev) => {
  const el = ev.target;
  if (el.classList && el.classList.contains("lazy-date") && !el.value) el.type = "text";
});

// Loans: the PSLF fields (count, date, recertification) only apply with PSLF checked.
document.addEventListener("change", (ev) => {
  const box = ev.target.closest && ev.target.closest(".pslf-toggle");
  if (!box) return;
  const fields = box.closest("form").querySelector(".pslf-fields");
  if (fields) fields.disabled = !box.checked;
});

// App icon badge (a Home Screen or Dock web app): the Inbox count plus the Accounts issues. Browsers
// show it only after notifications are allowed for the app, which the Accounts page asks for.
// Inside the native Finance app (iPhone, iPad, Mac), the page talks to the app through this handler:
// the app sets its own icon badge and handles notifications itself.
const nativeApp = () => !!(window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.finance);

function appBadge() {
  const count = (id) => {
    const el = document.getElementById(id);
    return el && !el.hidden ? Number(el.textContent) || 0 : 0;
  };
  const inbox = count("tab-badge"), accounts = count("tab-badge-accounts");
  const n = inbox + accounts;
  if (nativeApp()) {
    // The app sets its icon badge and, on iPhone, the badges on its own tab bar.
    window.webkit.messageHandlers.finance.postMessage({ badge: n, inbox, accounts });
    if (!n) nativeAllClear();
    return;
  }
  if (!("setAppBadge" in navigator)) return;
  (n ? navigator.setAppBadge(n) : navigator.clearAppBadge()).catch(() => { /* not allowed yet */ });
  if (!n) allClear();
}

// The native app clears its own notifications at zero; this asks the server to clear the other
// devices (with that setting on), leaving out this device's token. Once per page.
let nativeClearSent = false;
function nativeAllClear() {
  if (nativeClearSent) return;
  nativeClearSent = true;
  api("/api/push/all-clear", { device: window.financeDeviceToken || "" }).catch(() => {});
}

// Caught up: close this device's Finance notifications, and (if that setting is on) have the server
// replace the old notification on the other devices. Once per page.
let allClearSent = false;
async function allClear() {
  if (allClearSent || !("serviceWorker" in navigator)) return;
  allClearSent = true;
  try {
    const reg = await navigator.serviceWorker.getRegistration("/");
    if (!reg) return;
    if (reg.getNotifications) (await reg.getNotifications({ tag: "finance" })).forEach((x) => x.close());
    const sub = await reg.pushManager.getSubscription();
    if (sub) await api("/api/push/all-clear", { endpoint: sub.endpoint });
  } catch { /* nothing to clear */ }
}
// Notifications: a service worker shows pushes from the Pi and keeps the icon badge current.
const pushOK = () => "serviceWorker" in navigator && "PushManager" in window && typeof Notification !== "undefined";
const urlKey = (k) => Uint8Array.from(atob(k.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - k.length % 4) % 4)), (c) => c.charCodeAt(0));

document.addEventListener("DOMContentLoaded", async () => {
  appBadge();
  if (pushOK() && Notification.permission === "granted") {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
  }
  const card = document.getElementById("push-card");
  if (!card) return;
  const text = document.getElementById("push-text");
  const on = document.getElementById("push-enable");
  const test = document.getElementById("push-test");
  const off = document.getElementById("push-disable");
  if (nativeApp()) {
    // The native app gets notifications from the server through Apple; nothing to turn on here.
    card.hidden = false; on.hidden = true; off.hidden = true; test.hidden = false;
    document.getElementById("push-clear-row").hidden = false;
    text.textContent = "This app gets a notification when new transactions arrive or a bank connection needs attention, and its icon shows the count. Turn them off in Settings › Notifications › Finance.";
    document.getElementById("push-clear-others").addEventListener("change", async (ev) => {
      try { await api("/api/push/settings", { clear_others: ev.target.checked }); toast("Saved"); }
      catch (e) { toast(e.message, true); }
    });
    test.addEventListener("click", async () => {
      try { await api("/api/push/test"); toast("Sent"); } catch (e) { toast(e.message, true); }
    });
    return;
  }
  if (!pushOK()) {
    // iPhone: only the app added to the Home Screen can get notifications.
    if (/iPhone|iPad/.test(navigator.userAgent)) {
      card.hidden = false; on.hidden = true;
      text.textContent = "Notifications work in the app on your Home Screen. In Safari, tap Share, then Add to Home Screen, and open it from there.";
    }
    return;
  }
  card.hidden = false;
  const show = async () => {
    const reg = await navigator.serviceWorker.getRegistration("/");
    const sub = reg ? await reg.pushManager.getSubscription() : null;
    const denied = Notification.permission === "denied";
    on.hidden = !!sub || denied; test.hidden = !sub; off.hidden = !sub;
    document.getElementById("push-clear-row").hidden = !sub;
    text.textContent = denied
      ? "Notifications are off for this app. Turn them on in Settings › Notifications › Finance, then come back here."
      : sub ? "On for this device: new transactions and connection problems send a notification, and the icon shows the count."
            : "Get a notification when new transactions arrive or a bank connection needs attention, and keep the count on the app's icon current. On this device only.";
  };
  await show();
  on.addEventListener("click", async () => {
    try {
      if (await Notification.requestPermission() !== "granted") { await show(); return; }
      const reg = await navigator.serviceWorker.register("/sw.js", { scope: "/" });
      await navigator.serviceWorker.ready;
      const { key } = await fetch("/api/push/key", { credentials: "same-origin" }).then((r) => r.json());
      const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlKey(key) });
      await api("/api/push/subscribe", { subscription: sub.toJSON() });
      appBadge();
      toast("Notifications on");
    } catch (e) { toast(e.message || "Couldn't turn on notifications", true); }
    await show();
  });
  document.getElementById("push-clear-others").addEventListener("change", async (ev) => {
    try { await api("/api/push/settings", { clear_others: ev.target.checked }); toast("Saved"); }
    catch (e) { toast(e.message, true); }
  });
  test.addEventListener("click", async () => {
    try { await api("/api/push/test"); toast("Sent"); } catch (e) { toast(e.message, true); }
  });
  off.addEventListener("click", async () => {
    const reg = await navigator.serviceWorker.getRegistration("/");
    const sub = reg ? await reg.pushManager.getSubscription() : null;
    if (sub) { await api("/api/push/unsubscribe", { endpoint: sub.endpoint }).catch(() => {}); await sub.unsubscribe(); }
    toast("Notifications off on this device");
    await show();
  });
});

// Money in transit: choose the other account, or say it isn't a transfer between my accounts.
async function saveTransit(id, account) {
  try {
    await api("/api/transit", { id, account });
    closeAllSheets();
    toast("Saved");
    setTimeout(() => reloadHere(), 600);
  } catch (e) { toast(e.message, true); }
}
document.addEventListener("submit", (ev) => {
  const form = ev.target.closest(".transit-form");
  if (!form) return;
  ev.preventDefault();
  saveTransit(form.dataset.id, form.elements.account.value);
});
document.addEventListener("click", (ev) => {
  const btn = ev.target.closest(".transit-ignore");
  if (btn) saveTransit(btn.dataset.id, "");
});

// Tab bar: the highlight slides to the tapped tab right away, while the next page loads,
// instead of waiting for that page to arrive.
document.addEventListener("pointerdown", (ev) => {
  const link = ev.target.closest && ev.target.closest(".tabbar a");
  if (!link || ev.button > 0 || link.getAttribute("aria-current") === "page") return;
  const pill = document.querySelector(".tab-pill");
  const current = document.querySelector('.tabbar a[aria-current="page"]');
  if (current) current.removeAttribute("aria-current");
  link.setAttribute("aria-current", "page");
  if (!pill) return;
  const from = pill.getBoundingClientRect();
  link.prepend(pill);
  const to = pill.getBoundingClientRect();
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  pill.animate([
    { transform: `translate(${from.left - to.left}px, ${from.top - to.top}px)`, width: `${from.width}px`, height: `${from.height}px` },
    { transform: "none", width: `${to.width}px`, height: `${to.height}px` },
  ], { duration: 220, easing: "cubic-bezier(.32, .72, 0, 1)" });
});

// Keep browsers (Safari's Contacts AutoFill in particular) from offering personal details in app fields.
function noAutofill(root) {
  root.querySelectorAll("input:not([type=checkbox]):not([type=radio]):not([type=hidden]), textarea").forEach((el) => {
    if (!el.hasAttribute("autocomplete")) el.setAttribute("autocomplete", "off");  // explicit values (sign-in) are kept
    el.setAttribute("data-1p-ignore", "");
    el.setAttribute("data-lpignore", "true");
  });
}
new MutationObserver((muts) => {
  for (const m of muts) for (const n of m.addedNodes) if (n.nodeType === 1) noAutofill(n);
}).observe(document.documentElement, { childList: true, subtree: true });

document.addEventListener("DOMContentLoaded", () => {
  noAutofill(document);
  sheet.init();
  editor.init();
  suggest.init();
  const fn = pages[document.body.dataset.page];
  if (fn) fn();
});

// Accounts: while the server installs an update, check every few seconds and reload once it's done.
// (While the new version starts, the server is briefly unreachable; those checks just fail.)
document.addEventListener("DOMContentLoaded", () => {
  const line = document.getElementById("server-update");
  if (!line || line.dataset.state !== "updating") return;
  const timer = setInterval(async () => {
    try {
      const r = await fetch("/api/server-update", { credentials: "same-origin" });
      if (!r.ok) return;
      const info = await r.json();
      if (info.state && info.state !== "updating") { clearInterval(timer); reloadHere(); }
    } catch { /* restarting */ }
  }, 5000);
});
