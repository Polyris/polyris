#!/usr/bin/env bash
#
# get-e2e-token.sh — print a Cognito access token for local E2E test runs.
#
# Why this exists: the E2E suite (tests/e2e/) needs POLYRIS_ID_TOKEN when
# Cognito auth is enabled. polyris uses SRP + admin-only users, so the plain
# `initiate-auth --auth-flow USER_PASSWORD_AUTH` in the e2e README usually
# fails unless that flow is explicitly enabled. ADMIN_USER_PASSWORD_AUTH
# (below) is the reliable admin path.
#
# Usage:
#   export COGNITO_USER_POOL_ID=us-east-1_xxxxx
#   export COGNITO_CLIENT_ID=xxxxxxxxxxxxxxxxxxxxxx
#   export E2E_USERNAME=you@example.com
#   export E2E_PASSWORD='your-password'
#   export POLYRIS_ID_TOKEN="$(scripts/get-e2e-token.sh)"
#   export POLYRIS_API_URL=https://abc123.execute-api.us-east-1.amazonaws.com
#   make e2e-smoke
#
# Requirements on the Cognito side (one-time, see ci/README):
#   - app client must allow ADMIN_USER_PASSWORD_AUTH in ExplicitAuthFlows
#   - the E2E user must have a PERMANENT password (admin-set-user-password
#     --permanent) and MFA disabled, so no auth challenge is returned
#
set -euo pipefail

aws cognito-idp admin-initiate-auth \
  --user-pool-id "${COGNITO_USER_POOL_ID:?set COGNITO_USER_POOL_ID}" \
  --client-id "${COGNITO_CLIENT_ID:?set COGNITO_CLIENT_ID}" \
  --auth-flow ADMIN_USER_PASSWORD_AUTH \
  --auth-parameters "USERNAME=${E2E_USERNAME:?set E2E_USERNAME},PASSWORD=${E2E_PASSWORD:?set E2E_PASSWORD}" \
  --query 'AuthenticationResult.AccessToken' \
  --output text
