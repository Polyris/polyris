/**
 * Login Page Component
 * 
 * Provides authentication UI for polyris Console with support for:
 * - Email/password sign in
 * - New password setup (first login)
 * - MFA verification (if enabled)
 * - Password reset flow
 * 
 * Styled to match the polyris Console design system.
 * 
 * @module components/LoginPage
 */

import React, { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { useAuth, AUTH_STATE } from '@/hooks/useAuth';
import { 
    Zap, 
    Mail, 
    Lock, 
    Eye, 
    EyeOff, 
    AlertCircle, 
    Loader2,
    KeyRound,
    ArrowLeft,
    CheckCircle,
    Shield
} from 'lucide-react';
import type { LoginFormProps } from '@/types';

// -----------------------------------------------------------------------------
// Login Form
// -----------------------------------------------------------------------------

function LoginForm({ onForgotPassword }: LoginFormProps) {
    const { signIn, error, clearError } = useAuth();
    
    const [email, setEmail] = useState('');
    const [password, setPassword] = useState('');
    const [showPassword, setShowPassword] = useState(false);
    const [isLoading, setIsLoading] = useState(false);
    
    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        clearError();
        setIsLoading(true);
        
        try {
            await signIn(email, password);
        } catch {
            // Error is handled by context
        } finally {
            setIsLoading(false);
        }
    };
    
    return (
        <form onSubmit={handleSubmit} className="lp-login-form">
            <div className="form-group">
                <label htmlFor="email" className="lp-form-label">
                    <Mail size={16} className="lp-form-label-icon" />
                    Email
                </label>
                <Input
                    id="email"
                    type="email"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    placeholder="you@company.com"
                    autoComplete="email"
                    required
                    disabled={isLoading}
                />
            </div>
            
            <div className="form-group">
                <label htmlFor="password" className="lp-form-label">
                    <Lock size={16} className="lp-form-label-icon" />
                    Password
                </label>
                <div className="lp-password-input-wrapper">
                    <Input
                        id="password"
                        type={showPassword ? 'text' : 'password'}
                        value={password}
                        onChange={(e) => setPassword(e.target.value)}
                        placeholder="••••••••••••"
                        autoComplete="current-password"
                        required
                        disabled={isLoading}
                    />
                    <button
                        type="button"
                        className="lp-password-toggle" aria-label="Toggle password visibility"
                        onClick={() => setShowPassword(!showPassword)}
                        tabIndex={-1}
                    >
                        {showPassword ? <EyeOff size={18} /> : <Eye size={18} />}
                    </button>
                </div>
            </div>
            
            {error && (
                <div className="lp-login-error" role="alert">
                    <AlertCircle size={16} />
                    <span>{error}</span>
                </div>
            )}
            
            <Button 
                type="submit" 
                className="lp-login-button"
                disabled={isLoading || !email || !password}
            >
                {isLoading ? (
                    <>
                        <Loader2 size={18} className="animate-spin mr-2" />
                        Signing in...
                    </>
                ) : (
                    'Sign In'
                )}
            </Button>
            
            <button
                type="button"
                className="lp-forgot-password-link"
                onClick={onForgotPassword}
            >
                Forgot your password?
            </button>
        </form>
    );
}


// -----------------------------------------------------------------------------
// New Password Form
// -----------------------------------------------------------------------------

