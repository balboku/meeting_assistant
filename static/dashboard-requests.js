/* Bounded, non-overlapping dashboard reads; never used for mutations. */
(() => {
  'use strict';
  const active = new Set();
  window.DashboardRequests = Object.freeze({
    begin(key, timeoutMs) {
      if (active.has(key)) return null;
      active.add(key);
      const controller = new AbortController();
      let expired = false;
      let finished = false;
      const timer = setTimeout(() => {
        expired = true;
        controller.abort();
      }, timeoutMs);
      return {
        get timedOut() { return expired; },
        async json(url) {
          const response = await fetch(url, { signal: controller.signal });
          if (!response.ok) throw new Error(await response.text());
          // Keep the deadline active until the response body is decoded too.
          return await response.json();
        },
        finish() {
          if (finished) return;
          finished = true;
          clearTimeout(timer);
          controller.abort(); // Cancel a pending peer when Promise.all fails.
          active.delete(key);
        }
      };
    }
  });
})();
