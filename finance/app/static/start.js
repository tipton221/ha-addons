// Opening the app or reloading a page always starts on the Inbox. Moving between tabs is untouched.
// Loaded without defer from the head of each tab page, so it runs before anything is drawn.
(() => {
  let fresh = false;
  try {
    fresh = !sessionStorage.getItem("finance-open");
    sessionStorage.setItem("finance-open", "1");
  } catch { /* storage blocked: only the reload check applies */ }
  const nav = performance.getEntriesByType("navigation")[0];
  const reload = nav ? nav.type === "reload" : false;
  if ((fresh || reload) && location.pathname !== "/") location.replace("/");
})();
