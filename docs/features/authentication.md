# Console authentication

The console signs users in with Amazon Cognito. A platform installed with `polyris-up` has sign-in on by default, and there is no self sign-up: an admin creates every user, and every user has full access to the console. This page shows how to manage users, get a token for scripts, and turn sign-in on or off.

## Turn sign-in on or off

Sign-in is on unless you turned it off. `polyris-up` updates the stack and the console's `config.js` together, so there is no UI step:

```bash
polyris-up --param EnableCognitoAuth=false   # turn sign-in off
polyris-up --param EnableCognitoAuth=true --admin-email admin@yourcompany.com   # turn it back on
```

`--admin-email` creates the first user, and Cognito emails it a temporary password. Changing the email later replaces that user: the previous one is deleted.

> **Warning:** Turning sign-in off deletes the user pool with its users. Turning it back on creates a new, empty pool with only the `--admin-email` user. With sign-in off, anyone who can reach the console URL can use the console.

## Manage users

Users sign in with their email address. The commands below need the user pool's id, which the platform stack (`<namespace>-<stage>`, the one `polyris-up` installed) has as its output `CognitoUserPoolId`:

```bash
USER_POOL_ID=$(aws cloudformation describe-stacks --stack-name mycompany-dev \
  --query "Stacks[0].Outputs[?OutputKey=='CognitoUserPoolId'].OutputValue" --output text)
```

### Create a user

In the AWS console: Cognito → User pools → `<namespace>-<stage>-polyris-console` → Users → **Create user**. Enter the email address as the user name, set or generate a temporary password, and tick **Mark email address as verified**.

With the AWS CLI:

```bash
aws cognito-idp admin-create-user \
  --user-pool-id "$USER_POOL_ID" \
  --username user@example.com \
  --user-attributes Name=email,Value=user@example.com Name=email_verified,Value=true \
  --temporary-password "TempPassword123"
```

Cognito emails the user an invitation; add `--message-action SUPPRESS` to send none and pass the password yourself.

### List, disable, delete, reset

```bash
aws cognito-idp list-users --user-pool-id "$USER_POOL_ID"

aws cognito-idp admin-disable-user --user-pool-id "$USER_POOL_ID" --username user@example.com   # can be re-enabled
aws cognito-idp admin-delete-user --user-pool-id "$USER_POOL_ID" --username user@example.com    # permanent

aws cognito-idp admin-set-user-password --user-pool-id "$USER_POOL_ID" \
  --username user@example.com --password "NewPassword123" --permanent
```

### First sign-in

A new user signs in with the email and the temporary password, and then chooses a permanent password: at least 12 characters, with an uppercase letter, a lowercase letter and a number. A temporary password expires after 7 days.

## A token for scripts

The console API takes the same Cognito token the console uses: `Authorization: Bearer <token>`. To get one without a browser, give a user a permanent password (`admin-set-user-password ... --permanent`, above) and sign in with an admin call, which needs AWS credentials allowed `cognito-idp:AdminInitiateAuth`:

```bash
CLIENT_ID=$(aws cloudformation describe-stacks --stack-name mycompany-dev \
  --query "Stacks[0].Outputs[?OutputKey=='CognitoClientId'].OutputValue" --output text)
API_URL=$(aws cloudformation describe-stacks --stack-name mycompany-dev \
  --query "Stacks[0].Outputs[?OutputKey=='ConsoleApiUrl'].OutputValue" --output text)

TOKEN=$(aws cognito-idp admin-initiate-auth \
  --user-pool-id "$USER_POOL_ID" --client-id "$CLIENT_ID" \
  --auth-flow ADMIN_USER_PASSWORD_AUTH \
  --auth-parameters "USERNAME=ci@example.com,PASSWORD=$CI_PASSWORD" \
  --query 'AuthenticationResult.AccessToken' --output text)

curl -s "$API_URL/api/pipelines" -H "Authorization: Bearer $TOKEN"
```

The token is valid for 1 hour. In a checkout of this repository, `scripts/get-e2e-token.sh` runs the same sign-in. The routes are listed in [API.md](../operations/API.md).

## Which requests need a token

With sign-in on, every API request needs a token except these, which are always public:

| Path | Why public |
|------|-----------|
| `GET /api/health` and `GET /api/health/live` | Probes and monitors must work without credentials. Without a valid token, `/api/health` answers the status words only; its details (messages, resource names, `findings`) need one |
| `GET /api/metrics` | Ops metrics endpoint; scraped by monitoring agents |

The check runs in the `console-api` Lambda, not in an API Gateway authorizer: it verifies the token offline, against the user pool's keys, and accepts only tokens issued to this console's app client.

## Security settings

| Setting | Value |
|---------|-------|
| User pool | `<namespace>-<stage>-polyris-console`, app client `<namespace>-<stage>-polyris-console-client` |
| Password | 12+ characters with upper- and lowercase letters and a number; a temporary password expires after 7 days |
| Tokens | Access token 1 hour, refresh token 30 days; the console refreshes the access token on its own. Sign-out clears the tokens from this browser |
| MFA | Off when the pool is created (`MfaConfiguration` in `sam/template.yaml`). If you turn it on in Cognito, the login page asks for a code from a TOTP app |

The user pool carries the tags `polyris:namespace`, `polyris:stage` and `polyris:component`.

## Troubleshooting

### "Invalid username or password"

- Check the email, and that the user exists (`list-users`).
- A temporary password expires after 7 days: set a new one with `admin-set-user-password`.

### "User not confirmed"

Admin-created users need no confirmation. A user whose status is `FORCE_CHANGE_PASSWORD` still has to choose a permanent password at sign-in.

### Signed out again and again

The refresh token lasts 30 days. Clear the browser's storage for the console and sign in again.

### 401 Unauthorized from the API

- Get a fresh token: an access token lasts 1 hour.
- Check that the token comes from this platform's user pool and app client: the API accepts no other.

## From source

On a stack deployed from a checkout with `sam deploy`, set `EnableCognitoAuth` in `sam/samconfig.toml`'s `parameter_overrides`, run `sam build && sam deploy`, then rebuild and upload the UI so `config.js` follows: `cd ui && npm ci && npm run build && ./deploy.sh <stack> <region>`. To run the UI locally against a deployed API, with or without sign-in, see [ui/README.md](../tools/DEVELOPMENT.md#run-the-console-locally-against-a-deployed-api). The code: the console's sign-in in `ui/src/hooks/useAuth.tsx`, `ui/src/components/LoginPage.tsx` and `ui/src/lib/amplifyConfig.ts`; the API's check in `sam/lambdas/console_api/auth.py`; the user pool in `sam/template.yaml`.
