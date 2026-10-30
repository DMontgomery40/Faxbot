// Moving to another page moves the keyboard and screen-reader focus to that
// page's heading, so navigation never leaves focus on a control that has
// left the screen. The heading is focusable only by this move (tabIndex -1);
// it does not join the tab order.
export function focusPageHeading(container: HTMLElement | null): boolean {
  const heading = container?.querySelector<HTMLElement>('h1');
  if (!heading) return false;
  if (!heading.hasAttribute('tabindex')) heading.tabIndex = -1;
  heading.focus({ preventScroll: true });
  return true;
}

// How long a page that loads first may take to show its heading.
export const HEADING_WAIT_MS = 10000;

// A page that loads its data first shows its heading only once the data lands,
// so the move waits for the heading a short while. It stops early when the
// person moves focus into the page themselves, so a field they reached first
// is never taken from them. Returns a function that stops waiting (on the
// next page change).
export function focusPageHeadingWhenShown(container: HTMLElement | null, onFocused?: () => void,
  waitMs: number = HEADING_WAIT_MS): () => void {
  if (!container) return () => undefined;
  if (focusPageHeading(container)) {
    onFocused?.();
    return () => undefined;
  }
  const before = document.activeElement;
  const observer = new MutationObserver(() => {
    const now = document.activeElement;
    if (now && now !== before && now !== document.body && container.contains(now)) {
      stop();
      return;
    }
    if (focusPageHeading(container)) {
      stop();
      onFocused?.();
    }
  });
  const timer = window.setTimeout(() => stop(), waitMs);
  function stop() {
    observer.disconnect();
    window.clearTimeout(timer);
  }
  observer.observe(container, { childList: true, subtree: true });
  return stop;
}
