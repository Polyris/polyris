/**
 * Centralized Icon System using Lucide React
 * 
 * This file provides consistent icons across the entire application.
 * All icons are Lucide React components with standardized sizing and colors.
 * 
 * Replaces all emoji usage with professional Lucide icons.
 */

import React from 'react';
import {
    // Status icons
    CheckCircle2,
    XCircle,
    Loader2,
    Clock,
    Lock,
    Pause,
    Play,
    SkipForward,
    AlertTriangle,
    AlertCircle,
    PlayCircle,
    StopCircle,
    Circle,
    CircleDot,
    Ban,
    HelpCircle,
    Check,
    X as XIcon,
    
    // Navigation icons
    Workflow,
    Package,
    ListTodo,
    Activity,
    Calendar,
    LayoutDashboard,
    BarChart3,
    Gauge,
    KeyRound,
    
    // Action icons
    RefreshCw,
    Zap,
    Rocket,
    Rewind,
    Target,
    RotateCcw,
    Trash2,
    Copy,
    Code,
    ExternalLink,
    ChevronRight,
    ChevronDown,
    ChevronLeft,
    X,
    Plus,
    Minus,
    Search,
    Filter,
    Settings,
    MoreVertical,
    ArrowRight,
    ArrowUp,
    ArrowDown,
    ArrowLeft,
    Square,
    
    // UI icons
    Sun,
    Moon,
    Bell,
    BellOff,
    BellRing,
    Info,
    FileText,
    Terminal,
    Database,
    Server,
    Cloud,
    Link2,
    Unlink,
    Eye,
    EyeOff,
    Keyboard,
    Command,
    User,
    
    // New icons for emoji replacement
    BookOpen,
    Wrench,
    Download,
    ClipboardList,
    Timer,
    Hourglass,
    Inbox,
    Siren,
    Lightbulb,
    Palette,
    Plug,
    CircleHelp,
    
    // Graph/DAG icons
    GitBranch,
    GitMerge,
    Network,
    Share2,
    Globe,
    
    // Asset detail icons
    Shield,
    Table2,
    Folder,
    ArrowUpRight,
    ArrowDownRight,
    List
} from 'lucide-react';

// ============================================================================
// STATUS ICONS - For task/pipeline/asset statuses
// ============================================================================

/**
 * Status icon component with built-in styling
 */
export function StatusIcon({ status, size = 16, className = '' }: { status: string; size?: number; className?: string }) {
    const baseClass = `inline-flex items-center justify-center flex-shrink-0 ${className}`;
    
    const iconMap = {
        // Success states
        success: <CheckCircle2 size={size} className={`${baseClass} text-green-500`} />,
        succeeded: <CheckCircle2 size={size} className={`${baseClass} text-green-500`} />,
        
        // Error states
        failed: <XCircle size={size} className={`${baseClass} text-red-500`} />,
        upstream_failed: <XCircle size={size} className={`${baseClass} text-red-400`} />,
        up_failed: <XCircle size={size} className={`${baseClass} text-red-400`} />,
        
        // Active states
        running: <Loader2 size={size} className={`${baseClass} text-blue-500 animate-spin`} />,
        pending: <Loader2 size={size} className={`${baseClass} text-blue-400 animate-spin`} />,
        
        // Waiting states
        waiting: <Clock size={size} className={`${baseClass} text-gray-400`} />,
        waiting_delay: <Clock size={size} className={`${baseClass} text-amber-500`} />,
        waiting_decision: <HelpCircle size={size} className={`${baseClass} text-amber-500`} />,
        waiting_paused: <Pause size={size} className={`${baseClass} text-amber-500`} />,
        
        // Ready states
        deps_ready: <PlayCircle size={size} className={`${baseClass} text-indigo-500`} />,
        ready: <CheckCircle2 size={size} className={`${baseClass} text-green-500`} />,
        
        // Stopped/Skipped states
        stopped: <StopCircle size={size} className={`${baseClass} text-amber-500`} />,
        skipped: <SkipForward size={size} className={`${baseClass} text-slate-500`} />,
        aborted: <Ban size={size} className={`${baseClass} text-orange-500`} />,

        // Execution-level terminal states (ADR #112)
        timed_out: <Clock size={size} className={`${baseClass} text-red-500`} />,
        recovered: <CheckCircle2 size={size} className={`${baseClass} text-amber-500`} />,
        
        // Asset states
        updated: <CheckCircle2 size={size} className={`${baseClass} text-green-500`} />,
        listening: <Eye size={size} className={`${baseClass} text-blue-400`} />,
        watching: <Eye size={size} className={`${baseClass} text-blue-400`} />,
        queued: <Clock size={size} className={`${baseClass} text-gray-400`} />,
        
        // Partial/Warning
        partial: <AlertTriangle size={size} className={`${baseClass} text-amber-500`} />,
        warning: <AlertTriangle size={size} className={`${baseClass} text-amber-500`} />,
    };
    
    return iconMap[status as keyof typeof iconMap] || <Circle size={size} className={`${baseClass} text-gray-300`} />;
}

