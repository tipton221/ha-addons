"use strict";
// Finance service worker: shows a notification for each push (Safari requires one) and sets the
// app icon badge. Nothing is cached.

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let msg = {};
  try { msg = event.data ? event.data.json() : {}; } catch { msg = { body: event.data && event.data.text() }; }
  const badge = Number(msg.badge) || 0;
  const setBadge = "setAppBadge" in navigator
    ? (badge ? navigator.setAppBadge(badge) : navigator.clearAppBadge()).catch(() => {})
    : Promise.resolve();
  event.waitUntil(Promise.all([
    setBadge,
    // Every notification shares one tag, so a newer one replaces the last instead of piling up.
    // "All caught up" asks to arrive silently (whether a banner still shows is up to the system).
    self.registration.showNotification(msg.title || "Finance", {
      body: msg.body || "", icon: "/static/icons/icon-192.png", tag: "finance", data: { url: msg.url || "/" },
      silent: !!msg.quiet, renotify: !msg.quiet,
    }),
  ]));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const c of all) {
      if ("focus" in c) { await c.focus(); if ("navigate" in c) await c.navigate(url); return; }
    }
    await self.clients.openWindow(url);
  })());
});
