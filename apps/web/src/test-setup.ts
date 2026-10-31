import "@testing-library/jest-dom/vitest";
import { configure } from "@testing-library/react";

// findBy*/waitFor give up after 1 s by default. Test files run in parallel workers, and on a
// two-CPU runner a file rendering charts can starve the others past that (seen with the
// transactions test while the Insights tests ran). A longer budget changes no assertion.
configure({ asyncUtilTimeout: 3000 });

// jsdom has no HTMLDialogElement.showModal/close. This shim only toggles `open` so components that
// use <dialog> can be rendered; the focus trap, inert background and Esc handling are browser
// behaviour that unit tests do not prove.
if (typeof HTMLDialogElement !== "undefined" && !("showModal" in HTMLDialogElement.prototype)) {
  Object.assign(HTMLDialogElement.prototype, {
    showModal(this: HTMLDialogElement) {
      this.setAttribute("open", "");
    },
    close(this: HTMLDialogElement) {
      this.removeAttribute("open");
      this.dispatchEvent(new Event("close"));
    },
  });
}

// jsdom has no ResizeObserver. This shim allows chart components (e.g. Recharts ResponsiveContainer)
// to render without throwing in tests.
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

// The router reads the real URL: start every test at "/" so no test depends on another's path.
beforeEach(() => {
  window.history.replaceState(null, "", "/");
});
