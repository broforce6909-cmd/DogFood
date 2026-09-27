'use client';

import { useEffect, useRef, useState } from 'react';

/**
 * A number that tweens to a new value instead of jumping to it -- a count-up,
 * not a flip/odometer character animation, which is a much larger component
 * for the same "this changed" signal at thumbnail scale.
 *
 * Deliberately plain: `requestAnimationFrame` and an eased interpolation, no
 * animation library (this frontend has none, on purpose -- see globals.css).
 * The first render shows the value immediately, with nothing to animate from;
 * only a *change* to an already-mounted instance tweens, which is exactly the
 * case worth animating (see `NormalizationDemo`, where toggling the view is
 * a real, client-side value change) and not one this app currently forces
 * server-rendered pages to fake on every load.
 */
export function AnimatedNumber({
  value,
  decimals = 0,
  duration = 450,
}: {
  value: number;
  decimals?: number;
  duration?: number;
}) {
  const [display, setDisplay] = useState(value);
  const previous = useRef(value);
  const frame = useRef<number | undefined>(undefined);

  useEffect(() => {
    const from = previous.current;
    const to = value;
    previous.current = value;
    if (from === to) return undefined;

    const reduceMotion =
      typeof window !== 'undefined' &&
      window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    if (reduceMotion) {
      setDisplay(to);
      return undefined;
    }

    const start = performance.now();
    const step = (now: number) => {
      const t = Math.min(1, (now - start) / duration);
      const eased = 1 - (1 - t) ** 3; // ease-out cubic
      setDisplay(from + (to - from) * eased);
      if (t < 1) frame.current = requestAnimationFrame(step);
    };
    frame.current = requestAnimationFrame(step);
    return () => {
      if (frame.current !== undefined) cancelAnimationFrame(frame.current);
    };
  }, [value, duration]);

  return <span className="tabular">{display.toFixed(decimals)}</span>;
}
