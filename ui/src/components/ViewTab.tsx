import type { ViewTabProps } from '@/types';

/**
 * ViewTab — a single tab in the view switcher.
 *
 * Lives in its own module (not in Header) so the Team `BackfillNavTab` surface
 * can reuse it without importing back into Header — Header imports `paidSurface`,
 * so an ee → Header import would create a free↔paid module cycle (ADR #99/#104).
 */
export function ViewTab({ active, onClick, icon, label, badge }: ViewTabProps) {
    return (
        <div
            className={`nav-pill nav-pill--md ${active ? 'active' : ''}`}
            onClick={onClick}
            title={label}
            role="tab"
            tabIndex={0}
            aria-selected={active}
            aria-label={badge ? `${label} (${badge} active)` : label}
            onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick(); } }}
        >
            {icon} <span className="hdr-nav-pill-label">{label}</span>
            {badge && badge > 0 ? (
                <span className="nav-pill-badge" aria-hidden="true">{badge}</span>
            ) : null}
        </div>
    );
}
