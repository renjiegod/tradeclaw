import { useEffect, useRef, useState } from "react";

// "Waiting for the agent" cue: a pulsing asterisk glyph next to a shimmering
// status label, replacing antd's generic <Spin>. Glyph ramp advances on a
// 120ms cadence; the shimmer sweep is a background-clip gradient (see
// .assistant-thinking rules in styles.css).
const RAMP = ["·", "✢", "✳", "✶", "✻", "✽"];
const FRAMES = [...RAMP, ...[...RAMP].reverse()];
const FRAME_MS = 120;
const STILL_GLYPH = "✽";

function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return false;
  try {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}

export type ThinkingSpinnerProps = {
  /** Live status phrase, e.g. "Agent 思考中…". */
  label: string;
  /** Extra classes on the root (callers add spacing/variant hooks). */
  className?: string;
};

export function ThinkingSpinner({ label, className }: ThinkingSpinnerProps) {
  const [frame, setFrame] = useState(0);
  const reduced = useRef(prefersReducedMotion());

  useEffect(() => {
    if (reduced.current) return;
    const id = setInterval(() => setFrame((f) => (f + 1) % FRAMES.length), FRAME_MS);
    return () => clearInterval(id);
  }, []);

  const glyph = reduced.current ? STILL_GLYPH : FRAMES[frame];

  return (
    <span
      className={`assistant-thinking${className ? ` ${className}` : ""}`}
      role="status"
      aria-label={label}
    >
      <span className="assistant-thinking__glyph" aria-hidden="true">
        {glyph}
      </span>
      <span className="assistant-thinking__label" aria-hidden="true">
        {label}
      </span>
    </span>
  );
}
