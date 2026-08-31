// fast-browser-mcp bootstrap: installed via Page.addScriptToEvaluateOnNewDocument
// so it re-runs on EVERY new document (full reload, cross-origin nav) before any
// page script executes. Provides window.__fbm as shared infrastructure for:
//   - detecting a full reload vs. an in-page SPA transition (epoch counter)
//   - a MutationObserver ring buffer so act_and_observe/popup-resolution don't
//     need to install their own observer at action time (which would miss any
//     mutation that lands before the observer is wired up)
(function () {
  if (window.__fbm && window.__fbm.__coreInstalled) return;

  var MAX_MUTATIONS = 500;
  var EPOCH_KEY = "__fbm_epoch";

  // A full reload gives us a brand-new `window` — there is no in-memory way
  // to tell "did the document just get torn down and recreated" from the new
  // document's own execution. localStorage is the one thing that survives a
  // same-origin reload, so the epoch counter round-trips through it instead
  // of through window state. (Cross-origin nav lands on a different storage
  // bucket entirely, which is fine: the counter resetting to 1 there is
  // itself a correct signal that this is a different document.)
  var storedEpoch = 0;
  try {
    storedEpoch = parseInt(localStorage.getItem(EPOCH_KEY) || "0", 10) || 0;
  } catch (e) {
    // localStorage blocked (sandboxed iframe, strict privacy mode) — epoch
    // tracking degrades to "always looks reloaded", which is the safe default.
  }

  var fbm = {
    __coreInstalled: true,
    epoch: storedEpoch + 1,
    installedAt: Date.now(),
    mutations: [],
    lastInsertedContainer: null,
  };
  window.__fbm = fbm;

  try {
    localStorage.setItem(EPOCH_KEY, String(fbm.epoch));
  } catch (e) {
    // see above
  }

  function describe(node) {
    if (!node || node.nodeType !== 1) return null;
    var role = node.getAttribute ? node.getAttribute("role") : null;
    return {
      tag: node.tagName ? node.tagName.toLowerCase() : null,
      role: role,
      id: node.id || null,
      text: (node.textContent || "").trim().slice(0, 80),
    };
  }

  function recordMutation(kind, node) {
    if (!node || node.nodeType !== 1) return;
    fbm.mutations.push({ t: Date.now(), kind: kind, node: describe(node) });
    if (fbm.mutations.length > MAX_MUTATIONS) fbm.mutations.shift();
  }

  function startObserver() {
    if (!document.documentElement) {
      // documentElement isn't parsed yet this early in the load — retry on
      // the next microtask/frame until it exists.
      requestAnimationFrame(startObserver);
      return;
    }
    var observer = new MutationObserver(function (records) {
      for (var i = 0; i < records.length; i++) {
        var rec = records[i];
        if (rec.type === "childList") {
          rec.addedNodes.forEach(function (n) {
            recordMutation("added", n);
            // Track the most recently inserted element with popup-ish
            // semantics as a last-resort signal for "which container just
            // opened" when there is no aria-controls/aria-expanded link.
            if (
              n.nodeType === 1 &&
              /^(listbox|menu|dialog|tooltip)$/i.test(n.getAttribute && n.getAttribute("role") || "")
            ) {
              fbm.lastInsertedContainer = n;
            }
          });
          rec.removedNodes.forEach(function (n) {
            recordMutation("removed", n);
          });
        } else if (rec.type === "attributes") {
          recordMutation("attr:" + rec.attributeName, rec.target);
        }
      }
    });
    observer.observe(document.documentElement, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["aria-expanded", "aria-hidden", "class", "style"],
    });
    fbm._observer = observer;
  }

  startObserver();
})();
