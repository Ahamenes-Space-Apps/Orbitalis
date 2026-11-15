import * as React from 'react';
import { cn } from '@/lib/utils';

export function Card({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        'flex min-w-0 flex-col gap-3 rounded-none border border-line bg-panel p-4 backdrop-blur-sm transition-colors duration-150 hover:border-line-2',
        className,
      )}
      {...props}
    />
  );
}

export function CardTitle({ className, ...props }: React.HTMLAttributes<HTMLHeadingElement>) {
  return <h2 className={cn('m-0 text-[15px] font-semibold', className)} {...props} />;
}

export function CardDescription({ className, ...props }: React.HTMLAttributes<HTMLParagraphElement>) {
  return <p className={cn('m-0 mt-1 text-[12px] text-haze', className)} {...props} />;
}
