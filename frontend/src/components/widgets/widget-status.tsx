// Centred status / empty-state line shared by the data widgets.

export function WidgetStatus({ text }: { text: string }) {
  return (
    <div className="flex h-full items-center justify-center px-5 text-center text-xs text-[#7fd3e6]">
      {text}
    </div>
  );
}
