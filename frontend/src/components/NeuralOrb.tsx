import { useEffect, useRef } from 'react';

/*
 * NeuralOrb — a living particle sphere rendered on a 2D canvas.
 *
 * Zero dependencies: ~150 points on a Fibonacci sphere, rotated every
 * frame and projected to 2D. Near-neighbour links are pre-computed once.
 * The orb reacts to two inputs:
 *   - `state`   drives colour + spin speed (listening/thinking/speaking…)
 *   - `track`   (optional) an audio MediaStreamTrack — its live RMS level
 *               makes the sphere breathe and the core flare when Jarvis speaks.
 */

interface StateLook {
  r: number;
  g: number;
  b: number;
  spin: number; // radians per frame
  swirl: number; // extra per-particle twist
}

const LOOKS: Record<string, StateLook> = {
  listening: { r: 31, g: 213, b: 249, spin: 0.0018, swirl: 0.0 },
  thinking: { r: 170, g: 110, b: 250, spin: 0.0065, swirl: 0.0016 },
  speaking: { r: 34, g: 224, b: 161, spin: 0.0030, swirl: 0.0006 },
  initializing: { r: 130, g: 150, b: 182, spin: 0.0011, swirl: 0.0 },
  disconnected: { r: 108, g: 120, b: 142, spin: 0.0008, swirl: 0.0 },
};

export const PARTICLE_COUNT = 150;
const LINKS_PER_NODE = 2;

/**
 * Live stats written by the active NeuralOrb each frame so external HUD
 * components (e.g. the VoicePage `NODES` readout) can mirror the orb's
 * real state instead of showing a hardcoded number.
 *  - `active` : count of front-facing particles this frame (depth > 0.5)
 *  - `total`  : PARTICLE_COUNT
 *  - `level`  : smoothed mic/output RMS, 0..1
 */
export const orbStats = {
  active: PARTICLE_COUNT,
  total: PARTICLE_COUNT,
  level: 0,
};

interface P3 {
  x: number;
  y: number;
  z: number;
}

/** Evenly distribute N points on a unit sphere (Fibonacci spiral). */
function fibonacciSphere(n: number): P3[] {
  const pts: P3[] = [];
  const golden = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < n; i++) {
    const y = 1 - (i / (n - 1)) * 2;
    const radius = Math.sqrt(1 - y * y);
    const theta = i * golden;
    pts.push({ x: Math.cos(theta) * radius, y, z: Math.sin(theta) * radius });
  }
  return pts;
}

/** Pre-compute each node's nearest neighbours (de-duplicated pairs). */
function buildLinks(pts: P3[]): [number, number][] {
  const seen = new Set<string>();
  const links: [number, number][] = [];
  for (let i = 0; i < pts.length; i++) {
    const dist: { j: number; d: number }[] = [];
    for (let j = 0; j < pts.length; j++) {
      if (i === j) continue;
      const dx = pts[i].x - pts[j].x;
      const dy = pts[i].y - pts[j].y;
      const dz = pts[i].z - pts[j].z;
      dist.push({ j, d: dx * dx + dy * dy + dz * dz });
    }
    dist.sort((a, b) => a.d - b.d);
    for (let k = 0; k < LINKS_PER_NODE; k++) {
      const a = Math.min(i, dist[k].j);
      const b = Math.max(i, dist[k].j);
      const key = `${a}-${b}`;
      if (!seen.has(key)) {
        seen.add(key);
        links.push([a, b]);
      }
    }
  }
  return links;
}

