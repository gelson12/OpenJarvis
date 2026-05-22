// The live voice transcript as a floating, draggable panel — built on
// OpenJarvis's LiveKit transcription stream.

import { useLocalParticipant, useTranscriptions } from '@livekit/components-react';

export function ChatWidget() {
  const segments = useTranscriptions();
  const { localParticipant } = useLocalParticipant();

  if (segments.length === 0) {
    return (
      <div className="flex h-full items-center justify-center px-5 text-center text-xs text-[#7fd3e6]">
        No conversation yet — say something.
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2 p-3">
      {segments.map((seg, i) => {
        const mine = seg.participantInfo?.identity === localParticipant?.identity;
        return (
          <div key={i} className={`flex ${mine ? 'justify-end' : 'justify-start'}`}>
            <div
              className={`max-w-[85%] rounded-xl px-3 py-1.5 text-xs ${
                mine ? 'bg-[#3CDFFF]/20 text-[#eafaff]' : 'bg-[#3CDFFF]/8 text-[#cdf2fb]'
              }`}
            >
              <div className="mb-0.5 font-mono text-[9px] tracking-widest text-[#5fb0c6] uppercase">
                {mine ? 'You' : 'Jarvis'}
              </div>
              {seg.text}
            </div>
          </div>
        );
      })}
    </div>
  );
}