function NewPasswordForm() {
    const { completeNewPassword, error, clearError, challengeData } = useAuth();
    
    const [newPassword, setNewPassword] = useState('');
    const [confirmPassword, setConfirmPassword] = useState('');
    const [showPassword, setShowPassword] = useState(false);
    const [isLoading, setIsLoading] = useState(false);
    const [validationError, setValidationError] = useState('');
    
    const validatePassword = (pwd: string) => {
        if (pwd.length < 12) return 'Password must be at least 12 characters';
        if (!/[a-z]/.test(pwd)) return 'Password must contain a lowercase letter';
        if (!/[A-Z]/.test(pwd)) return 'Password must contain an uppercase letter';
        if (!/[0-9]/.test(pwd)) return 'Password must contain a number';
        if (!/[^a-zA-Z0-9]/.test(pwd)) return 'Password must contain a special character';
        return '';
    };
    
    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        clearError();
        setValidationError('');
        
        const pwdError = validatePassword(newPassword);
        if (pwdError) {
            setValidationError(pwdError);
            return;
        }
        
        if (newPassword !== confirmPassword) {
            setValidationError('Passwords do not match');
            return;
        }
        
        setIsLoading(true);
        
        try {
            await completeNewPassword(newPassword, {
                email: (challengeData?.userAttributes as Record<string, string> | undefined)?.email,
            });
        } catch {
            // Error is handled by context
        } finally {
            setIsLoading(false);
        }
    };
    
    const displayError = validationError || error;
    
    return (
        <form onSubmit={handleSubmit} className="lp-login-form">
            <div className="lp-new-password-notice">
                <Shield size={20} />
                <div>
                    <strong>Set Your Password</strong>
                    <p>Please create a new password for your account.</p>
                </div>
            </div>
            
            <div className="form-group">
                <label htmlFor="newPassword" className="lp-form-label">
                    <Lock size={16} className="lp-form-label-icon" />
                    New Password
                </label>
                <div className="lp-password-input-wrapper">
                    <Input
                        id="newPassword"
                        type={showPassword ? 'text' : 'password'}
                        value={newPassword}
                        onChange={(e) => setNewPassword(e.target.value)}
                        placeholder="••••••••••••"
                        autoComplete="new-password"
                        required
                        disabled={isLoading}
                    />
                    <button
                        type="button"
                        className="lp-password-toggle" aria-label="Toggle password visibility"
                        onClick={() => setShowPassword(!showPassword)}
                        tabIndex={-1}
                    >
                        {showPassword ? <EyeOff size={18} /> : <Eye size={18} />}
                    </button>
                </div>
                <div className="lp-password-requirements">
                    Min 12 chars, uppercase, lowercase, number, special character
                </div>
            </div>
            
            <div className="form-group">
                <label htmlFor="confirmPassword" className="lp-form-label">
                    <Lock size={16} className="lp-form-label-icon" />
                    Confirm Password
                </label>
                <Input
                    id="confirmPassword"
                    type={showPassword ? 'text' : 'password'}
                    value={confirmPassword}
                    onChange={(e) => setConfirmPassword(e.target.value)}
                    placeholder="••••••••••••"
                    autoComplete="new-password"
                    required
                    disabled={isLoading}
                />
            </div>
            
            {displayError && (
                <div className="lp-login-error" role="alert">
                    <AlertCircle size={16} />
                    <span>{displayError}</span>
                </div>
            )}
            
            <Button 
                type="submit" 
                className="lp-login-button"
                disabled={isLoading || !newPassword || !confirmPassword}
            >
                {isLoading ? (
                    <>
                        <Loader2 size={18} className="animate-spin mr-2" />
                        Updating...
                    </>
                ) : (
                    'Set Password'
                )}
            </Button>
        </form>
    );
}

// -----------------------------------------------------------------------------
// MFA Form
// -----------------------------------------------------------------------------

