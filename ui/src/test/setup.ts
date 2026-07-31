// Pin the timezone so date-boundary assertions (Today/Yesterday in
// formatDateTime) are deterministic regardless of the host TZ. Without this,
// tests that fix a UTC "now" near a local midnight boundary pass on UTC CI
// runners but fail on developer machines east of UTC (e.g. UTC+2/+3). Node
// honours a runtime change to process.env.TZ, and setupFiles run in-worker
// before any test module loads, so this takes effect for all Date operations.
process.env.TZ = 'UTC';

import '@testing-library/jest-dom';
import { vi, beforeEach, afterEach } from 'vitest';

// Mock localStorage
const localStorageMock = (() => {
  let store = {};
  return {
    getItem: vi.fn((key) => store[key] || null),
    setItem: vi.fn((key, value) => { store[key] = value; }),
    removeItem: vi.fn((key) => { delete store[key]; }),
    clear: vi.fn(() => { store = {}; }),
  };
})();

Object.defineProperty(window, 'localStorage', { value: localStorageMock });

// Reset mocks before each test
beforeEach(() => {
  vi.clearAllMocks();
  localStorageMock.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
});

// Mock fetch globally
global.fetch = vi.fn();

// jsdom doesn't implement scrollIntoView
Element.prototype.scrollIntoView = vi.fn();

// Suppress console errors in tests (optional)
vi.spyOn(console, 'error').mockImplementation(() => {});