export function NeuralOrb({
  state,
  track,
  className,
}: {
  state: string;
  track?: MediaStreamTrack | null;
  className?: string;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const stateRef = useRef(state);
  const levelRef = useRef(0); // smoothed audio RMS, 0..1
  stateRef.current = state;

  // --- Live audio level from the (optional) speaking track --------------
  useEffect(() => {
    if (!track) {
      levelRef.current = 0;
      return;
    }
    let ctx: AudioContext | null = null;
    let raf = 0;
    try {
      const AC = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      ctx = new AC();
      const src = ctx.createMediaStreamSource(new MediaStream([track]));
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 256;
      src.connect(analyser);
      const buf = new Uint8Array(analyser.fftSize);
      const tick = () => {
        analyser.getByteTimeDomainData(buf);
        let sum = 0;
        for (let i = 0; i < buf.length; i++) {
          const v = (buf[i] - 128) / 128;
          sum += v * v;
        }
        const rms = Math.sqrt(sum / buf.length);
        // ease toward the new level so the orb glides rather than snaps
        levelRef.current += (Math.min(1, rms * 3.2) - levelRef.current) * 0.25;
        raf = requestAnimationFrame(tick);
      };
      tick();
    } catch {
      /* AudioContext unavailable — fall back to state-only animation */
    }
    return () => {
      cancelAnimationFrame(raf);
      ctx?.close().catch(() => {});
    };
  }, [track]);

  // --- The render loop --------------------------------------------------
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const pts = fibonacciSphere(PARTICLE_COUNT);
    const links = buildLinks(pts);
    const cur = { r: 130, g: 150, b: 182 }; // colour, lerped each frame

    let w = 0;
    let h = 0;
    let dpr = 1;
    const resize = () => {
      const rect = canvas.getBoundingClientRect();
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = rect.width;
      h = rect.height;
      canvas.width = w * dpr;
      canvas.height = h * dpr;
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(canvas);

    let spin = 0;
    let swirlPhase = 0;
    let frame = 0;
    let raf = 0;
    const tiltCos = Math.cos(0.42);
    const tiltSin = Math.sin(0.42);
    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    const draw = () => {
      frame++;
      const look = LOOKS[stateRef.current] ?? LOOKS.disconnected;
      const level = levelRef.current;

      // glide colour toward the active state
      cur.r += (look.r - cur.r) * 0.06;
      cur.g += (look.g - cur.g) * 0.06;
      cur.b += (look.b - cur.b) * 0.06;
      const col = (a: number) =>
        `rgba(${cur.r | 0},${cur.g | 0},${cur.b | 0},${a})`;

      spin += reduceMotion ? 0 : look.spin;
      swirlPhase += look.swirl;

      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);

      const cx = w / 2;
      const cy = h / 2;
      const base = Math.min(w, h) * 0.34;
      // breathing: slow idle pulse + live audio flare
      const breath = 1 + Math.sin(frame * 0.03) * 0.03 + level * 0.22;
      const radius = base * breath;

      // project every node for this frame
      const sx = new Float32Array(pts.length);
      const sy = new Float32Array(pts.length);
      const sd = new Float32Array(pts.length); // depth 0..1 (1 = nearest)
      let activeFront = 0;
      for (let i = 0; i < pts.length; i++) {
        const p = pts[i];
        // per-particle swirl twists the sphere as it "thinks"
        const a = spin + swirlPhase * p.y * 12;
        const ca = Math.cos(a);
        const sa = Math.sin(a);
        let x = p.x * ca - p.z * sa;
        let z = p.x * sa + p.z * ca;
        let y = p.y;
        // fixed forward tilt
        const y2 = y * tiltCos - z * tiltSin;
        z = y * tiltSin + z * tiltCos;
        y = y2;
        // particles drift slightly outward with the audio level
        const r = radius * (1 + level * 0.12 * x);
        sx[i] = cx + x * r;
        sy[i] = cy + y * r;
        sd[i] = (z + 1.25) / 2.5;
        if (sd[i] > 0.5) activeFront++;
      }

      // Publish for external readouts (NODES HUD value, etc.)
      orbStats.active = activeFront;
      orbStats.level = level;

      // 1. neighbour links — the "neural" mesh
      ctx.lineWidth = 1;
      for (let k = 0; k < links.length; k++) {
        const [i, j] = links[k];
        const depth = (sd[i] + sd[j]) / 2;
        ctx.strokeStyle = col(depth * depth * (0.42 + level * 0.3));
        ctx.beginPath();
        ctx.moveTo(sx[i], sy[i]);
        ctx.lineTo(sx[j], sy[j]);
        ctx.stroke();
      }

      // 2. core bloom
      const coreR = radius * (0.62 + level * 0.35);
      const glow = ctx.createRadialGradient(cx, cy, 0, cx, cy, coreR);
      glow.addColorStop(0, col(0.55 + level * 0.4));
      glow.addColorStop(0.4, col(0.18));
      glow.addColorStop(1, col(0));
      ctx.fillStyle = glow;
      ctx.beginPath();
      ctx.arc(cx, cy, coreR, 0, Math.PI * 2);
      ctx.fill();

      // 3. particles, painted back-to-front
      const order = Array.from(pts.keys()).sort((a, b) => sd[a] - sd[b]);
      for (const i of order) {
        const d = sd[i];
        const size = (0.7 + d * 2.6) * (1 + level * 0.5);
        // soft halo
        ctx.fillStyle = col(d * 0.16);
        ctx.beginPath();
        ctx.arc(sx[i], sy[i], size * 2.4, 0, Math.PI * 2);
        ctx.fill();
        // bright dot
        ctx.fillStyle = col(0.25 + d * 0.75);
        ctx.beginPath();
        ctx.arc(sx[i], sy[i], size, 0, Math.PI * 2);
        ctx.fill();
      }

      // 4. orbiting reticle rings — HUD framing
      ctx.lineWidth = 1;
      ctx.strokeStyle = col(0.3);
      ctx.setLineDash([3, 9]);
      ctx.beginPath();
      ctx.arc(cx, cy, radius * 1.32, spin * 1.6, spin * 1.6 + Math.PI * 2);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.strokeStyle = col(0.16);
      ctx.beginPath();
      ctx.arc(cx, cy, radius * 1.5, 0, Math.PI * 2);
      ctx.stroke();
      // orbiting tick
      const tick = -spin * 1.6;
      ctx.fillStyle = col(0.85);
      ctx.beginPath();
      ctx.arc(
        cx + Math.cos(tick) * radius * 1.32,
        cy + Math.sin(tick) * radius * 1.32,
        2.4,
        0,
        Math.PI * 2,
      );
      ctx.fill();

      raf = requestAnimationFrame(draw);
    };
    draw();

    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
    };
  }, []);

  return <canvas ref={canvasRef} className={className} aria-hidden="true" />;
}
