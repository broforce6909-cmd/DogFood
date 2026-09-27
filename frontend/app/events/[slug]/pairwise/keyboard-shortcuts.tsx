'use client';

import { useEffect } from 'react';

/**
 * Left/A picks the left project, right/B picks the right one -- a keyboard
 * shortcut layered on the two existing submit buttons. It finds the same
 * `<button name="winner" value="...">` the page already renders and clicks
 * it, so this triggers the exact same server-action POST a mouse click would;
 * there is no separate submission path to keep in sync.
 */
export function PairwiseShortcuts({ leftId, rightId }: { leftId: string; rightId: string }) {
  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      const target = event.target;
      if (target instanceof HTMLElement && ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)) {
        return;
      }

      let id: string | null = null;
      if (event.key === 'ArrowLeft' || event.key === 'a' || event.key === 'A') id = leftId;
      else if (event.key === 'ArrowRight' || event.key === 'b' || event.key === 'B') id = rightId;
      if (!id) return;

      const button = document.querySelector<HTMLButtonElement>(
        `button[name="winner"][value="${id}"]`,
      );
      if (button) {
        event.preventDefault();
        button.click();
      }
    }

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [leftId, rightId]);

  return (
    <p className="muted" style={{ fontSize: 12, textAlign: 'center', marginTop: 10 }}>
      Keyboard: <kbd>&larr;</kbd>/<kbd>A</kbd> picks the left project, <kbd>&rarr;</kbd>/<kbd>B</kbd>{' '}
      picks the right one.
    </p>
  );
}
