import { AlertTriangle } from '@/utils/icons';
import { useEngineHealthQuery } from '@/hooks/queries';

/**
 * Shown while the coordination engine is unhealthy (ADR #130 §9): waiting tasks
 * may not be woken until it is fixed. The problems come from /api/health.
 */
export default function EngineHealthBanner() {
    const { data } = useEngineHealthQuery();
    const problems = data?.problems ?? [];
    if (problems.length === 0) return null;
    return (
        <div className="engine-health-banner" role="alert">
            <AlertTriangle size={16} />
            <div className="engine-health-banner__text">
                <strong>Task coordination needs attention.</strong>
                <ul className="engine-health-banner__problems">
                    {problems.map(problem => <li key={problem}>{problem}</li>)}
                </ul>
            </div>
        </div>
    );
}
