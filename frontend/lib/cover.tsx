/**
 * A generated cover for a project with no uploaded thumbnail.
 *
 * Deterministic, not random: seeded from the project's own id, so the same
 * project always draws the same cover, on every render and every viewer's
 * screen, rather than reshuffling on refresh. Everything else about it is
 * drawn from the project's actual data rather than decoration for its own
 * sake -- the number of nodes comes from how many tech tags it has, the
 * rotation comes from when it was submitted, and the hue comes from its
 * track -- so two covers differing is always a fact about the two projects,
 * never noise.
 *
 * No new colors: every fill is `var(--accent)`/`var(--fg)`/`var(--surface)`
 * mixed with `color-mix()`, the same tokens (and the same technique) the
 * rest of `globals.css` already uses for `.card:hover` and `.tag.on`. A
 * cover therefore re-themes for light/dark exactly like everything else on
 * the page, for free, and can never drift from the palette.
 *
 * Pure and server-rendered -- no client JS, no image request, nothing to
 * hydrate. `initials()` (lib/format.ts) is reused for the corner mark so a
 * generated cover and the old letter-only placeholder it replaces still
 * share one idea: failing that, at least show the name.
 */

import { initials } from './format';
import type { Track } from './types';

const WIDTH = 400;
const HEIGHT = 250;
const PAD = 30;

/** FNV-1a. Small, fast, good enough dispersion for a decorative seed -- this
 * is not cryptography, it just needs "different strings, different numbers". */
function hashString(value: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < value.length; i += 1) {
    h ^= value.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

/** mulberry32 -- a tiny seeded PRNG. Deterministic for a given seed, which is
 * the one property this needs that `Math.random()` does not have. */
function mulberry32(seed: number): () => number {
  let a = seed;
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function ProjectCover({
  id,
  name,
  track,
  techTags,
  submittedAt,
  className,
}: {
  id: string;
  name: string;
  track: Track | null;
  techTags: string[];
  submittedAt: string | null;
  className?: string;
}) {
  const rand = mulberry32(hashString(id));

  // More tags, more nodes in the mark -- a busier project reads as a busier
  // cover, within a range that still stays legible at thumbnail size.
  const nodeCount = Math.min(9, Math.max(4, 4 + techTags.length));
  const nodes = Array.from({ length: nodeCount }, () => ({
    x: PAD + rand() * (WIDTH - 2 * PAD),
    y: PAD + rand() * (HEIGHT - 2 * PAD),
    r: 3 + rand() * 4,
  }));

  // Each node joins its single nearest neighbour: enough to read as one
  // connected mark (a project's own small "review network"), not a grid.
  const edges: Array<[number, number]> = [];
  for (let i = 0; i < nodes.length; i += 1) {
    let nearest = -1;
    let nearestDist = Infinity;
    for (let j = 0; j < nodes.length; j += 1) {
      if (i === j) continue;
      const dist = Math.hypot(nodes[i].x - nodes[j].x, nodes[i].y - nodes[j].y);
      if (dist < nearestDist) {
        nearestDist = dist;
        nearest = j;
      }
    }
    const already = edges.some(([a, b]) => (a === i && b === nearest) || (a === nearest && b === i));
    if (nearest !== -1 && !already) edges.push([i, nearest]);
  }

  // A track nudges the hue within a narrow arc around the brand accent --
  // enough that two tracks visibly differ, never enough to leave "our own
  // warm accent" and become an arbitrary rainbow. Narrow on purpose: the
  // accent itself sits close to red (hue ~15-20deg), so a wider swing in the
  // negative direction crosses 0deg into magenta -- off-brand, and not a
  // color this app uses anywhere else.
  const hueShift = track ? (hashString(track.key) % 25) - 12 : 0;
  // The submission date turns the whole mark a few degrees -- a project from
  // later in the window sits at a different angle than one from the first day.
  const rotation = hashString(submittedAt ?? id) % 9;

  const gradientId = `cover-grad-${id}`;

  return (
    <svg
      className={className}
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      role="img"
      aria-label={`Generated cover for ${name}`}
      style={hueShift ? { filter: `hue-rotate(${hueShift}deg)` } : undefined}
    >
      <defs>
        <linearGradient id={gradientId} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="color-mix(in srgb, var(--accent) 16%, var(--surface))" />
          <stop offset="100%" stopColor="var(--surface)" />
        </linearGradient>
      </defs>
      <rect width={WIDTH} height={HEIGHT} fill={`url(#${gradientId})`} />
      <g transform={`rotate(${rotation} ${WIDTH / 2} ${HEIGHT / 2})`}>
        {edges.map(([a, b], i) => (
          <line
            key={`e${i}`}
            x1={nodes[a].x}
            y1={nodes[a].y}
            x2={nodes[b].x}
            y2={nodes[b].y}
            stroke="color-mix(in srgb, var(--accent) 40%, transparent)"
            strokeWidth={1}
          />
        ))}
        {nodes.map((n, i) => (
          <circle
            key={`n${i}`}
            cx={n.x}
            cy={n.y}
            r={n.r}
            fill={i % 2 === 0 ? 'color-mix(in srgb, var(--accent) 75%, transparent)' : 'var(--fg)'}
            fillOpacity={i % 2 === 0 ? 1 : 0.22}
          />
        ))}
      </g>
      <text
        x={WIDTH - 18}
        y={HEIGHT - 16}
        textAnchor="end"
        fontSize={78}
        fontWeight={700}
        fill="var(--fg)"
        fillOpacity={0.05}
      >
        {initials(name)}
      </text>
    </svg>
  );
}
