import type { ReactNode } from 'react';
import { cn } from '@/lib/utils';

export function Skeleton({ className }: { className?: string }) {
  return <div aria-hidden="true" className={cn('animate-pulse rounded-none bg-panel-2', className)} />;
}

export function LoadingBlock({ label, className }: { label: string; className?: string }) {
  return (
    <div role="status" className={cn('flex flex-col gap-3', className)}>
      <p className="m-0 text-sm text-haze">{label}</p>
      <Skeleton className="h-24 w-full" />
    </div>
  );
}

export function Stat({
  label,
  value,
  hint,
  className,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        'rounded-none border border-line bg-panel px-3.5 py-3 backdrop-blur-sm',
        className,
      )}
    >
      <div className="font-mono text-[10px] font-medium tracking-[0.16em] text-haze uppercase">
        {label}
      </div>
      <div className="mt-1 font-mono text-[19px] leading-tight text-ink">{value}</div>
      {hint && <div className="mt-1 text-[11px] leading-snug text-mist">{hint}</div>}
    </div>
  );
}

export function SectionHeading({
  eyebrow,
  title,
  description,
}: {
  eyebrow?: string;
  title: string;
  description?: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      {eyebrow && (
        <div className="font-mono text-[10px] font-semibold tracking-[0.18em] text-accent uppercase">
          {eyebrow}
        </div>
      )}
      <h1 className="m-0 text-2xl font-bold tracking-tight sm:text-[34px] sm:leading-[0.95]">
        {title}
      </h1>
      {description && (
        <p className="m-0 max-w-3xl text-[13px] leading-relaxed text-mist">{description}</p>
      )}
    </div>
  );
}
