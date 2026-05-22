// A live clock + date. The simplest possible widget — proves the
// framework end to end with no external dependencies.

import { useEffect, useState } from 'react';

export function ClockWidget() {
  const [now, setNow] = useState<Date>(() => new Date());

  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);

  const time = now.toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
  const date = now.toLocaleDateString([], {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
  });

  return (
    <div className="flex h-full flex-col items-center justify-center gap-1.5">
      <div className="font-mono text-4xl tracking-[0.1em] text-[#eafaff] tabular-nums">
        {time}
      </div>
      <div className="font-mono text-[11px] tracking-[0.22em] text-[#7fd3e6] uppercase">
        {date}
      </div>
    </div>
  );
}
