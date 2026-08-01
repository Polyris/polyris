import React from 'react';
import type { LucideIcon } from 'lucide-react';

export interface EmptyStateProps {
    /** Optional icon shown in a soft circle above the text. */
    icon?: LucideIcon;
    /** Primary line — what's happening, named from the reader's side of the screen. */
    title: string;
    /** Optional secondary line with supporting detail. */
    description?: string;
    /** Optional action slot, e.g. a <Button>. */
    action?: React.ReactNode;
    /** ARIA live-region role. 'status' (default) for informational states. */
    role?: 'status' | 'alert';
    /** Visual tone. 'neutral' (default) for information; 'warning'/'error' tint
     *  the icon for degraded or failed states. */
    tone?: 'neutral' | 'warning' | 'error';
}

/**
 * Centered placeholder panel for a region that has nothing to render yet — a
 * feature that's coming, one gated to a paid tier, or otherwise unavailable.
 *
 * Purely presentational and tier-agnostic: the caller supplies the icon, copy,
 * and action, so the same panel is reusable in both free and paid builds. Style
 * lives in `.empty-state` (styles/modules/_enhanced-ui.css) and themes via the
 * app's CSS variables, so it tracks light/dark automatically.
 */
export function EmptyState({ icon: Icon, title, description, action, role = 'status', tone = 'neutral' }: EmptyStateProps) {
    const toneClass = tone !== 'neutral' ? ` empty-state--${tone}` : '';
    return (
        <div className={`empty-state${toneClass}`} role={role}>
            {Icon && (
                <div className="empty-state__icon" aria-hidden="true">
                    <Icon />
                </div>
            )}
            <div className="empty-state__text">
                <p className="empty-state__title">{title}</p>
                {description && <p className="empty-state__description">{description}</p>}
            </div>
            {action && <div className="empty-state__action">{action}</div>}
        </div>
    );
}

export default EmptyState;
