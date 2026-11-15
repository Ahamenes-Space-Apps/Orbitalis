import * as React from 'react';
import { cva, type VariantProps } from 'class-variance-authority';
import { cn } from '@/lib/utils';

const badgeVariants = cva(
  'inline-flex items-center rounded-none border px-1.5 py-0.5 font-mono text-[10px] font-medium tracking-[0.04em] uppercase whitespace-nowrap',
  {
    variants: {
      tone: {
        neutral: 'border-line-2 text-mist',
        amber: 'border-accent/60 bg-accent-soft font-semibold text-accent',
        violet: 'border-violet/50 bg-violet/10 font-semibold text-violet',
        teal: 'border-teal/50 text-teal',
        quiet: 'border-line text-haze',
      },
    },
    defaultVariants: { tone: 'neutral' },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

export function Badge({ className, tone, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ tone }), className)} {...props} />;
}