// ============================================================================
// STALENESS ICONS - For asset freshness indicators
// ============================================================================

export function StalenessIcon({ status, size = 16, className = '' }: { status: string; size?: number; className?: string }) {
    const baseClass = `inline-flex items-center justify-center flex-shrink-0 ${className}`;
    
    const iconMap: Record<string, React.ReactNode> = {
        fresh: <CheckCircle2 size={size} className={`${baseClass} text-green-500`} />,
        warning: <AlertTriangle size={size} className={`${baseClass} text-amber-500`} />,
        stale: <Clock size={size} className={`${baseClass} text-red-500`} />,
        unknown: <HelpCircle size={size} className={`${baseClass} text-gray-400`} />,
    };
    
    return iconMap[status] || <HelpCircle size={size} className={`${baseClass} text-gray-400`} />;
}

export const STALENESS_ICONS_COMPONENTS = {
    fresh: (size = 16) => <CheckCircle2 size={size} className="text-green-500" />,
    warning: (size = 16) => <AlertTriangle size={size} className="text-amber-500" />,
    stale: (size = 16) => <Clock size={size} className="text-red-500" />,
    unknown: (size = 16) => <HelpCircle size={size} className="text-gray-400" />,
};

// ============================================================================
// NAVIGATION ICONS - For main app navigation
// ============================================================================

export const NavIcons = {
    pipelines: Workflow,
    assets: Package,
    tasks: ListTodo,
    runs: Activity,
    calendar: Calendar,
    dashboard: LayoutDashboard,
};

export function NavIcon({ type, size = 18, className = '' }: { type: string; size?: number; className?: string }) {
    const Icon = NavIcons[type as keyof typeof NavIcons];
    if (!Icon) return <Circle size={size} className={className} />;
    return <Icon size={size} className={className} />;
}

// ============================================================================
// ACTION ICONS - For buttons and interactive elements
// ============================================================================

export const ActionIcons = {
    // Pipeline actions
    run: Rocket,
    refresh: RefreshCw,
    // v0.79.7 (ADR #79) — THE canonical backfill icon. Every backfill
    // affordance in the app (nav tab, list/detail headers, modal header,
    // run badges, notifications, pipeline button, help legend) renders
    // this. Change it here and it changes everywhere. Components must
    // import ActionIcons (or ContextIcons.backfill, which aliases this)
    // — never a raw lucide Rewind/Rocket/History for backfill.
    backfill: Rewind,
    stop: StopCircle,
    pause: Pause,
    resume: Play,
    
    // Task actions
    skip: SkipForward,
    fail: XCircle,
    restart: RotateCcw,
    runToHere: Target,
    runFromHere: Play,
    runOnlyThis: CircleDot,
    
    // Asset actions
    trigger: Zap,
    delete: Trash2,
    
    // General actions
    copy: Copy,
    externalLink: ExternalLink,
    close: X,
    add: Plus,
    remove: Minus,
    search: Search,
    filter: Filter,
    settings: Settings,
    more: MoreVertical,
    expand: ChevronDown,
    collapse: ChevronRight,
};

// ============================================================================
// UI ICONS - For interface elements
// ============================================================================

export const UIIcons = {
    // Theme
    lightMode: Sun,
    darkMode: Moon,
    
    // Notifications
    bell: Bell,
    bellActive: BellRing,
    
    // Info
    info: Info,
    help: HelpCircle,
    warning: AlertTriangle,
    error: AlertCircle,
    
    // Data
    file: FileText,
    terminal: Terminal,
    database: Database,
    server: Server,
    cloud: Cloud,
    
    // Connection
    link: Link2,
    unlink: Unlink,
    
    // Visibility
    visible: Eye,
    hidden: EyeOff,
    
    // Keyboard
    keyboard: Keyboard,
    command: Command,
    
    // Graph
    branch: GitBranch,
    merge: GitMerge,
    network: Network,
    share: Share2,
};

