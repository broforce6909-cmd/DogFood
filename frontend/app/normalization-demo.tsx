'use client';

import { useLayoutEffect, useRef, useState } from 'react';

import { AnimatedNumber } from './animated-number';

type Metric = 'raw' | 'normalized';
type Row = { key: string; name: string; track: string; raw: number; normalized: number };

/**
 * "Raw average vs. normalized ranking", made visible instead of only argued
 * in prose. Client-side and self-contained -- no API call, nothing to seed --
 * so it works the same on a fresh clone as it does here.
 *
 * The numbers are not invented for this widget: they are
 * `python scripts/normalization_proof.py`'s real output against our own
 * seeded `raptors-winter` event -- six projects, three judges (one
 * harsh, one generous, one who scored every project a flat 3, JUDGING.md
 * §3's own worked example) -- copied in once, not fetched live, so this
 * stays correct even if the seeded fixture's numbers ever drift; re-run the
 * script and update the table below if they do.
 */
const PROJECTS: Row[] = [
  { key: 'pocketful', name: 'Pocketful', track: 'Civic', raw: 3.956, normalized: 4.023 },
  { key: 'switchyard', name: 'Switchyard', track: 'Logistics', raw: 3.861, normalized: 3.891 },
  { key: 'tidemark', name: 'Tidemark', track: 'Logistics', raw: 3.611, normalized: 3.566 },
  { key: 'foundry', name: 'Foundry', track: 'Civic', raw: 3.556, normalized: 3.591 },
  { key: 'quietline', name: 'Quietline', track: 'Civic', raw: 3.222, normalized: 3.172 },
  { key: 'crosshatch', name: 'Crosshatch', track: 'Logistics', raw: 3.194, normalized: 3.128 },
];

export function NormalizationDemo() {
  const [metric, setMetric] = useState<Metric>('raw');
  const rowEls = useRef(new Map<string, HTMLDivElement>());
  // The FLIP "First" snapshot, taken synchronously in the click handler --
  // before React re-renders into the new order -- and consumed once, in the
  // layout effect that follows that re-render.
  const flipFrom = useRef<Map<string, DOMRect> | null>(null);

  const sorted = [...PROJECTS].sort((a, b) => b[metric] - a[metric]);

  function choose(next: Metric) {
    if (next === metric) return;
    const rects = new Map<string, DOMRect>();
    rowEls.current.forEach((el, key) => rects.set(key, el.getBoundingClientRect()));
    flipFrom.current = rects;
    setMetric(next);
  }

  useLayoutEffect(() => {
    const from = flipFrom.current;
    if (!from) return;
    flipFrom.current = null;
    for (const row of sorted) {
      const el = rowEls.current.get(row.key);
      const start = from.get(row.key);
      if (!el || !start) continue;
      const end = el.getBoundingClientRect();
      const dy = start.top - end.top;
      if (Math.abs(dy) < 0.5) continue;
      el.style.transition = 'none';
      el.style.transform = `translateY(${dy}px)`;
      el.getBoundingClientRect(); // force a reflow before animating away from it
      el.style.transition = 'transform 420ms cubic-bezier(0.2, 0.7, 0.2, 1)';
      el.style.transform = '';
    }
    // `sorted` is derived from `metric` every render; re-running this whenever
    // the derived order changes (not just the raw metric) is the point.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [metric]);

  return (
    <div className="panel norm-demo">
      <div className="row" role="tablist" aria-label="Score view" style={{ marginBottom: 4 }}>
        <button
          type="button"
          role="tab"
          aria-selected={metric === 'raw'}
          className={metric === 'raw' ? 'button primary small' : 'button small'}
          onClick={() => choose('raw')}
        >
          Raw average
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={metric === 'normalized'}
          className={metric === 'normalized' ? 'button primary small' : 'button small'}
          onClick={() => choose('normalized')}
        >
          Normalized
        </button>
      </div>
      <p className="muted small" style={{ marginTop: 8 }}>
        {metric === 'raw'
          ? 'A plain average of every ballot. A harsh judge and a generous judge count equally, whichever range they actually used.'
          : 'Per-judge z-score, shrunk toward the field, rescaled back onto the 1-5 rubric. A judge who marked everything a 3 (sd = 0) contributes no ordering at all.'}
      </p>
      <div className="norm-demo-list">
        {sorted.map((row, index) => (
          <div
            key={row.key}
            ref={(el) => {
              if (el) rowEls.current.set(row.key, el);
              else rowEls.current.delete(row.key);
            }}
            className="norm-demo-row"
          >
            <span className="norm-demo-rank">{index + 1}</span>
            <span className="norm-demo-name">
              {row.name}
              <span className="tag" style={{ marginLeft: 8 }}>
                {row.track}
              </span>
            </span>
            <span className="norm-demo-score">
              <AnimatedNumber value={row[metric]} decimals={3} />
            </span>
          </div>
        ))}
      </div>
      <p className="muted small" style={{ marginTop: 10, marginBottom: 0 }}>
        Real numbers from our own seeded event, <code>raptors-winter</code> — the same run{' '}
        <code>scripts/normalization_proof.py</code> prints. Method and the full worked example:
        JUDGING.md §3.
      </p>
    </div>
  );
}
