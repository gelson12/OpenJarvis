import { useEffect, useState } from 'react';
import { motion } from 'motion/react';
import { Activity, Cpu, Radio, ShieldCheck } from 'lucide-react';
import { NeuralOrb } from '../NeuralOrb';

/** Live clock for the hero readout. */
function useClock() {
  const [t, setT] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setT(new Date()), 1000);
    return () => clearInterval(id);
  }, []);
  return t;
}

function Vital({
  icon: Icon,
  label,
  value,
  glow,
}: {
  icon: typeof Activity;
  label: string;
  value: string;
  glow?: boolean;
}) {
  return (
    <div className="flex items-center gap-2.5">
      <span
        className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md"
        style={{
          background: 'var(--color-accent-subtle)',
          boxShadow: glow ? '0 0 16px -4px var(--color-accent-glow)' : 'none',
        }}
      >
        <Icon size={14} style={{ color: 'var(--color-accent)' }} />
      </span>
      <div className="flex flex-col">
        <span className="hud-label" style={{ fontSize: '0.5625rem', letterSpacing: '0.2em' }}>
          {label}
        </span>
        <span className="hud-mono text-sm font-semibold" style={{ color: 'var(--color-text)' }}>
          {value}
        </span>
      </div>
    </div>
  );
}

export function DashboardHero() {
  const now = useClock();
  const clock = now.toTimeString().slice(0, 8);
  const stamp = now.toISOString().replace('T', ' ').slice(0, 19) + ' UTC';

  return (
    <motion.div
      className="hud-panel relative mb-4 overflow-hidden p-6"
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4 }}
    >
      {/* drifting scan beam across the hero */}
      <motion.div
        aria-hidden="true"
        className="pointer-events-none absolute inset-x-0 h-px"
        style={{
          background:
            'linear-gradient(90deg, transparent, color-mix(in srgb, var(--color-accent) 55%, transparent), transparent)',
        }}
        animate={{ top: ['10%', '90%', '10%'] }}
        transition={{ duration: 9, repeat: Infinity, ease: 'easeInOut' }}
      />

      <div className="relative flex flex-col items-center gap-6 sm:flex-row">
        {/* live neural core */}
        <div className="relative h-24 w-24 shrink-0">
          <div
            className="absolute inset-0 rounded-full blur-xl"
            style={{ background: 'radial-gradient(circle, var(--color-accent-glow) 0%, transparent 70%)' }}
          />
          <NeuralOrb state="listening" className="absolute inset-0 h-full w-full" />
        </div>

        {/* title block */}
        <div className="min-w-0 flex-1 text-center sm:text-left">
          <div className="flex items-center justify-center gap-2 sm:justify-start">
            <span className="hud-heartbeat" />
            <span className="hud-label" style={{ fontSize: '0.5625rem', letterSpacing: '0.26em' }}>
              OPENJARVIS · ONLINE
            </span>
          </div>
          <h1
            className="hud-mono mt-1 text-2xl font-semibold tracking-[0.06em]"
            style={{ color: 'var(--color-text)' }}
          >
            SYSTEM OVERVIEW
            <span className="hud-caret" />
          </h1>
          <p className="mt-1 text-sm" style={{ color: 'var(--color-text-secondary)' }}>
            Live telemetry for the on-device inference engine — power, throughput, and savings vs. cloud APIs.
          </p>
          <p className="hud-mono mt-1 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            {stamp}
          </p>
        </div>

        {/* vitals */}
        <div className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-1">
          <Vital icon={Radio} label="SYS TIME" value={clock} glow />
          <Vital icon={Cpu} label="ENGINE" value="LOCAL" />
          <Vital icon={Activity} label="STREAM" value="LIVE" />
          <Vital icon={ShieldCheck} label="LINK" value="SECURE" />
        </div>
      </div>
    </motion.div>
  );
}
