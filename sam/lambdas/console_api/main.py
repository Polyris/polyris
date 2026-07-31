"""
Pipeline Console API - Lambda Handler

Serverless API for pipeline management. Routes requests to appropriate handlers.

NOTE: This is the refactored entry point (main.py).
All business logic has been moved to route modules in routes/.
See HANDLERS_REFACTORING_PLAN.md for details.
"""

from typing import Dict

from response import cors_response, error_response
from logger import log

# Auth gate (ADR #65)
from auth import authenticate, authorize, is_public_path, is_auth_enabled, AuthError, AuthzError
from dal import api_tokens_repo
from routing import Router

# OSS route modules (ADR #97). main.py is the runner: each module exposes
# register(router) and adds its own routes. This list is the open-core API
# surface; the proprietary `ee` package appends its modules below (ADR #98).
from routes import (
    health,
    pipelines_list, pipelines_info, pipelines_actions,
    tasks, executions,
    notifications,
    settings,
)

ROUTE_MODULES = [
    health,
    pipelines_list, pipelines_info, pipelines_actions,
    tasks, executions,
    notifications,
    settings,
]

# Proprietary route modules (Team tier) live in console_api/ee/ and ship inside
# this Lambda's CodeUri. The OSS build strips ee/ (ADR #98) → ImportError → free
# routes only. The AttributeError guard covers the case where a *different*
# top-level `ee` (e.g. the SDK's `ee` package) is on the path without our
# MODULES: treat that as "no backend ee" rather than crashing. In the Lambda only
# console_api/ee is present, so this resolves to our package.
try:
    import ee
    ROUTE_MODULES += ee.MODULES
except (ImportError, AttributeError):
    pass


# =============================================================================
# Route Table (ADR #97)
# =============================================================================
# Built by running each route module's register(router); the runner iterates the
# explicit ROUTE_MODULES list above. ROUTES is the resulting
# (METHOD, path) -> (handler, param_key) mapping; dispatch below is unchanged.
#   param_key = None     → handler(event)
#   param_key = 'name'   → handler(params['name'], event)  (with required check)
#   param_key = 'id'     → handler(params['id'], event)    (with required check)
router = Router()
for _module in ROUTE_MODULES:
    _module.register(router)
ROUTES = router.table


def handler(event: Dict, context) -> Dict:
    """Main Lambda handler - routes requests to appropriate function."""
    
    # Extract request_id for error tracking
    request_id = event.get('requestContext', {}).get('requestId') or getattr(context, 'aws_request_id', None)
    
    # Handle CORS preflight
    if event.get('requestContext', {}).get('http', {}).get('method') == 'OPTIONS':
        return cors_response(200, {})
    
    path = event.get('rawPath', '')
    # Strip stage prefix if present (e.g. /dev/api/health -> /api/health)
    # HTTP API v2 with named stage includes stage name in rawPath
    stage_name = event.get('requestContext', {}).get('stage', '')
    if stage_name and stage_name != '$default' and path.startswith(f'/{stage_name}'):
        path = path[len(f'/{stage_name}'):]
    method = event.get('requestContext', {}).get('http', {}).get('method', 'GET')
    params = event.get('queryStringParameters', {}) or {}

    # Auth gate (ADR #65). On by default via AUTH_ENABLED (the template sets it
    # true); disabling enforcement is a deliberate, reversible step. Health/
    # metrics are public.
    if is_auth_enabled() and not is_public_path(path):
        try:
            principal = authenticate(event, api_tokens_repo)
            event['principal'] = principal
            authorize(principal, method, path)  # scope check (ADR #66) -> 403
        except AuthError as e:
            return error_response(401, 'UNAUTHORIZED', str(e), request_id=request_id)
        except AuthzError as e:
            return error_response(403, 'FORBIDDEN', str(e), request_id=request_id)

    try:
        route = ROUTES.get((method, path))
        
        if route is None:
            return error_response(404, 'NOT_FOUND', f'Route not found: {method} {path}', request_id=request_id)
        
        handler_fn, param_key = route
        
        if param_key is not None:
            param_value = params.get(param_key, '')
            if not param_value:
                return error_response(400, 'MISSING_PARAM', f'{param_key} parameter required')
            return handler_fn(param_value, event)
        else:
            return handler_fn(event)
    
    except Exception as e:
        import traceback
        log.error("handler", str(e), path=path, method=method, traceback=traceback.format_exc())
        return error_response(500, 'INTERNAL_ERROR', str(e), request_id=request_id)
