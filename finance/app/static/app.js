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
         t.account, t.pending ? "Pending" : null, t.bill ? `↻ ${t.bill}` : null].filter(Boolean).join(" · ");
      const form = document.getElementById("sheet-form");
      form.display_name.value = t.display_name;
      form.note.value = t.note;
      const sel = form.category_id;
      sel.innerHTML = "";
      for (const c of cats.categories) {
        const o = document.createElement("option");
        o.value = c.id; o.textContent = c.name;
        if (c.id === t.category_id) o.selected = true;
        sel.appendChild(o);
      }
      form.scope.value = t.category_is_override ? "this" : "merchant";
      document.getElementById("sheet-bank").textContent =
        t.bank_name && t.bank_name !== t.display_name ? `Bank description: ${t.bank_name}` : "";
      el.hidden = false;
      requestAnimationFrame(() => {
        el.classList.add("open");
        document.getElementById("scrim").classList.add("open");
      });
      this.li = li;
    } catch (e) { toast("Could not open this transaction", true); }
  },

  close() {
    const el = document.getElementById("sheet");
    el.classList.remove("open");
    document.getElementById("scrim").classList.remove("open");
    setTimeout(() => { if (!el.classList.contains("open")) el.hidden = true; }, 450);
  },

  async save(ev) {
    ev.preventDefault();
    const t = this.current;
    const form = ev.target;
    const body = {};
    if (form.display_name.value.trim() !== t.display_name) body.display_name = form.display_name.value;
    const cid = Number(form.category_id.value);
    const scope = form.scope.value;
    if (cid !== t.category_id || (scope === "this") !== t.category_is_override) {
      body.category_id = cid;
      body.scope = scope;
    }
    if (form.note.value.trim() !== t.note) body.note = form.note.value;
    if (!Object.keys(body).length) { this.close(); return; }
    try {
      await api(`/api/transactions/${t.source}/${encodeURIComponent(t.id)}`, body);
      this.close();
      toast(body.display_name !== undefined || scope === "merchant" && body.category_id !== undefined
        ? "Saved for every transaction from this merchant" : "Saved");
      setTimeout(() => location.reload(), 700);
    } catch (e) { toast(e.message, true); }
  },

  init() {
    const el = document.getElementById("sheet");
    if (!el) return;
    document.addEventListener("click", (ev) => {
      if (ev.target.closest(".tick, .load-more, a, button:not(.txn)")) return;
      const row = ev.target.closest(".txn");
      if (row) this.open(row.closest(".txn-item"));
    });
    document.addEventListener("keydown", (ev) => {
      if ((ev.key === "Enter" || ev.key === " ") && ev.target.classList.contains("txn")) {
        ev.preventDefault();
        this.open(ev.target.closest(".txn-item"));
      }
      if (ev.key === "Escape" && el.classList.contains("open")) this.close();
    });
    document.getElementById("scrim").addEventListener("click", () => this.close());
    document.getElementById("sheet-cancel").addEventListener("click", () => this.close());
    document.getElementById("sheet-form").addEventListener("submit", (ev) => this.save(ev));
    // Swipe the sheet down to close it.
    let startY = null;
    el.addEventListener("touchstart", (e) => { if (el.scrollTop <= 0) startY = e.touches[0].clientY; }, { passive: true });
    el.addEventListener("touchmove", (e) => {
      if (startY === null) return;
      const dy = Math.max(0, e.touches[0].clientY - startY);
      el.style.transition = "none";
      el.style.transform = `translate(-50%, ${dy}px)`;
    }, { passive: true });
    el.addEventListener("touchend", (e) => {
      if (startY === null) return;
      const dy = e.changedTouches[0].clientY - startY;
      el.style.transition = "";
      el.style.transform = "";
      startY = null;
      if (dy > 120) this.close();
    });
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

function bindColorPickers() {
  document.querySelectorAll(".color-picker").forEach((box) => {
    const row = box.querySelector(".hex-row");
    const picker = box.querySelector('input[name="custom_picker"]');
    const hex = box.querySelector('input[name="custom_hex"]');
    box.querySelectorAll('input[name="color"]').forEach((r) =>
      r.addEventListener("change", () => { row.hidden = r.value !== "custom" || !r.checked; }));
    picker.addEventListener("input", () => { hex.value = picker.value.toUpperCase(); });
    hex.addEventListener("input", () => {
      const v = hex.value.trim().replace(/^#?/, "#");
      if (/^#[0-9a-fA-F]{6}$/.test(v)) picker.value = v.toLowerCase();
    });
  });
}

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
    const list = document.getElementById("inbox");
    const count = document.getElementById("inbox-count");
    const refresh = () => {
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
    list.addEventListener("click", async (ev) => {
      const btn = ev.target.closest(".tick");
      if (!btn) return;
      ev.stopPropagation();
      const li = btn.closest(".txn-item");
      btn.classList.add("checked");
      btn.disabled = true;
      try {
        await api(`/api/transactions/${li.dataset.source}/${encodeURIComponent(li.dataset.id)}/review`, { reviewed: true });
        await fold(li, 250);
        refresh();
      } catch (e) { btn.classList.remove("checked"); btn.disabled = false; toast(e.message, true); }
    });
    const all = document.getElementById("review-all");
    if (all) all.addEventListener("click", async () => {
      const items = [...list.querySelectorAll(".txn-item")];
      if (!items.length || !confirm(`Mark all ${items.length} as reviewed?`)) return;
      all.disabled = true;
      try {
        await api("/api/inbox/review-all", { ids: items.map((li) => li.dataset.id) });
        items.forEach((li, i) => li.querySelector(".tick")?.classList.add("checked"));
        await Promise.all(items.map((li, i) => fold(li, 120 + Math.min(i, 20) * 45)));
        refresh();
        toast(`${items.length} reviewed`);
        setTimeout(() => location.reload(), 900);
      } catch (e) { all.disabled = false; toast(e.message, true); }
    });
    const filters = document.getElementById("filters");
    if (filters) filters.addEventListener("click", async (ev) => {
      const btn = ev.target.closest(".filter");
      if (!btn || btn.classList.contains("on")) return;
      filters.querySelectorAll(".filter").forEach((b) => {
        const on = b === btn;
        b.classList.toggle("on", on);
        b.setAttribute("aria-checked", on ? "true" : "false");
      });
      const cur = document.getElementById("all");
      cur.style.transition = "opacity .15s";
      cur.style.opacity = "0.25";
      try {
        await api("/api/view/account-filter", { account: btn.dataset.account });
        const r = await fetch("/", { credentials: "same-origin" });
        const doc = new DOMParser().parseFromString(await r.text(), "text/html");
        const fresh = doc.getElementById("all");
        cur.replaceWith(fresh);
        fresh.animate([{ opacity: 0, transform: "translateY(6px)" }, { opacity: 1, transform: "none" }],
                      { duration: 260, easing: "cubic-bezier(.2,.8,.2,1)" });
      } catch (e) { cur.style.opacity = "1"; toast(e.message, true); }
    });
    bindLoadMore(document.body);
  },

  accounts() {
    bindColorPickers();
    const on = (id, fn) => { const el = document.getElementById(id); if (el) el.addEventListener("click", fn); };
    on("connect", () => openLink(null));
    on("logout", async (ev) => { ev.preventDefault(); await api("/api/logout"); location.href = "/login"; });
    on("sync", syncNow);
    document.querySelectorAll(".reconnect").forEach((b) =>
      b.addEventListener("click", () => openLink(b.dataset.item)));
    document.querySelectorAll("form.rename").forEach((form) => {
      const input = form.elements.name;
      const save = async () => {
        if (input.value.trim() === input.dataset.original) return;
        try {
          await api(`/api/accounts/${encodeURIComponent(form.dataset.account)}/rename`, { name: input.value });
          input.dataset.original = input.value.trim();
          toast("Renamed");
        } catch (e) { toast(e.message, true); }
      };
      form.addEventListener("submit", (ev) => { ev.preventDefault(); input.blur(); });
      input.addEventListener("blur", save);
    });
    document.querySelectorAll(".cat-form").forEach((form) =>
      form.addEventListener("submit", async (ev) => {
        ev.preventDefault();
        try {
          await api("/api/categories", { id: form.dataset.id || null, name: form.elements.name.value,
                                         color: colorValue(form) });
          location.reload();
        } catch (e) { toast(e.message, true); }
      }));
    document.querySelectorAll(".delete-cat").forEach((b) =>
      b.addEventListener("click", async () => {
        if (!confirm("Remove this category? Its transactions go back to their default category.")) return;
        try { await api(`/api/categories/${b.dataset.id}/delete`); location.reload(); } catch (e) { toast(e.message, true); }
      }));
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
  },

  bills() {
    bindColorPickers();
    document.querySelectorAll(".bill-form").forEach((form) => {
      const varies = form.elements.varies;
      const amount = form.elements.amount;
      varies.addEventListener("change", () => {
        amount.disabled = varies.checked;
        amount.placeholder = varies.checked ? "Average of recent charges" : "";
      });
    });
    document.querySelectorAll(".bill-form").forEach((form) =>
      form.addEventListener("submit", async (ev) => {
        ev.preventDefault();
        const f = form.elements;
        try {
          await api("/api/bills", {
            id: form.dataset.id || null, name: f.name.value, amount: f.amount.value,
            cadence: f.cadence.value, due_day: f.due_day.value, match: f.match.value,
            next_due: f.next_due.value, active: f.active.checked, color: colorValue(form),
            varies: f.varies.checked,
          });
          location.reload();
        } catch (e) { toast(e.message, true); }
      }));
    document.querySelectorAll(".delete-bill").forEach((b) =>
      b.addEventListener("click", async () => {
        if (!confirm("Remove this bill?")) return;
        try { await api(`/api/bills/${b.dataset.id}/delete`); location.reload(); } catch (e) { toast(e.message, true); }
      }));
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
    const pay = document.getElementById("payday-form");
    if (pay) pay.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const f = pay.elements;
      const kind = f.kind.value;
      try {
        await api("/api/paydays", kind === "biweekly"
          ? { kind, anchor: f.anchor.value }
          : { kind, days: f.days.value.split(/[^0-9]+/).filter(Boolean) });
        location.reload();
      } catch (e) { toast(e.message, true); }
    });
  },
};

document.addEventListener("DOMContentLoaded", () => {
  sheet.init();
  const fn = pages[document.body.dataset.page];
  if (fn) fn();
});
