import { logger } from '@/utils/logger';
/**
 * AWS Amplify Configuration
 * 
 * Configures Amplify Auth with Cognito User Pool settings.
 * Reads from window.CONFIG (runtime) or NEXT_PUBLIC_* env vars (build time).
 * 
 * @module lib/amplifyConfig
 */

import { Amplify } from 'aws-amplify';
import config from './config';

/**
 * Configure Amplify with Cognito settings
 * Called once at app startup
 */
export function configureAmplify() {
    // Read lazily from window.CONFIG to ensure it is loaded after config.js
    const windowAuth = typeof window !== 'undefined' ? window.CONFIG?.AUTH : undefined;
    const auth = windowAuth?.userPoolId ? windowAuth : config.AUTH;
    
    if (!auth?.enabled) {
        return false;
    }
    
    if (!auth.userPoolId || !auth.clientId || !auth.region) {
        logger.error('amplify', 'Missing Cognito config: userPoolId, clientId, region required');
        return false;
    }
    
    Amplify.configure({
        Auth: {
            Cognito: {
                userPoolId: auth.userPoolId,
                userPoolClientId: auth.clientId,
                loginWith: {
                    email: true,
                },
                signUpVerificationMethod: 'code',
                userAttributes: {
                    email: {
                        required: true,
                    },
                },
                passwordFormat: {
                    minLength: 12,
                    requireLowercase: true,
                    requireUppercase: true,
                    requireNumbers: true,
                    requireSpecialCharacters: true,
                },
            },
        },
    });
    
    return true;
}

export default configureAmplify;
