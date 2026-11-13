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

async function syncNow(ev) {
  ev.target.disabled = true;
  say("Syncing");
  try { await api("/api/sync"); location.reload(); } catch (e) { say(e.message, true); ev.target.disabled = false; }
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
    const go = async () => {
      say("");
      try { await usePasskey(); location.href = "/"; } catch (e) { say(e.message, true); }
    };
    btn.addEventListener("click", go);
  },

  oauth() {
    const state = JSON.parse(localStorage.getItem(LINK_KEY) || "{}");
    if (!state.token) { say("This bank connection expired. Start again from the main page.", true); return; }
    plaidHandler(state.token, { receivedRedirectUri: window.location.href }).open();
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

  accounts() {
    const on = (id, fn) => { const el = document.getElementById(id); if (el) el.addEventListener("click", fn); };
    on("connect", () => openLink(null));
    on("logout", async () => { await api("/api/logout"); location.href = "/login"; });
    on("sync", syncNow);
    document.querySelectorAll(".reconnect").forEach((b) =>
      b.addEventListener("click", () => openLink(b.dataset.item)));
  },

  home() {
    const sync = document.getElementById("sync");
    if (sync) sync.addEventListener("click", syncNow);
    const list = document.getElementById("inbox");
    const count = document.getElementById("inbox-count");
    const refresh = () => {
      const n = list ? list.querySelectorAll("li").length : 0;
      if (count) count.textContent = n;
      if (!n) {
        document.getElementById("inbox-empty").hidden = false;
        const all = document.getElementById("review-all");
        if (all) all.hidden = true;
      }
    };
    document.querySelectorAll(".tick").forEach((btn) =>
      btn.addEventListener("click", async () => {
        const li = btn.closest("li");
        li.classList.add("leaving");
        try {
          await api(`/api/transactions/${btn.dataset.source}/${encodeURIComponent(btn.dataset.id)}/review`, { reviewed: true });
          setTimeout(() => { li.remove(); refresh(); }, 180);
        } catch (e) { li.classList.remove("leaving"); say(e.message, true); }
      }));
    const all = document.getElementById("review-all");
    if (all) all.addEventListener("click", async () => {
      const ids = [...list.querySelectorAll("li")].map((li) => li.dataset.id);
      if (!ids.length || !confirm(`Mark all ${ids.length} as reviewed?`)) return;
      try { await api("/api/inbox/review-all", { ids }); list.innerHTML = ""; refresh(); }
      catch (e) { say(e.message, true); }
    });
  },

  bills() {
    document.querySelectorAll(".bill-form").forEach((form) =>
      form.addEventListener("submit", async (ev) => {
        ev.preventDefault();
        const f = form.elements;
        try {
          await api("/api/bills", {
            id: form.dataset.id || null, name: f.name.value, amount: f.amount.value,
            cadence: f.cadence.value, due_day: f.due_day.value, match: f.match.value,
            next_due: f.next_due.value, active: f.active.checked,
          });
          location.reload();
        } catch (e) { say(e.message, true); window.scrollTo(0, 0); }
      }));
    document.querySelectorAll(".delete-bill").forEach((b) =>
      b.addEventListener("click", async () => {
        if (!confirm("Remove this bill?")) return;
        try { await api(`/api/bills/${b.dataset.id}/delete`); location.reload(); } catch (e) { say(e.message, true); }
      }));
    const detect = document.getElementById("detect");
    if (detect) detect.addEventListener("click", async () => {
      try {
        const r = await api("/api/bills/detect");
        say(r.added ? `Added ${r.added} bill${r.added > 1 ? "s" : ""}.` : "No new recurring charges found.");
        if (r.added) setTimeout(() => location.reload(), 900);
      } catch (e) { say(e.message, true); }
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
      } catch (e) { say(e.message, true); }
    });
  },
};

document.addEventListener("DOMContentLoaded", () => {
  const fn = pages[document.body.dataset.page];
  if (fn) fn();
});
