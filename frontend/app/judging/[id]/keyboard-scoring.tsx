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
 *
 * Three rules keep the shortcuts from getting in the way of ordinary typing:
 *
 * - A decimal point typed into a score field switches that field to plain typing
 *   until focus leaves it or a quick-pick happens. Scores are continuous (0.1
 *   steps), and without this the "3" in "4.3" was taken for a quick-pick that
 *   replaced everything typed so far, so the field ended up as 3.
 * - Combinations with Ctrl, Alt or Meta are never ours: Alt+Left is the browser's
 *   Back, Ctrl+K opens the quick-navigation palette, Ctrl+1 switches tabs.
 * - Digits, j and k typed into any *other* input (the palette's search box, for
 *   one) belong to that input.
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
  // Id of the score field a decimal point was just typed into (see the note above).
  const typingDecimalIn = useRef<string | null>(null);

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

    // Moving focus anywhere other than the field a decimal is being typed into
    // ends that decimal: the next digit is a quick-pick again.
    function handleFocusIn(event: FocusEvent) {
      const target = event.target;
      if (!(target instanceof HTMLElement) || target.id !== typingDecimalIn.current) {
        typingDecimalIn.current = null;
      }
    }

    function handleKeyDown(event: KeyboardEvent) {
      // Never ours: Alt+Left (browser Back), Ctrl+K (the palette), Ctrl+1 (tabs)...
      if (event.ctrlKey || event.metaKey || event.altKey) return;

      const target = event.target;
      const editingText = target instanceof HTMLTextAreaElement;
      if (editingText) return;

      // The id of the score field that has focus, if one does.
      const scoreId =
        target instanceof HTMLInputElement && target.id.startsWith('score:') ? target.id : null;
      const onScoreInput = scoreId !== null;

      // Any other input (the palette's search box, say) keeps its own digits.
      if (
        !onScoreInput &&
        target instanceof HTMLElement &&
        (['INPUT', 'SELECT'].includes(target.tagName) || target.isContentEditable)
      ) {
        return;
      }

      // "4." -- from here to the end of the number the browser does the typing.
      if (scoreId !== null && (event.key === '.' || event.key === ',')) {
        typingDecimalIn.current = scoreId;
        return;
      }

      if (/^[1-5]$/.test(event.key)) {
        // The fraction digit of a decimal being typed, not a quick-pick.
        if (scoreId !== null && typingDecimalIn.current === scoreId) return;

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
          typingDecimalIn.current = null;
          event.preventDefault();
        }
        return;
      }

      if (event.key === 'ArrowDown' || event.key === 'j') {
        focusIndex.current = Math.min(criterionIds.length - 1, activeScoreIndex() + 1);
        scoreInput(focusIndex.current)?.focus();
        event.preventDefault();
        return;
      }

      if (event.key === 'ArrowUp' || event.key === 'k') {
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
    window.addEventListener('focusin', handleFocusIn);
    return () => {
      window.removeEventListener('keydown', handleKeyDown);
      window.removeEventListener('focusin', handleFocusIn);
    };
  }, [criterionIds, canScore, prevHref, nextHref, router]);

  if (!canScore || criterionIds.length === 0) return null;

  return (
    <p className="muted" style={{ fontSize: 12, marginTop: -4, marginBottom: 14 }}>
      Keyboard: <kbd>1</kbd>-<kbd>5</kbd> quick-picks a whole score (type <kbd>4.3</kbd> for a
      decimal), <kbd>&uarr;</kbd>/<kbd>&darr;</kbd>{' '}
      (or <kbd>j</kbd>/<kbd>k</kbd>) moves between criteria, <kbd>&larr;</kbd>/<kbd>&rarr;</kbd>{' '}
      jumps to the previous/next ballot when nothing is focused.
    </p>
  );
}
