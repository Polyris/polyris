import { describe, it, expect, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import React from 'react';
import { RouteProvider, useClientRoute } from './useClientRoute';

const wrapper = ({ children }: { children: React.ReactNode }) => (
    <RouteProvider>{children}</RouteProvider>
);

describe('useClientRoute', () => {
    beforeEach(() => {
        window.history.replaceState(null, '', '/pipelines/');
    });

    it('initializes pathname from window.location', () => {
        window.history.replaceState(null, '', '/tasks/');
        const { result } = renderHook(() => useClientRoute(), { wrapper });
        expect(result.current.pathname).toBe('/tasks/');
    });

    it('push changes the pathname and URL without a reload (query stripped from pathname)', () => {
        const { result } = renderHook(() => useClientRoute(), { wrapper });
        act(() => result.current.push('/runs/?pipeline=x'));
        expect(result.current.pathname).toBe('/runs/');
        expect(window.location.pathname).toBe('/runs/');
        expect(window.location.search).toBe('?pipeline=x');
    });

    it('replace changes the pathname and URL', () => {
        const { result } = renderHook(() => useClientRoute(), { wrapper });
        act(() => result.current.replace('/assets/'));
        expect(result.current.pathname).toBe('/assets/');
        expect(window.location.pathname).toBe('/assets/');
    });

    it('reacts to popstate (browser back/forward)', () => {
        const { result } = renderHook(() => useClientRoute(), { wrapper });
        act(() => {
            window.history.replaceState(null, '', '/tasks/');
            window.dispatchEvent(new PopStateEvent('popstate'));
        });
        expect(result.current.pathname).toBe('/tasks/');
    });
});