function MfaForm() {
    const { verifyMfa, error, clearError } = useAuth();
    
    const [code, setCode] = useState('');
    const [isLoading, setIsLoading] = useState(false);
    
    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        clearError();
        setIsLoading(true);
        
        try {
            await verifyMfa(code);
        } catch {
            // Error is handled by context
        } finally {
            setIsLoading(false);
        }
    };
    
    return (
        <form onSubmit={handleSubmit} className="lp-login-form">
            <div className="lp-mfa-notice">
                <KeyRound size={20} />
                <div>
                    <strong>Two-Factor Authentication</strong>
                    <p>Enter the code from your authenticator app.</p>
                </div>
            </div>
            
            <div className="form-group">
                <label htmlFor="mfaCode" className="lp-form-label">
                    <KeyRound size={16} className="lp-form-label-icon" />
                    Verification Code
                </label>
                <Input
                    id="mfaCode"
                    type="text"
                    inputMode="numeric"
                    pattern="[0-9]*"
                    value={code}
                    onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
                    placeholder="000000"
                    autoComplete="one-time-code"
                    required
                    disabled={isLoading}
                    className="lp-mfa-input"
                />
            </div>
            
            {error && (
                <div className="lp-login-error" role="alert">
                    <AlertCircle size={16} />
                    <span>{error}</span>
                </div>
            )}
            
            <Button 
                type="submit" 
                className="lp-login-button"
                disabled={isLoading || code.length !== 6}
            >
                {isLoading ? (
                    <>
                        <Loader2 size={18} className="animate-spin mr-2" />
                        Verifying...
                    </>
                ) : (
                    'Verify'
                )}
            </Button>
        </form>
    );
}

// -----------------------------------------------------------------------------
// Forgot Password Form
// -----------------------------------------------------------------------------

function ForgotPasswordForm({ onBack }: { onBack: () => void }) {
    const { forgotPassword, confirmForgotPassword, error, clearError } = useAuth();
    
    const [step, setStep] = useState('request'); // 'request' | 'confirm' | 'success'
    const [email, setEmail] = useState('');
    const [code, setCode] = useState('');
    const [newPassword, setNewPassword] = useState('');
    const [showPassword, setShowPassword] = useState(false);
    const [isLoading, setIsLoading] = useState(false);
    const [validationError, setValidationError] = useState('');
    
    const handleRequestReset = async (e: React.FormEvent) => {
        e.preventDefault();
        clearError();
        setIsLoading(true);
        
        try {
            await forgotPassword(email);
            setStep('confirm');
        } catch {
            // Error handled by context
        } finally {
            setIsLoading(false);
        }
    };
    
    const handleConfirmReset = async (e: React.FormEvent) => {
        e.preventDefault();
        clearError();
        setValidationError('');
        
        if (newPassword.length < 12) {
            setValidationError('Password must be at least 12 characters');
            return;
        }
        
        setIsLoading(true);
        
        try {
            await confirmForgotPassword(email, code, newPassword);
            setStep('success');
        } catch {
            // Error handled by context
        } finally {
            setIsLoading(false);
        }
    };
    
    const displayError = validationError || error;
    
    if (step === 'success') {
        return (
            <div className="lp-login-form">
                <div className="lp-success-notice">
                    <CheckCircle size={24} />
                    <div>
                        <strong>Password Reset Complete</strong>
                        <p>You can now sign in with your new password.</p>
                    </div>
                </div>
                <Button onClick={onBack} className="lp-login-button">
                    Back to Sign In
                </Button>
            </div>
        );
    }
    
    if (step === 'confirm') {
        return (
            <form onSubmit={handleConfirmReset} className="lp-login-form">
                <div className="lp-reset-notice">
                    <Mail size={20} />
                    <div>
                        <strong>Check Your Email</strong>
                        <p>We sent a verification code to {email}</p>
                    </div>
                </div>
                
                <div className="form-group">
                    <label htmlFor="resetCode" className="lp-form-label">
                        <KeyRound size={16} className="lp-form-label-icon" />
                        Verification Code
                    </label>
                    <Input
                        id="resetCode"
                        type="text"
                        value={code}
                        onChange={(e) => setCode(e.target.value)}
                        placeholder="Enter code"
                        required
                        disabled={isLoading}
                    />
                </div>
                
                <div className="form-group">
                    <label htmlFor="resetPassword" className="lp-form-label">
                        <Lock size={16} className="lp-form-label-icon" />
                        New Password
                    </label>
                    <div className="lp-password-input-wrapper">
                        <Input
                            id="resetPassword"
                            type={showPassword ? 'text' : 'password'}
                            value={newPassword}
                            onChange={(e) => setNewPassword(e.target.value)}
                            placeholder="••••••••••••"
                            autoComplete="new-password"
                            required
                            disabled={isLoading}
                        />
                        <button
                            type="button"
                            className="lp-password-toggle" aria-label="Toggle password visibility"
                            onClick={() => setShowPassword(!showPassword)}
                            tabIndex={-1}
                        >
                            {showPassword ? <EyeOff size={18} /> : <Eye size={18} />}
                        </button>
                    </div>
                </div>
                
                {displayError && (
                    <div className="lp-login-error" role="alert">
                        <AlertCircle size={16} />
                        <span>{displayError}</span>
                    </div>
                )}
                
                <Button 
                    type="submit" 
                    className="lp-login-button"
                    disabled={isLoading || !code || !newPassword}
                >
                    {isLoading ? (
                        <>
                            <Loader2 size={18} className="animate-spin mr-2" />
                            Resetting...
                        </>
                    ) : (
                        'Reset Password'
                    )}
                </Button>
                
                <button type="button" className="lp-back-link" onClick={onBack}>
                    <ArrowLeft size={16} />
                    Back to Sign In
                </button>
            </form>
        );
    }
    
    return (
        <form onSubmit={handleRequestReset} className="lp-login-form">
            <div className="lp-reset-notice">
                <Mail size={20} />
                <div>
                    <strong>Reset Password</strong>
                    <p>Enter your email to receive a verification code.</p>
                </div>
            </div>
            
            <div className="form-group">
                <label htmlFor="resetEmail" className="lp-form-label">
                    <Mail size={16} className="lp-form-label-icon" />
                    Email
                </label>
                <Input
                    id="resetEmail"
                    type="email"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    placeholder="you@company.com"
                    autoComplete="email"
                    required
                    disabled={isLoading}
                />
            </div>
            
            {error && (
                <div className="lp-login-error" role="alert">
                    <AlertCircle size={16} />
                    <span>{error}</span>
                </div>
            )}
            
            <Button 
                type="submit" 
                className="lp-login-button"
                disabled={isLoading || !email}
            >
                {isLoading ? (
                    <>
                        <Loader2 size={18} className="animate-spin mr-2" />
                        Sending...
                    </>
                ) : (
                    'Send Reset Code'
                )}
            </Button>
            
            <button type="button" className="lp-back-link" onClick={onBack}>
                <ArrowLeft size={16} />
                Back to Sign In
            </button>
        </form>
    );
}


