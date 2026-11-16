// Opening the app or reloading a page always starts on the Inbox. Moving between tabs is untouched,
// and so is a reload the app does itself after a save (it sets finance-keep first).
// Loaded without defer from the head of each tab page, so it runs before anything is drawn.
(() => {
  let fresh = false;
  try {
    fresh = !sessionStorage.getItem("finance-open");
    sessionStorage.setItem("finance-open", "1");
  } catch { /* storage blocked: only the reload check applies */ }
  let keep = false;
  try {
    keep = sessionStorage.getItem("finance-keep") === "1";
    sessionStorage.removeItem("finance-keep");
  } catch { /* ignore */ }
  const nav = performance.getEntriesByType("navigation")[0];
  const reload = nav ? nav.type === "reload" : false;
  if (fresh || (reload && !keep)) {
    // A fresh start forgets the last search (see the Search tab in app.js).
    try { sessionStorage.removeItem("finance-search"); } catch { /* ignore */ }
  }
  if ((fresh || (reload && !keep)) && location.pathname !== "/") location.replace("/");
})();
