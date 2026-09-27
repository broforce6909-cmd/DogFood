'use client';

import { useEffect, useRef } from 'react';
import { useRouter } from 'next/navigation';

/**
 * Keyboard enhancement for the ballot form -- optional, never required. The
 * form underneath still works with no JavaScript at all; this only adds a
 * faster path on top of it, and touches nothing but `.value` on the existing
 * number inputs, so the plain POST and its validation are untouched.
 *
 * Left/right submission navigation is deliberately scoped to "no input is
 * focused" -- a judge editing a score with the cursor mid-number (e.g.
 * "4|.3") needs the browser's native left/right cursor movement, and hijacking
 * it globally would silently discard an unsaved score on a stray keystroke.
 * Up/down and j/k do take over from the number input's native step
 * increment while a score is focused, because criterion-to-criterion
 * navigation is the more valuable behavior there and the numeric value is
 * still fully editable by typing or by the 1-5 quick-pick.
 */
export function KeyboardScoring({
  criterionIds,
  canScore,
  prevHref,
  nextHref,
}: {
  criterionIds: string[];
  canScore: boolean;
  prevHref: string | null;
  nextHref: string | null;
}) {
  const router = useRouter();
  const focusIndex = useRef(0);

  useEffect(() => {
    if (!canScore || criterionIds.length === 0) return undefined;

    function scoreInput(index: number): HTMLInputElement | null {
      const id = criterionIds[index];
      return id ? document.getElementById(`score:${id}`) as HTMLInputElement | null : null;
    }

    function activeScoreIndex(): number {
      const active = document.activeElement;
      if (active instanceof HTMLInputElement && active.id.startsWith('score:')) {
        const index = criterionIds.indexOf(active.id.slice('score:'.length));
        if (index !== -1) return index;
      }
      return focusIndex.current;
    }

    function handleKeyDown(event: KeyboardEvent) {
      const target = event.target;
      const editingText = target instanceof HTMLTextAreaElement;
      if (editingText) return;

      const onScoreInput =
        target instanceof HTMLInputElement && target.id.startsWith('score:');

      if (/^[1-5]$/.test(event.key)) {
        const index = activeScoreIndex();
        const input = scoreInput(index);
        if (input) {
          const min = Number(input.min);
          const max = Number(input.max);
          const picked = Math.min(max, Math.max(min, Number(event.key)));
          input.value = String(picked);
          input.dispatchEvent(new Event('input', { bubbles: true }));
          input.dispatchEvent(new Event('change', { bubbles: true }));
          focusIndex.current = index;
          event.preventDefault();
        }
        return;
      }

      if (event.key === 'ArrowDown' || event.key === 'j') {
        if (!onScoreInput && target instanceof HTMLElement && ['INPUT', 'SELECT'].includes(target.tagName)) {
          return;
        }
        focusIndex.current = Math.min(criterionIds.length - 1, activeScoreIndex() + 1);
        scoreInput(focusIndex.current)?.focus();
        event.preventDefault();
        return;
      }

      if (event.key === 'ArrowUp' || event.key === 'k') {
        if (!onScoreInput && target instanceof HTMLElement && ['INPUT', 'SELECT'].includes(target.tagName)) {
          return;
        }
        focusIndex.current = Math.max(0, activeScoreIndex() - 1);
        scoreInput(focusIndex.current)?.focus();
        event.preventDefault();
        return;
      }

      // Submission navigation only when nothing editable has focus -- see
      // the note above about not clobbering in-place cursor movement.
      const nothingEditableFocused =
        !(target instanceof HTMLElement) ||
        !['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName);
      if (!nothingEditableFocused) return;

      if (event.key === 'ArrowLeft' && prevHref) {
        event.preventDefault();
        router.push(prevHref);
      } else if (event.key === 'ArrowRight' && nextHref) {
        event.preventDefault();
        router.push(nextHref);
      }
    }

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [criterionIds, canScore, prevHref, nextHref, router]);

  if (!canScore || criterionIds.length === 0) return null;

  return (
    <p className="muted" style={{ fontSize: 12, marginTop: -4, marginBottom: 14 }}>
      Keyboard: <kbd>1</kbd>-<kbd>5</kbd> quick-picks a score, <kbd>&uarr;</kbd>/<kbd>&darr;</kbd>{' '}
      (or <kbd>j</kbd>/<kbd>k</kbd>) moves between criteria, <kbd>&larr;</kbd>/<kbd>&rarr;</kbd>{' '}
      jumps to the previous/next ballot when nothing is focused.
    </p>
  );
}
