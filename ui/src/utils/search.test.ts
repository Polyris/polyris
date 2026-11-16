import { describe, it, expect } from 'vitest';
import { matchesSearch } from './search';

describe('matchesSearch', () => {
    it.each([
        ['orders-clean', 'orders', true],
        ['orders-clean', 'ORDERS clean', true],
        ['orders-clean', 'clean orders', true],
        ['orders-clean', 'orders report', false],
        ['orders-clean', '   ', true],
    ])('%s with %j → %s', (name, search, expected) => {
        expect(matchesSearch(name, search)).toBe(expected);
    });
});
