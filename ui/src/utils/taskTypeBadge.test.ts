import { describe, it, expect } from 'vitest';
import { taskTypeBadge } from './taskTypeBadge';

describe('taskTypeBadge', () => {
    it('returns AWS brand colours by category', () => {
        expect(taskTypeBadge('lambda')).toEqual({ label: 'LAMBDA', color: '#ED7100' }); // compute
        expect(taskTypeBadge('glue')).toEqual({ label: 'GLUE', color: '#8C4FFF' });     // analytics
        expect(taskTypeBadge('sfn')).toEqual({ label: 'SFN', color: '#E7157B' });       // app-integration
    });

    it('is case-insensitive', () => {
        expect(taskTypeBadge('LAMBDA')?.label).toBe('LAMBDA');
        expect(taskTypeBadge('Ecs')?.color).toBe('#ED7100');
    });

    it('returns null for unknown or absent types', () => {
        expect(taskTypeBadge('python')).toBeNull();
        expect(taskTypeBadge(null)).toBeNull();
        expect(taskTypeBadge(undefined)).toBeNull();
        expect(taskTypeBadge('')).toBeNull();
    });
});
