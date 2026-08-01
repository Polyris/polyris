# polyris Console Authentication

This document describes how the polyris Console authenticates: AWS Cognito for
the browser UI (via Amplify) and polyris Personal Access Tokens (PATs) for
scripts/CI. For day-to-day API usage and token management, see
[api-tokens.md](./api-tokens.md).

> **OSS vs Team.** The CE gate accepts **both** a Cognito access token and a PAT,
> but *minting* PATs is **🔒 Team** (see api-tokens.md). On an OSS deployment,
> authenticate scripts/CI with a **Cognito access token** —
> `scripts/get-e2e-token.sh` obtains one from the deployment's user pool — or set
> `AUTH_ENABLED=false`.

> **How enforcement actually works (v0.87+, ADR #65).** Auth is enforced by a
> single gate inside the `console-api` Lambda (`auth.authenticate`), **not** by
> an API Gateway authorizer. The HTTP API uses a `/{proxy+}` integration, so
> every request reaches the Lambda, and the gate runs there before route
> dispatch. It accepts **either** a Cognito access token (browser) **or** a PAT
> (`plrs_…`). Enforcement is gated by the `AUTH_ENABLED` env var and is **on by
> default** (the deployed template sets it `true`) — disabling it is a
> deliberate step (see "Enabling enforcement" below). Health/metrics paths are
> always public.

## Overview

- **AWS Amplify SDK** - Battle-tested authentication library with automatic token refresh
- **Admin-only user creation** - No self-registration, users must be created by administrators
- **Dual auth at the Lambda gate** - the `console-api` Lambda verifies a Cognito token **offline** (RS256 against the pool JWKS, bound to this deployment's app client) or a PAT (hash lookup) on every non-public request when `AUTH_ENABLED=true`
- **MFA support** - Optional TOTP-based multi-factor authentication
- **Strong password policies** - 12+ characters with mixed case, numbers, and symbols

## Prerequisites

Before enabling authentication, ensure you have:

- AWS CLI configured with appropriate permissions
- Console infrastructure already deployed (`sam deploy` completed)
- Node.js 22+ for building the UI

## Architecture

```
┌─────────────────┐     ┌─────────────┐     ┌──────────────┐     ┌──────────────────────┐
│   React UI      │────▶│ CloudFront  │────▶│ API Gateway  │────▶│  Lambda console_api  │
│   + Amplify     │     │   /api/*    │     │ /{proxy+}    │     │  auth.authenticate() │
└─────────────────┘     └─────────────┘     └──────────────┘     └──────────────────────┘
        │                                    (no authorizer)              │
        │  Amplify.Auth                                                    │ verify JWT (JWKS)
        ▼                                                                  │ or hash lookup (PAT)
┌───────────────────────────────────────────────────────┐                 │
│     {namespace}-{stage}-polyris-console-users         │◀────────────────┘
│  - Admin-only user creation                           │
│  - Secure Remote Password (SRP) protocol              │
│  - Automatic token refresh                            │
└───────────────────────────────────────────────────────┘
```

## Resource Naming

All Cognito resources follow the pattern `{namespace}-{stage}-polyris-{resource}`:

| Resource | Name Pattern |
|----------|--------------|
| User Pool | `{namespace}-{stage}-polyris-console-users` |
| App Client | `{namespace}-{stage}-polyris-console-client` |
| API Tokens table | `{namespace}-{stage}-polyris-api-tokens` (PATs, ADR #65) |
| User Groups | `admins`, `viewers` |

All resources are tagged with `Environment = "polyris"`.

## Enabling Authentication

### Step 1: Enable Cognito

In `sam/samconfig.toml`, set `EnableCognitoAuth` to `true`:

```toml
parameter_overrides = [
  "Namespace=myorg",
  "Stage=dev",
  "EnableCognitoAuth=true",   # Enable Cognito authentication
]
```

CloudFront URL is automatically added to Cognito callbacks by the SAM template.

### Step 2: Redeploy SAM stack

```bash
cd sam
sam build && sam deploy
```

### Step 3: Get Auth Configuration

After `sam deploy` completes, get outputs:

```bash
aws cloudformation describe-stacks \
  --stack-name polyris-dev \
  --query "Stacks[0].Outputs" \
  --output table
```

Output example:
```json
{
  "enabled": true,
  "userPoolId": "us-east-1_abc123XYZ",
  "clientId": "7abcdef123456789ghijklmnop",
  "region": "us-east-1"
}
```

### Step 4: Configure Environment Variables

Get the env vars from CloudFormation outputs:

```bash
cd ui && ./deploy.sh
```

`ui/deploy.sh` reads CloudFormation outputs and generates `config.js` automatically.

| Variable | Value |
|----------|-------|
| `NEXT_PUBLIC_AUTH_ENABLED` | `true` |
| `NEXT_PUBLIC_COGNITO_USER_POOL_ID` | (from output) |
| `NEXT_PUBLIC_COGNITO_CLIENT_ID` | (from output) |
| `NEXT_PUBLIC_COGNITO_REGION` | `us-east-1` |


**For local development**, create `.env.local`:
```bash
cd ui
cp .env.example .env.local
# Edit with your Cognito values
```

### Step 5: Create First User

```bash
# Get User Pool ID from aws cloudformation describe-stacks --stack-name polyris-dev --query "Stacks[0].Outputs"
USER_POOL_ID=$(aws cloudformation describe-stacks --stack-name polyris-dev \
  --query "Stacks[0].Outputs[?OutputKey=='CognitoUserPoolId'].OutputValue" --output text)

# Create admin user
aws cognito-idp admin-create-user \
  --user-pool-id $USER_POOL_ID \
  --username admin@yourcompany.com \
  --user-attributes Name=email,Value=admin@yourcompany.com \
  --temporary-password "TempPassword123!"

# Add to admins group
aws cognito-idp admin-add-user-to-group \
  --user-pool-id $USER_POOL_ID \
  --username admin@yourcompany.com \
  --group-name admins
```

## Disabling Authentication

To disable authentication:

### Step 1: Update SAM parameters

In `sam/samconfig.toml`, change `EnableCognitoAuth` to `false`:

```toml
parameter_overrides = [
  "EnableCognitoAuth=false",
]
```

### Step 2: Apply Changes

```bash
cd sam
sam build && sam deploy
```

### Step 3: Redeploy UI

Rerun `ui/deploy.sh` to regenerate `config.js`:

Set `NEXT_PUBLIC_AUTH_ENABLED` = `false`

Or remove all `NEXT_PUBLIC_COGNITO_*` variables.


> **Note:** Disabling auth will NOT delete the Cognito User Pool. Users and settings are preserved if you re-enable later.

## User Management

### Creating Users

Users can only be created by administrators. There are two methods:

#### Method 1: AWS Console (Recommended)

1. **Go to AWS Console** → Cognito → User pools
2. **Select your user pool** (e.g., `mycompany-dev-polyris-console-users`)
3. **Click "Users"** in the left sidebar
4. **Click "Create user"** button
5. **Fill in the form:**
   - **Invitation message:** Send email invitation (optional)
   - **User name:** Enter email address (e.g., `user@company.com`)
   - **Email address:** Same as username
   - **Temporary password:** Set or generate one
   - ✅ **Mark email address as verified**
6. **Click "Create user"**

After creating the user, add them to a group:

1. **Go to "Groups"** in the left sidebar
2. **Click "admins"** (or "viewers" for read-only)
3. **Click "Add user to group"**
4. **Select the user** and confirm

#### Method 2: AWS CLI

```bash
# Get User Pool ID
USER_POOL_ID=$(aws cloudformation describe-stacks --stack-name polyris-dev \
  --query "Stacks[0].Outputs[?OutputKey=='CognitoUserPoolId'].OutputValue" --output text)

# Create user
aws cognito-idp admin-create-user \
  --user-pool-id $USER_POOL_ID \
  --username user@example.com \
  --user-attributes \
      Name=email,Value=user@example.com \
      Name=email_verified,Value=true \
  --temporary-password "TempPassword123!" \
  --message-action SUPPRESS  # Remove to send email invitation

# Add user to admins group
aws cognito-idp admin-add-user-to-group \
  --user-pool-id $USER_POOL_ID \
  --username user@example.com \
  --group-name admins
```

### User Groups

Two groups are created by default:

| Group | Description | Precedence |
|-------|-------------|------------|
| `admins` | Full access to console | 0 |
| `viewers` | Read-only access (future) | 10 |

### Listing Users

```bash
aws cognito-idp list-users \
  --user-pool-id us-east-1_abc123XYZ
```

### Disabling/Deleting Users

```bash
# Disable user (can be re-enabled)
aws cognito-idp admin-disable-user \
  --user-pool-id us-east-1_abc123XYZ \
  --username user@example.com

# Delete user (permanent)
aws cognito-idp admin-delete-user \
  --user-pool-id us-east-1_abc123XYZ \
  --username user@example.com
```

### Resetting Password

```bash
aws cognito-idp admin-set-user-password \
  --user-pool-id us-east-1_abc123XYZ \
  --username user@example.com \
  --password "NewPassword123!" \
  --permanent
```

## First Login Experience

When a new user first logs in:

1. Enter email and temporary password
2. System prompts to set a new permanent password
3. Password must meet requirements:
   - Minimum 12 characters
   - Uppercase letter
   - Lowercase letter  
   - Number
   - Special character
4. If MFA is enabled, user may be prompted to set up TOTP

## Security Features

### Password Policy

- Minimum length: 12 characters
- Requires: uppercase, lowercase, numbers, symbols
- Temporary password validity: 7 days

### Token Management (Amplify)

- Access tokens expire after 1 hour (configurable)
- Refresh tokens valid for 30 days (configurable)
- **Automatic token refresh** handled by Amplify SDK
- Token revocation on sign out (global)

### MFA Configuration

MFA can be configured as:
- `OFF` - No MFA
- `OPTIONAL` - Users can enable MFA
- `ON` - MFA required for all users

When MFA is enabled, users can use any TOTP authenticator app (Google Authenticator, Authy, 1Password, etc.)

## API Route Protection

### Protected Routes

All API routes require authentication except public routes listed below:

- `GET /api/pipelines`
- `GET /api/pipeline-status?name={name}`
- `POST /api/pipeline-run?name={name}`
- `POST /api/task-retry?name={name}`
- ... (all standard API routes)

### Public Routes (no auth required)

These routes are accessible without authentication:

- `GET /api/action/skip` - Slack callback
- `GET /api/action/fail` - Slack callback
- `GET /api/action/restart` - Slack callback

## Troubleshooting

### "Invalid username or password"

- Verify the email is correct
- Check if user exists in Cognito
- Ensure temporary password hasn't expired (7 days)

### "User not confirmed"

- Admin-created users don't need confirmation
- Check if user status is `FORCE_CHANGE_PASSWORD` - user needs to set new password

### Token refresh failing

- Check if refresh token hasn't expired (30 days)
- Clear browser storage and sign in again
- Verify Cognito user pool is accessible

### 401 Unauthorized from API

- Confirm `AUTH_ENABLED=true` is intended (when off, the API requires no token)
- Check the token isn't expired or revoked; for a PAT, regenerate via the
  Console (avatar → Settings → API Tokens)
- Ensure `COGNITO_USER_POOL_ID` / `COGNITO_CLIENT_ID` on the `console-api`
  Lambda match the pool/client the UI logs into (the gate binds tokens to this
  app client). Note: auth is enforced **in the Lambda gate**, not by an API
  Gateway authorizer.

## Development

### Running Locally Without Auth

For local development, create `.env.local`:

```bash
cd ui
cp .env.example .env.local
```

Keep auth disabled (default):
```env
NEXT_PUBLIC_AUTH_ENABLED=false
```

### Testing Auth Locally

To test auth locally against deployed Cognito, set in `.env.local` (the
`API_GATEWAY_URL` name in older docs is gone — use `NEXT_PUBLIC_API_URL`, the full
invoke URL with stage and `/api`):

```env
NEXT_PUBLIC_API_URL=https://xxxxxxxxxx.execute-api.us-east-1.amazonaws.com/dev/api
NEXT_PUBLIC_AUTH_ENABLED=true
NEXT_PUBLIC_COGNITO_USER_POOL_ID=us-east-1_abc123XYZ
NEXT_PUBLIC_COGNITO_CLIENT_ID=7abcdef123456789ghijklmnop
NEXT_PUBLIC_COGNITO_REGION=us-east-1
```

Amplify signs in with email/password directly, so **no OAuth callback URLs need
to be registered** for `http://localhost:3000`. The pool/client above must match
what the `console-api` Lambda validates against. Then run:
```bash
npm run dev
```

See `docs/operations/UI.md` → *Local UI against a deployed API* for the full
setup (with and without auth).

## Related Files

| File | Description |
|------|-------------|
| `sam/template.yaml` | Auth & Console parameters |
| `sam/template.yaml` | Cognito infrastructure |
| `ui/src/lib/config.ts` | Config module (env vars + fallback) |
| `ui/src/lib/amplifyConfig.ts` | Amplify configuration |
| `ui/src/hooks/useAuth.tsx` | Auth context and hooks (Amplify) |
| `ui/src/components/LoginPage.tsx` | Login UI |
| `ui/src/components/AuthGate.tsx` | Auth wrapper component |
| `ui/src/components/UserMenu.tsx` | User dropdown in header |
| `ui/src/utils/api.ts` | API client with auth headers |
| `ui/.env.example` | Example environment variables |
