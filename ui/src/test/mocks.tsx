/**
 * Component Mocks - Centralized mock setup for UI component tests.
 * 
 * Usage in test files:
 *   import '../test/mocks';
 * 
 * This automatically mocks heavy dependencies (icons, BaseModal, etc.)
 * so component tests focus on behavior, not rendering SVGs.
 */
import { vi } from 'vitest';

// ─── Icon mocks (avoid importing all of lucide-react) ────────────────────────

const createIconMock = (name) => {
    const Component = ({ size, className, ...props }) => (
        <span data-testid={`icon-${name}`} data-size={size} className={className} {...props} />
    );
    Component.displayName = name;
    return Component;
};

// Mock the icons module wholesale
vi.mock('../utils/icons', () => {
    return new Proxy({}, {
        get: (target, prop) => {
            if (prop === '__esModule') return true;
            if (prop === 'default') return {};
            return createIconMock(prop);
        }
    });
});

vi.mock('@/utils/icons', () => {
    return new Proxy({}, {
        get: (target, prop) => {
            if (prop === '__esModule') return true;
            if (prop === 'default') return {};
            return createIconMock(prop);
        }
    });
});

vi.mock('lucide-react', () => {
    return new Proxy({}, {
        get: (target, prop) => {
            if (prop === '__esModule') return true;
            return createIconMock(prop);
        }
    });
});

// ─── shadcn/ui mocks ─────────────────────────────────────────────────────────

vi.mock('@/components/ui/button', () => ({
    Button: ({ children, onClick, variant, size, className, disabled, title, ...props }) => (
        <button 
            onClick={onClick} 
            disabled={disabled} 
            className={className} 
            title={title}
            data-variant={variant} 
            data-size={size}
            {...props}
        >
            {children}
        </button>
    ),
}));

// ─── BaseModal mock (renders children without portal/animation) ──────────────

vi.mock('../components/BaseModal', () => ({
    BaseModal: ({ isOpen, onClose: _onClose, children, className }) => {
        if (!isOpen) return null;
        return (
            <div data-testid="base-modal" className={className} role="dialog">
                {children}
            </div>
        );
    },
    ModalHeader: ({ children, icon, onClose }) => (
        <div data-testid="modal-header">
            {icon}
            <span>{children}</span>
            {onClose && <button data-testid="modal-close" onClick={onClose}>×</button>}
        </div>
    ),
    ModalBody: ({ children }) => <div data-testid="modal-body">{children}</div>,
    ModalFooter: ({ children }) => <div data-testid="modal-footer">{children}</div>,
}));

// Also mock via @/ alias path so components using @/components/BaseModal are covered
vi.mock('@/components/BaseModal', () => ({
    BaseModal: ({ isOpen, onClose: _onClose, children, className }) => {
        if (!isOpen) return null;
        return (
            <div data-testid="base-modal" className={className} role="dialog">
                {children}
            </div>
        );
    },
    ModalHeader: ({ children, icon, onClose }) => (
        <div data-testid="modal-header">
            {icon}
            <span>{children}</span>
            {onClose && <button data-testid="modal-close" onClick={onClose}>×</button>}
        </div>
    ),
    ModalBody: ({ children }) => <div data-testid="modal-body">{children}</div>,
    ModalFooter: ({ children }) => <div data-testid="modal-footer">{children}</div>,
}));

// ─── Config mock ─────────────────────────────────────────────────────────────

vi.mock('../lib/config', () => ({
    default: {
        API_URL: '/api',
        POLLING_INTERVAL: 5000,
        AUTH_ENABLED: false,
    },
}));

// ─── CountdownTimer mock ─────────────────────────────────────────────────────

vi.mock('../components/CountdownTimer', () => ({
    CountdownTimer: ({ targetTime }) => <span data-testid="countdown">{targetTime}</span>,
}));

// ─── API mock ────────────────────────────────────────────────────────────────

vi.mock('../utils/api', () => ({
    api: {
        get: vi.fn().mockResolvedValue({ ok: true, data: {} }),
        post: vi.fn().mockResolvedValue({ ok: true, data: {} }),
    },
}));
