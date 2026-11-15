import * as React from 'react';
import { Slot } from '@radix-ui/react-slot';
import { cva, type VariantProps } from 'class-variance-authority';
import { cn } from '@/lib/utils';

const buttonVariants = cva(
  'inline-flex shrink-0 cursor-pointer items-center justify-center gap-2 whitespace-nowrap rounded-none font-semibold tracking-[0.04em] transition-colors duration-150 disabled:pointer-events-none disabled:opacity-50',
  {
    variants: {
      variant: {
        default:
          'border border-accent bg-accent text-accent-ink hover:border-ink hover:bg-ink hover:text-canvas',
        outline:
          'border border-line-2 bg-panel/60 text-ink backdrop-blur transition-colors hover:border-accent hover:text-accent',
        ghost: 'text-mist hover:bg-panel-2 hover:text-ink',
        pill: 'rounded-full border border-line bg-transparent text-mist hover:text-ink data-[active=true]:border-accent data-[active=true]:bg-accent-soft data-[active=true]:text-accent',
        segment:
          'rounded-none bg-transparent text-haze hover:text-ink data-[active=true]:bg-accent-soft data-[active=true]:text-ink',
      },
      size: {
        default: 'min-h-10 px-4 text-[13px]',
        sm: 'min-h-8 px-2.5 text-[12px]',
        icon: 'size-9',
      },
    },
    defaultVariants: { variant: 'default', size: 'default' },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

export function Button({ className, variant, size, asChild = false, ...props }: ButtonProps) {
  const Comp = asChild ? Slot : 'button';
  return <Comp className={cn(buttonVariants({ variant, size }), className)} {...props} />;
}

export { buttonVariants };