// -----------------------------------------------------------------------------
// Main Login Page
// -----------------------------------------------------------------------------

export function LoginPage() {
    const { authState } = useAuth();
    const [showForgotPassword, setShowForgotPassword] = useState(false);
    
    const renderForm = () => {
        if (showForgotPassword) {
            return <ForgotPasswordForm onBack={() => setShowForgotPassword(false)} />;
        }
        
        switch (authState) {
            case AUTH_STATE.NEW_PASSWORD_REQUIRED:
                return <NewPasswordForm />;
            case AUTH_STATE.MFA_REQUIRED:
            case AUTH_STATE.MFA_SETUP:
                return <MfaForm />;
            default:
                return <LoginForm onForgotPassword={() => setShowForgotPassword(true)} />;
        }
    };
    
    return (
        <div className="lp-login-page">
            <div className="lp-login-container">
                <div className="lp-login-header">
                    <div className="lp-login-logo">
                        <div className="lp-login-logo-icon">
                            <Zap size={28} />
                        </div>
                        <span>polyris</span>
                    </div>
                    <h1 className="lp-login-title">Console</h1>
                    <p className="lp-login-subtitle">
                        Sign in to manage your data pipelines
                    </p>
                </div>
                
                <div className="lp-login-card">
                    {renderForm()}
                </div>
                
                <div className="lp-login-footer">
                    <p>Serverless Data Pipeline Orchestration</p>
                </div>
            </div>
        </div>
    );
}

export default LoginPage;
