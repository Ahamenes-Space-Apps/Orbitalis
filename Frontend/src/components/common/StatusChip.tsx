import { Check, Clock3, LoaderCircle, Minus, X } from 'lucide-react';
import type { FieldStatus, JobStatus } from '@/data/types';
import { cn } from '@/lib/utils';

const COPY: Record<FieldStatus, string> = {
  queued: 'Queued',
  running: 'Running',
  completed: 'Completed',
  failed: 'Failed',
  cancelled: 'Cancelled',
};

const TONE: Record<FieldStatus, string> = {
  queued: 'border-line-2 text-mist',
  running: 'border-accent/60 bg-accent-soft text-accent',
  completed: 'border-teal/60 bg-teal/10 text-teal',
  failed: 'border-danger/60 bg-danger/10 text-danger',
  cancelled: 'border-line-2 text-haze',
};

function Icon({ status }: { status: FieldStatus }) {
  const className = 'size-3.5';
  if (status === 'queued') return <Clock3 className={className} aria-hidden="true" />;
  if (status === 'running') return <LoaderCircle className={cn(className, 'animate-spin')} aria-hidden="true" />;
  if (status === 'completed') return <Check className={className} aria-hidden="true" />;
  if (status === 'failed') return <X className={className} aria-hidden="true" />;
  return <Minus className={className} aria-hidden="true" />;
}

/** Text + icon, never colour alone (UX §8.1). */
export function StatusChip({
  status,
  className,
}: {
  status: FieldStatus | JobStatus;
  className?: string;
}) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-none border px-1.5 py-0.5 font-mono text-[10px] font-medium tracking-[0.04em] uppercase whitespace-nowrap',
        TONE[status],
        className,
      )}
      data-testid={`status-${status}`}
    >
      <Icon status={status} />
      {COPY[status]}
    </span>
  );
}
