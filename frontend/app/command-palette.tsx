'use client';

import type { KeyboardEvent as ReactKeyboardEvent } from 'react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';

import { logoutAction } from './actions';

type PaletteItem = {
  id: string;
  label: string;
  hint: string;
  href?: string;
  run?: () => void;
};

/**
 * Cmd/Ctrl+K quick navigation. The list it searches is not a parallel
 * permission system -- `pages` and `events` arrive already filtered by the
 * server component that renders this (`layout.tsx`, using the same
 * `atLeast(me.role, ...)` checks the visible nav uses, and `events` from
 * `GET /api/events`, which applies the identical `check_access` every direct
 * visit to an event page goes through). This component only searches and
 * navigates; it grants nothing a plain link click could not already reach.
 */
export function CommandPalette({
  pages,
  events,
  canSignOut,
}: {
  pages: { href: string; label: string; hint: string }[];
  events: { slug: string; name: string }[];
  canSignOut: boolean;
}) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  const items = useMemo<PaletteItem[]>(() => {
    const nav: PaletteItem[] = pages.map((p) => ({
      id: `page:${p.href}`,
      label: p.label,
      hint: p.hint,
      href: p.href,
    }));
    const eventItems: PaletteItem[] = events.map((e) => ({
      id: `event:${e.slug}`,
      label: e.name,
      hint: 'Event',
      href: `/events/${e.slug}`,
    }));
    const actions: PaletteItem[] = canSignOut
      ? [
          {
            id: 'action:sign-out',
            label: 'Sign out',
            hint: 'Action',
            run: () => {
              void logoutAction();
            },
          },
        ]
      : [];
    return [...nav, ...eventItems, ...actions];
  }, [pages, events, canSignOut]);

  const results = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter((item) => item.label.toLowerCase().includes(q));
  }, [items, query]);

  useEffect(() => {
    setActiveIndex(0);
  }, [query, open]);

  useEffect(() => {
    function handleGlobalKeyDown(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setOpen((o) => !o);
      } else if (event.key === 'Escape') {
        setOpen(false);
      }
    }
    window.addEventListener('keydown', handleGlobalKeyDown);
    return () => window.removeEventListener('keydown', handleGlobalKeyDown);
  }, []);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  function select(item: PaletteItem) {
    setOpen(false);
    setQuery('');
    if (item.href) router.push(item.href);
    else item.run?.();
  }

  function handleInputKeyDown(event: ReactKeyboardEvent<HTMLInputElement>) {
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setActiveIndex((i) => Math.min(results.length - 1, i + 1));
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      setActiveIndex((i) => Math.max(0, i - 1));
    } else if (event.key === 'Enter') {
      event.preventDefault();
      const item = results[activeIndex];
      if (item) select(item);
    }
  }

  return (
    <>
      <button
        type="button"
        className="command-palette-trigger"
        onClick={() => setOpen(true)}
        aria-label="Open quick navigation"
        title="Quick navigation"
      >
        <kbd>Ctrl</kbd>/<kbd>&#8984;</kbd>+<kbd>K</kbd>
      </button>

      {open && (
        <div className="command-palette-backdrop" onClick={() => setOpen(false)} role="presentation">
          <div
            className="command-palette"
            role="dialog"
            aria-modal="true"
            aria-label="Quick navigation"
            onClick={(event) => event.stopPropagation()}
          >
            <input
              ref={inputRef}
              className="command-palette-input"
              type="text"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={handleInputKeyDown}
              placeholder="Jump to a page, an event, or an action..."
              aria-label="Search"
            />
            <div className="command-palette-results" role="listbox">
              {results.length === 0 ? (
                <p className="command-palette-empty">Nothing matches &ldquo;{query}&rdquo;.</p>
              ) : (
                results.map((item, index) => (
                  <button
                    key={item.id}
                    type="button"
                    role="option"
                    aria-selected={index === activeIndex}
                    data-active={index === activeIndex}
                    className="command-palette-item"
                    onMouseEnter={() => setActiveIndex(index)}
                    onClick={() => select(item)}
                  >
                    <span>{item.label}</span>
                    <span className="command-palette-item-hint">{item.hint}</span>
                  </button>
                ))
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
