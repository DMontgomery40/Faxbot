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