// ============================================================================
// ICON WRAPPER COMPONENTS - For common use cases
// ============================================================================

/**
 * Icon with loading state
 */
export function LoadingIcon({ size = 16, className = '' }) {
    return <Loader2 size={size} className={`animate-spin ${className}`} />;
}

/**
 * Refresh icon with optional spinning state
 */
export function RefreshIcon({ spinning = false, size = 16, className = '' }) {
    return <RefreshCw size={size} className={`${spinning ? 'animate-spin' : ''} ${className}`} />;
}

/**
 * Chevron that rotates based on expanded state
 */
export function ExpandIcon({ expanded = false, size = 16, className = '' }) {
    const Icon = expanded ? ChevronDown : ChevronRight;
    return <Icon size={size} className={`transition-transform ${className}`} />;
}

// ============================================================================
// ELEMENT TYPE ICONS - For task/asset/trigger types
// ============================================================================

export const ElementIcons = {
    task: Wrench,
    asset: Package,
    producer: Wrench,
    consumer: Download,
    dagTrigger: Rocket,
};

// ============================================================================
// CONTEXT ICONS - For UI sections and contexts
// ============================================================================

export const ContextIcons = {
    help: BookOpen,
    shortcuts: Keyboard,
    icons: Palette,
    // v0.79.7 (ADR #79) — single source of truth for the backfill icon.
    // References ActionIcons.backfill so there is exactly ONE place to
    // change it. Do not hardcode a lucide component here.
    backfill: ActionIcons.backfill,
    api: Plug,
    clipboard: ClipboardList,
    events: FileText,
    actions: Zap,
    tip: Lightbulb,
    link: Link2,
    empty: Inbox,
    critical: Siren,
    timer: Timer,
    hourglass: Hourglass,
    question: CircleHelp,
    chart: Gauge,
    settings: Settings,
    search: Search,
    arrowRight: ArrowRight,
};

// ============================================================================
// MARK ICONS - For inline status marks (check/cross)
// ============================================================================

export const MarkIcons = {
    check: Check,
    cross: XIcon,
};

// ============================================================================
// TOAST ICONS - For notifications/toasts
// ============================================================================

export const ToastIcons = {
    success: CheckCircle2,
    error: XCircle,
    warning: AlertTriangle,
    info: Info,
};

// ============================================================================
// EXPORTS - Re-export commonly used Lucide icons directly
// ============================================================================

export {
    // Re-export for direct use
    CheckCircle2,
    XCircle,
    Loader2,
    Clock,
    Lock,
    Pause,
    Play,
    SkipForward,
    AlertTriangle,
    AlertCircle,
    PlayCircle,
    StopCircle,
    Circle,
    CircleDot,
    HelpCircle,
    RefreshCw,
    Zap,
    Rocket,
    Rewind,
    Target,
    RotateCcw,
    Trash2,
    Copy,
    Code,
    ExternalLink,
    X,
    Plus,
    Minus,
    Search,
    Filter,
    Settings,
    Sun,
    Moon,
    Bell,
    BellOff,
    BellRing,
    Info,
    Eye,
    Keyboard,
    Link2,
    Package,
    Workflow,
    ListTodo,
    Activity,
    Calendar,
    KeyRound,
    ChevronRight,
    ChevronDown,
    ChevronLeft,
    Ban,
    Database,
    Terminal,
    FileText,
    BarChart3,
    GitBranch,
    GitMerge,
    Network,
    // New exports for emoji replacement
    BookOpen,
    Wrench,
    Download,
    ClipboardList,
    Timer,
    Hourglass,
    Inbox,
    Siren,
    Lightbulb,
    Palette,
    Plug,
    CircleHelp,
    Check,
    XIcon,
    Gauge,
    ArrowRight,
    ArrowUp,
    ArrowDown,
    ArrowLeft,
    Square,
    Globe,
    User,
    Shield,
    Table2,
    Folder,
    ArrowUpRight,
    ArrowDownRight,
    List,
    MoreVertical,
};
