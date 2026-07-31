import React from 'react';
import { Lock } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { EmptyState } from '@/components/EmptyState';

interface EeFeatureFallbackProps {
    feature: string;
    onHome?: () => void;
}

/**
 * Placeholder shown when a view is not present in this build (ADR #99) — a
 * capability this deployment does not include.
 */
export function EeFeatureFallback({ feature, onHome }: EeFeatureFallbackProps) {
    return (
        <EmptyState
            icon={Lock}
            title={`${feature} isn’t available in this edition.`}
            description="This feature isn’t available in this build."
            action={onHome && (
                <Button size="sm" variant="outline" onClick={onHome}>Go to Pipelines</Button>
            )}
        />
    );
}

export default EeFeatureFallback;
