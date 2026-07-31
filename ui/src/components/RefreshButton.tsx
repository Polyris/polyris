import React from 'react';
import { RefreshCw } from '../utils/icons';
import { Button } from '@/components/ui/button';

interface RefreshButtonProps {
    onRefresh: () => void;
    /** Spins the icon and disables the button while true. */
    isFetching?: boolean;
    /** Optional text after the icon; omit for an icon-only button. */
    label?: string;
    /** Button size, forwarded to the underlying Button. */
    size?: React.ComponentProps<typeof Button>['size'];
    /** Button variant, forwarded to the underlying Button (default 'secondary'). */
    variant?: React.ComponentProps<typeof Button>['variant'];
    /** Icon pixel size (default 16). */
    iconSize?: number;
    /** Tooltip + accessible name (default 'Refresh'). Use for domain-specific
     *  affordances, e.g. 'Re-check now'. */
    title?: string;
    /** Extra classes forwarded to the underlying Button (sizing overrides, etc.). */
    className?: string;
}

/**
 * Shared refresh control. Single source of truth for refresh affordance so every
 * surface (History toolbar, pipeline canvas, …) spins the icon and disables while a
 * fetch is in flight, rather than each re-implementing it.
 */
export function RefreshButton({
    onRefresh, isFetching = false, label, size, variant = 'secondary', iconSize = 16,
    title = 'Refresh', className,
}: RefreshButtonProps) {
    return (
        <Button
            size={size}
            variant={variant}
            className={className}
            onClick={onRefresh}
            disabled={isFetching}
            title={title}
            aria-label={title}
        >
            <RefreshCw size={iconSize} className={isFetching ? 'animate-spin' : ''} />
            {label ? <> {label}</> : null}
        </Button>
    );
}
