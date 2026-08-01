import { describe, it, expect, vi } from 'vitest';
import React from 'react';
import { render, screen } from '@testing-library/react';
import { CountdownTimer } from './CountdownTimer';

// Mock icons
vi.mock('@/utils/icons', () => ({
    Hourglass: ({ children, ...p }: React.PropsWithChildren<Record<string, unknown>>) => <span data-testid="icon-hourglass" {...p}>{children}</span>,
    Timer: ({ children, ...p }: React.PropsWithChildren<Record<string, unknown>>) => <span data-testid="icon-timer" {...p}>{children}</span>,
    Check: ({ children, ...p }: React.PropsWithChildren<Record<string, unknown>>) => <span data-testid="icon-check" {...p}>{children}</span>,
}));

const nowMs = Date.now();

describe('CountdownTimer', () => {
    describe('renders nothing when', () => {
        it('status is terminal (succeeded)', () => {
            const { container } = render(
                <CountdownTimer waitBefore={60} waitDelayUntilMs={null} waitDelayStartedMs={null} status="succeeded" serverOffsetMs={0} />
            );
            expect(container.innerHTML).toBe('');
        });

        it('status is terminal (failed)', () => {
            const { container } = render(
                <CountdownTimer waitBefore={60} waitDelayUntilMs={null} waitDelayStartedMs={null} status="failed" serverOffsetMs={0} />
            );
            expect(container.innerHTML).toBe('');
        });

        it('status is stopped', () => {
            const { container } = render(
                <CountdownTimer waitBefore={60} waitDelayUntilMs={null} waitDelayStartedMs={null} status="stopped" serverOffsetMs={0} />
            );
            expect(container.innerHTML).toBe('');
        });

        it('waitBefore is null', () => {
            const { container } = render(
                <CountdownTimer waitBefore={null} waitDelayUntilMs={null} waitDelayStartedMs={null} status="waiting_delay" serverOffsetMs={0} />
            );
            expect(container.innerHTML).toBe('');
        });

        it('waitBefore is 0', () => {
            const { container } = render(
                <CountdownTimer waitBefore={0} waitDelayUntilMs={null} waitDelayStartedMs={null} status="waiting_delay" serverOffsetMs={0} />
            );
            expect(container.innerHTML).toBe('');
        });

        it('waitBefore is negative', () => {
            const { container } = render(
                <CountdownTimer waitBefore={-10} waitDelayUntilMs={null} waitDelayStartedMs={null} status="running" serverOffsetMs={0} />
            );
            expect(container.innerHTML).toBe('');
        });
    });

    describe('deps_ready state (pending)', () => {
        it('shows timer icon and wait duration', () => {
            render(
                <CountdownTimer waitBefore={300} waitDelayUntilMs={null} waitDelayStartedMs={null} status="deps_ready" serverOffsetMs={0} />
            );
            expect(screen.getByTestId('icon-timer')).toBeInTheDocument();
            expect(screen.getByText('Wait Before Start')).toBeInTheDocument();
            expect(screen.getByText('Waiting for delay to start...')).toBeInTheDocument();
        });

        it('displays formatted wait time', () => {
            render(
                <CountdownTimer waitBefore={3600} waitDelayUntilMs={null} waitDelayStartedMs={null} status="deps_ready" serverOffsetMs={0} />
            );
            // 3600s = 1h 0m 0s
            expect(screen.getByText(/1h/)).toBeInTheDocument();
        });
    });

    describe('waiting_delay state (active countdown)', () => {
        it('shows hourglass icon', () => {
            const futureMs = nowMs + 120_000; // 2 minutes from now
            render(
                <CountdownTimer waitBefore={300} waitDelayUntilMs={futureMs} waitDelayStartedMs={nowMs - 180_000} status="waiting_delay" serverOffsetMs={0} />
            );
            expect(screen.getByTestId('icon-hourglass')).toBeInTheDocument();
            expect(screen.getByText('Waiting to Start')).toBeInTheDocument();
        });

        it('shows remaining text', () => {
            const futureMs = nowMs + 60_000;
            render(
                <CountdownTimer waitBefore={120} waitDelayUntilMs={futureMs} waitDelayStartedMs={nowMs - 60_000} status="waiting_delay" serverOffsetMs={0} />
            );
            expect(screen.getByText(/remaining/)).toBeInTheDocument();
        });

        it('shows progress bar', () => {
            const futureMs = nowMs + 60_000;
            const { container } = render(
                <CountdownTimer waitBefore={120} waitDelayUntilMs={futureMs} waitDelayStartedMs={nowMs - 60_000} status="waiting_delay" serverOffsetMs={0} />
            );
            // Progress bar uses ct-progress-fill class
            const progressBar = container.querySelector('.ct-progress-fill');
            expect(progressBar).toBeInTheDocument();
        });

        it('shows "Task will start after countdown"', () => {
            const futureMs = nowMs + 30_000;
            render(
                <CountdownTimer waitBefore={60} waitDelayUntilMs={futureMs} waitDelayStartedMs={nowMs - 30_000} status="waiting_delay" serverOffsetMs={0} />
            );
            expect(screen.getByText('Task will start after countdown')).toBeInTheDocument();
        });
    });

    describe('completed countdown', () => {
        it('shows check icon and "waited" text for terminal status with waitBefore', () => {
            // isCompleted is set when task reached terminal status (e.g. running) after wait
            render(
                <CountdownTimer waitBefore={60} waitDelayUntilMs={null} waitDelayStartedMs={null} status="running" serverOffsetMs={0} />
            );
            expect(screen.getByTestId('icon-check')).toBeInTheDocument();
            expect(screen.getByText(/waited/)).toBeInTheDocument();
            expect(screen.getByText('Wait completed')).toBeInTheDocument();
        });
    });

    describe('serverOffsetMs', () => {
        it('applies server time offset', () => {
            // With 5s offset, a countdown ending 3s in the future (local) is actually 2s away (server-adjusted = local + 5s > end)
            // This just verifies the component doesn't crash with offset
            const futureMs = nowMs + 10_000;
            const { container } = render(
                <CountdownTimer waitBefore={60} waitDelayUntilMs={futureMs} waitDelayStartedMs={nowMs - 50_000} status="waiting_delay" serverOffsetMs={5000} />
            );
            expect(container.innerHTML).not.toBe('');
        });
    });
});
