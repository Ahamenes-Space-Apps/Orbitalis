import * as React from 'react';
import * as TabsPrimitive from '@radix-ui/react-tabs';
import { cn } from '@/lib/utils';

export const Tabs = TabsPrimitive.Root;

export function TabsList({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.List>) {
  return <TabsPrimitive.List className={cn('flex flex-wrap gap-1', className)} {...props} />;
}

export function TabsTrigger({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.Trigger>) {
  return (
    <TabsPrimitive.Trigger
      className={cn(
        'min-h-8 cursor-pointer whitespace-nowrap rounded-md border border-line px-2.5 text-[12px] text-mist transition-colors hover:text-ink data-[state=active]:border-accent data-[state=active]:bg-accent-soft data-[state=active]:font-semibold data-[state=active]:text-accent',
        className,
      )}
      {...props}
    />
  );
}
