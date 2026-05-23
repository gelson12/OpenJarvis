import { useEffect, useRef } from 'react';

/*
 * AmbientBackground — a full-window canvas that gives every page a living,
 * futuristic backdrop. Four layers, all low-alpha so content stays readable:
 *   1. a HUD grid — accent-tinted lines, glowing intersection nodes that
 *      twinkle, and energy pulse-rings that radiate outward from centre
 *   2. slow-drifting aurora blobs (accent + purple), additive-blended
 *   3. a faint network of drifting particles with proximity links
 *   4. per-particle twinkle
 *
 * Zero dependencies, single rAF loop. Colours are read live from the
 * OpenJarvis design tokens so it follows the light/dark theme.
 */

function hexToRgb(hex: string): [number, number, number] {
  let h = hex.replace('#', '').trim();
  if (h.length === 3) h = h.split('').map((c) => c + c).join('');
  const n = parseInt(h, 16);
  if (Number.isNaN(n)) return [34, 211, 238];
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

interface Dot {
  x: number;
  y: number;
  vx: number;
  vy: number;
  r: number;
  tw: number; // twinkle phase
}

const CELL = 48; // grid cell size in px
const PULSE_PERIOD = 460; // frames per pulse-ring cycle (~7.5s)
const PULSE_BAND = 80; // px thickness of the energy ring

export function AmbientBackground() {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const cs = getComputedStyle(document.documentElement);
    const accent = hexToRgb(cs.getPropertyValue('--color-accent').trim() || '#22d3ee');
    const purple = hexToRgb(cs.getPropertyValue('--color-accent-purple').trim() || '#b794ff');
    const [ar, ag, ab] = accent;
    const acc = (a: number) => `rgba(${ar},${ag},${ab},${a})`;

    let w = 0;
    let h = 0;
    let dpr = 1;
    const resize = () => {
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = window.innerWidth;
      h = window.innerHeight;
      canvas.width = w * dpr;
      canvas.height = h * dpr;
    };
    resize();
    window.addEventListener('resize', resize);

    // drifting particles
    const DOT_COUNT = reduce ? 26 : 60;
    const dots: Dot[] = [];
    for (let i = 0; i < DOT_COUNT; i++) {
      dots.push({
        x: Math.random() * w,
        y: Math.random() * h,
        vx: (Math.random() - 0.5) * 0.17,
        vy: (Math.random() - 0.5) * 0.17,
        r: 0.6 + Math.random() * 1.7,
        tw: Math.random() * Math.PI * 2,
      });
    }

    // slow lissajous aurora blobs
    const blobs = [
      { fx: 0.2, fy: 0.16, ox: 0.13, oy: 0.1, sx: 0.00026, sy: 0.0002, phase: 0.0, color: accent, rad: 0.62 },
      { fx: 0.82, fy: 0.84, ox: 0.11, oy: 0.13, sx: 0.0002, sy: 0.0003, phase: 1.7, color: purple, rad: 0.66 },
      { fx: 0.72, fy: 0.22, ox: 0.15, oy: 0.1, sx: 0.00032, sy: 0.00023, phase: 3.4, color: accent, rad: 0.46 },
    ];

    const LINK = 132;
    let raf = 0;
    let t = 0;

    const draw = () => {
      t += reduce ? 0 : 1;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);

      // Dispersion origin — defaults to window centre, but VoicePage
      // overrides it to the live orb position so the grid pulse-rings and
      // intersection glow appear to *radiate from the orb itself*.
      const css = getComputedStyle(document.documentElement);
      const ox = parseFloat(css.getPropertyValue('--ambient-origin-x'));
      const oy = parseFloat(css.getPropertyValue('--ambient-origin-y'));
      const cx = Number.isFinite(ox) ? ox : w / 2;
      const cy = Number.isFinite(oy) ? oy : h / 2;
      // maxR from the chosen origin to the farthest screen corner so the
      // pulse always reaches the edges no matter how off-centre we are.
      const maxR = Math.max(
        Math.hypot(cx, cy),
        Math.hypot(w - cx, cy),
        Math.hypot(cx, h - cy),
        Math.hypot(w - cx, h - cy),
      );
      // slow diagonal drift so the whole grid feels alive
      const driftX = ((t * 0.07) % CELL + CELL) % CELL;
      const driftY = ((t * 0.05) % CELL + CELL) % CELL;
      // two pulse-rings, staggered half a cycle apart
      const ringR1 = ((t % PULSE_PERIOD) / PULSE_PERIOD) * maxR;
      const ringR2 = (((t + PULSE_PERIOD / 2) % PULSE_PERIOD) / PULSE_PERIOD) * maxR;
      const ringGlow = (d: number) => {
        if (reduce) return 0;
        const g1 = Math.max(0, 1 - Math.abs(d - ringR1) / PULSE_BAND);
        const g2 = Math.max(0, 1 - Math.abs(d - ringR2) / PULSE_BAND);
        return Math.max(g1, g2);
      };

      // 1a. grid lines — alpha falls off toward the edges (radial mask feel)
      ctx.lineWidth = 1;
      for (let x = driftX % CELL; x <= w; x += CELL) {
        const fade = 1 - Math.min(1, Math.abs(x - cx) / (w * 0.6));
        if (fade <= 0) continue;
        ctx.strokeStyle = acc(0.05 * fade);
        ctx.beginPath();
        ctx.moveTo(x, 0);
        ctx.lineTo(x, h);
        ctx.stroke();
      }
      for (let y = driftY % CELL; y <= h; y += CELL) {
        const fade = 1 - Math.min(1, Math.abs(y - cy) / (h * 0.6));
        if (fade <= 0) continue;
        ctx.strokeStyle = acc(0.05 * fade);
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(w, y);
        ctx.stroke();
      }

      // 1b. glowing intersection nodes — twinkle + energy-ring boost
      for (let x = driftX % CELL; x <= w; x += CELL) {
        for (let y = driftY % CELL; y <= h; y += CELL) {
          const d = Math.hypot(x - cx, y - cy);
          const edge = 1 - Math.min(1, d / (maxR * 0.92));
          if (edge <= 0) continue;
          const twinkle = 0.5 + 0.5 * Math.sin(t * 0.035 + x * 0.05 + y * 0.07);
          const glow = ringGlow(d);
          const a = (0.04 + twinkle * 0.05 + glow * 0.55) * edge;
          const size = 0.7 + glow * 2.2;
          if (glow > 0.15) {
            // bright nodes on the pulse ring get a soft halo
            ctx.fillStyle = acc(a * 0.3);
            ctx.beginPath();
            ctx.arc(x, y, size * 2.6, 0, Math.PI * 2);
            ctx.fill();
          }
          ctx.fillStyle = acc(a);
          ctx.beginPath();
          ctx.arc(x, y, size, 0, Math.PI * 2);
          ctx.fill();
        }
      }

      // 2. aurora blobs (additive)
      ctx.globalCompositeOperation = 'lighter';
      for (const b of blobs) {
        const bx = (b.fx + Math.sin(t * b.sx + b.phase) * b.ox) * w;
        const by = (b.fy + Math.cos(t * b.sy + b.phase) * b.oy) * h;
        const radius = Math.min(w, h) * b.rad;
        const breath = 0.052 + Math.sin(t * 0.004 + b.phase) * 0.022;
        const g = ctx.createRadialGradient(bx, by, 0, bx, by, radius);
        const [r, gr, bl] = b.color;
        g.addColorStop(0, `rgba(${r},${gr},${bl},${Math.max(0, breath)})`);
        g.addColorStop(1, `rgba(${r},${gr},${bl},0)`);
        ctx.fillStyle = g;
        ctx.beginPath();
        ctx.arc(bx, by, radius, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.globalCompositeOperation = 'source-over';

      // advance particles
      for (const dot of dots) {
        dot.x += dot.vx;
        dot.y += dot.vy;
        dot.tw += 0.018;
        if (dot.x < -20) dot.x = w + 20;
        if (dot.x > w + 20) dot.x = -20;
        if (dot.y < -20) dot.y = h + 20;
        if (dot.y > h + 20) dot.y = -20;
      }

      // 3. proximity links
      for (let i = 0; i < dots.length; i++) {
        for (let j = i + 1; j < dots.length; j++) {
          const dx = dots[i].x - dots[j].x;
          const dy = dots[i].y - dots[j].y;
          const dist = Math.hypot(dx, dy);
          if (dist > LINK) continue;
          ctx.strokeStyle = acc((1 - dist / LINK) * 0.1);
          ctx.beginPath();
          ctx.moveTo(dots[i].x, dots[i].y);
          ctx.lineTo(dots[j].x, dots[j].y);
          ctx.stroke();
        }
      }

      // 4. particles with twinkle
      for (const dot of dots) {
        const a = 0.16 + (Math.sin(dot.tw) + 1) * 0.18;
        ctx.fillStyle = acc(a);
        ctx.beginPath();
        ctx.arc(dot.x, dot.y, dot.r, 0, Math.PI * 2);
        ctx.fill();
      }

      raf = requestAnimationFrame(draw);
    };
    draw();

    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener('resize', resize);
    };
  }, []);

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      className="pointer-events-none fixed inset-0 z-0 h-full w-full"
    />
  );
}
