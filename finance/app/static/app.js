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
          await api("/api/plaid/updated", { item_id: state.itemId });
        } else {
          await api("/api/plaid/exchange", { public_token: publicToken, institution: metadata.institution });
        }
        say("Connected. The first sync can take a few minutes.");
        setTimeout(() => { location.href = "/"; }, 1500);
      } catch (e) { say(e.message, true); }
    },
    onExit: (err) => {
      if (err) say(err.display_message || err.error_message || "Bank connection was not completed.", true);
      if (document.body.dataset.page === "oauth") setTimeout(() => { location.href = "/"; }, 2500);
    },
  });
}

async function openLink(itemId) {
  say("");
  try {
    const { link_token } = await api("/api/plaid/link-token", itemId ? { item_id: itemId } : {});
    // Saved so the OAuth return page can resume the same Link session.
    localStorage.setItem(LINK_KEY, JSON.stringify({ token: link_token, itemId: itemId || null }));
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
    setTimeout(() => location.reload(), 650);
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

// ---------- refresh without reloading (keeps the scroll position) ----------

// Swap in fresh copies of sections of the home page.
async function swapSections(ids) {
  const r = await fetch("/", { credentials: "same-origin" });
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
  const rows = items.map((li) => ({ id: li.dataset.id, review: !!li.closest("#inbox") }));
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

// After an edit: pages with transaction lists update in place; other pages reload.
async function refreshInPlace() {
  const page = document.body.dataset.page;
  if (page !== "home" && page !== "search") { setTimeout(() => location.reload(), 600); return; }
  try {
    await refreshRows();
    if (page === "home") await swapSections(["coverage", "scheduled-wrap"]);
  } catch (e) { location.reload(); }
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
    input.addEventListener("blur", () => setTimeout(() => this.hide(), 150));
    input.addEventListener("keydown", (ev) => {
      if (this.list().hidden) return;
      if (ev.key === "ArrowDown") { ev.preventDefault(); this.move(1); }
      else if (ev.key === "ArrowUp") { ev.preventDefault(); this.move(-1); }
      else if (ev.key === "Enter" && this.active >= 0) { ev.preventDefault(); this.choose(this.list().children[this.active].textContent); }
      else if (ev.key === "Escape") { ev.preventDefault(); this.hide(); }
    });
    // pointerdown keeps the input focused, so the keyboard stays up on phones
    this.list().addEventListener("pointerdown", (ev) => {
      const li = ev.target.closest("li");
      if (!li) return;
      ev.preventDefault();
      this.choose(li.textContent);
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

// ---------- money fields: digits fill in from the cents, like a register (1500 -> 15.00) ----------

function formatMoneyInput(input) {
  const digits = input.value.replace(/\D/g, "").replace(/^0+/, "").slice(0, 10);
  input.value = digits ? (Number(digits) / 100).toFixed(2) : "";
}

document.addEventListener("input", (ev) => {
  if (ev.target.matches("input.money")) formatMoneyInput(ev.target);
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
      form.scope.value = t.category_is_override || t.name_is_override ? "this" : "merchant";
      const sim = document.getElementById("sheet-similar");
      form.similar.checked = false;
      if (t.similar.length) {
        const n = t.similar.reduce((a, s) => a + s.count, 0);
        document.getElementById("sheet-similar-text").textContent =
          `Also apply to ${t.similar.length} similar merchant${t.similar.length > 1 ? "s" : ""} (${n} transaction${n > 1 ? "s" : ""}), like ${t.similar[0].sample}`;
      }
      this.updateHint();
      document.getElementById("sheet-bank").textContent =
        t.bank_name && t.bank_name !== t.display_name ? `Bank description: ${t.bank_name}` : "";
      suggest.hide();
      openSheet(el);
      this.li = li;
    } catch (e) { toast("Could not open this transaction", true); }
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
    const scopeChanged = (scope === "this") !== wasThis;
    // A one-off name becomes the merchant's name when "All from this merchant" is chosen.
    if (form.merchant.value.trim() !== t.display_name || (scope === "merchant" && t.name_is_override)) {
      body.display_name = form.merchant.value;
    }
    const cid = Number(form.category_id.value);
    if (cid !== t.category_id || scopeChanged) body.category_id = cid;
    if (form.note.value.trim() !== t.note) body.note = form.note.value;
    const bill = form.bill_id.value;
    if (bill !== "new" && bill !== (t.bill_choice === null ? "" : String(t.bill_choice))) body.bill_id = bill;
    if (scope === "merchant" && form.similar.checked) body.similar = t.similar.map((s) => s.key);
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

function bindLoadMore(container) {
  container.addEventListener("click", async (ev) => {
    const btn = ev.target.closest(".load-more");
    if (!btn) return;
    btn.disabled = true;
    btn.textContent = "Loading…";
    try {
      const r = await fetch(`/api/transactions/page?before=${encodeURIComponent(btn.dataset.before)}`, { credentials: "same-origin" });
      const html = await r.text();
      const tmp = document.createElement("div");
      tmp.innerHTML = html;
      btn.replaceWith(...tmp.childNodes);
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
  if (t.matches('#sheet-form input[name="scope"]')) sheet.updateHint();
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
      from_txn: form.dataset.fromTxn || null,
    });
  },
  async "cat-form"(form) {
    await api("/api/categories", { id: form.dataset.id || null, name: form.elements.title.value,
                                   color: colorValue(form) });
  },
  async "acct-form"(form) {
    await api(`/api/accounts/${encodeURIComponent(form.dataset.account)}/rename`, { name: form.elements.title.value });
  },
  async "payday-form"(form) {
    const f = form.elements;
    const kind = f.kind.value;
    await api("/api/paydays", kind === "biweekly"
      ? { kind, anchor: f.anchor.value }
      : { kind, days: f.days.value.split(/[^0-9]+/).filter(Boolean) });
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
    else setTimeout(() => location.reload(), 600);
  } catch (e) { if (btn) btn.disabled = false; toast(e.message, true); }
});

document.addEventListener("click", async (ev) => {
  const del = ev.target.closest(".delete-bill, .delete-cat");
  if (!del) return;
  const isBill = del.classList.contains("delete-bill");
  if (!confirm(isBill ? "Remove this bill?" : "Remove this category? Its transactions go back to their default category.")) return;
  try {
    await api(isBill ? `/api/bills/${del.dataset.id}/delete` : `/api/categories/${del.dataset.id}/delete`);
    closeAllSheets();
    toast("Removed");
    if (isBill) await refreshInPlace();
    else setTimeout(() => location.reload(), 600);
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
        await api("/api/view/account-filter", { account: btn.dataset.account });
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
        await api("/api/view/pending", { on });
        await refresh(["all-head", "all"]);
      } catch (e) { cur.style.opacity = "1"; toast(e.message, true); }
    });
    bindLoadMore(document.body);
  },

  accounts() {
    const on = (id, fn) => { const el = document.getElementById(id); if (el) el.addEventListener("click", fn); };
    on("connect", () => openLink(null));
    on("logout", async (ev) => { ev.preventDefault(); await api("/api/logout"); location.href = "/login"; });
    on("sync", syncNow);
    document.querySelectorAll(".reconnect").forEach((b) =>
      b.addEventListener("click", () => openLink(b.dataset.item)));
  },

  search() {
    bindLoadMore(document.body);
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
      try { await api("/api/links", { links }); location.reload(); } catch (e) { say(e.message, true); }
    });
    const names = document.getElementById("ynab-names");
    if (names) names.addEventListener("click", async () => {
      names.disabled = true;
      try {
        const r = await api("/api/ynab-names");
        toast(`${r.merchants} merchants renamed, ${r.one_off} one-off names, ${r.notes} notes`);
        setTimeout(() => location.reload(), 1200);
      } catch (e) { toast(e.message, true); names.disabled = false; }
    });
  },

  bills() {
    const detect = document.getElementById("detect");
    if (detect) detect.addEventListener("click", async () => {
      detect.classList.add("spin");
      try {
        const r = await api("/api/bills/detect");
        toast(r.added ? `Added ${r.added} bill${r.added > 1 ? "s" : ""}` : "No new recurring charges found");
        if (r.added) setTimeout(() => location.reload(), 900);
      } catch (e) { toast(e.message, true); }
      detect.classList.remove("spin");
    });
  },
};

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
