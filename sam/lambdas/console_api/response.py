"""
Console API Response Helpers

Response formatting utilities for API Gateway:
- cors_response: JSON response with CORS headers
- error_response: Standardized error response format
- html_response: HTML response for Slack button callbacks
"""

import json
from typing import Dict, Any


def cors_response(status_code: int, body: Any) -> Dict:
    """
    Return response with CORS headers and no-cache directives.
    
    Args:
        status_code: HTTP status code
        body: Response body (will be JSON serialized)
    
    Returns:
        API Gateway response dict with CORS headers
    """
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type, Authorization, Cache-Control',
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma': 'no-cache',
            'Expires': '0'
        },
        'body': json.dumps(body, default=str)
    }


def error_response(
    status_code: int,
    error: str,
    message: str = None,
    details: Dict = None,
    request_id: str = None
) -> Dict:
    """
    Return standardized error response.
    
    Standard error format:
    {
        "error": "ERROR_CODE",
        "message": "Human readable message",
        "details": {...},  // optional additional info
        "request_id": "..."  // optional, for debugging
    }
    
    Common status codes:
    - 400: Bad Request (validation errors)
    - 404: Not Found
    - 409: Conflict (resource already exists)
    - 500: Internal Server Error
    
    Args:
        status_code: HTTP status code
        error: Error code (e.g., "VALIDATION_ERROR", "NOT_FOUND")
        message: Human-readable error message
        details: Optional additional error details
        request_id: Optional request ID for debugging
    
    Returns:
        API Gateway response dict with standardized error format
    """
    body = {'error': error}
    
    if message:
        body['message'] = message
    
    if details:
        body['details'] = details
    
    if request_id:
        body['request_id'] = request_id
    
    return cors_response(status_code, body)


def safe_parse_body(event: Dict) -> tuple:
    """
    Safely parse JSON body from API Gateway event.
    
    Returns (body_dict, error_response_or_None).
    On success: ({...}, None)
    On missing body: ({}, None)
    On malformed JSON: (None, 400 error response)
    
    Usage:
        body, err = safe_parse_body(event)
        if err:
            return err
    """
    raw = event.get('body')
    if not raw:
        return {}, None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None, error_response(400, 'INVALID_JSON', 'Request body is not valid JSON')
    if not isinstance(parsed, dict):
        # Syntactically valid JSON (an array, string, number, or null) is not
        # a usable request body — every caller immediately does body.get(...),
        # which would otherwise raise AttributeError deep in the route handler.
        return None, error_response(400, 'INVALID_JSON', 'Request body must be a JSON object')
    return parsed, None


def validation_error(field: str, reason: str, value: Any = None) -> Dict:
    """
    Return 400 validation error response.
    
    Args:
        field: Field name that failed validation
        reason: Reason for validation failure
        value: Optional invalid value (will be stringified)
    
    Returns:
        API Gateway 400 response
    """
    details = {'field': field, 'reason': reason}
    if value is not None:
        details['received'] = str(value)[:100]  # Truncate long values
    
    return error_response(
        400,
        'VALIDATION_ERROR',
        f"Invalid {field}: {reason}",
        details
    )


def not_found_error(resource_type: str, resource_id: str) -> Dict:
    """
    Return 404 not found error response.
    
    Args:
        resource_type: Type of resource (e.g., "task", "pipeline")
        resource_id: ID of the missing resource
    
    Returns:
        API Gateway 404 response
    """
    return error_response(
        404,
        'NOT_FOUND',
        f"{resource_type.title()} not found: {resource_id}",
        {'resource_type': resource_type, 'resource_id': resource_id}
    )


def html_response(status_code: int, title: str, message: str, icon: str = "✅", success: bool = True) -> Dict:
    """
    Return HTML response for Slack button clicks.
    
    Args:
        status_code: HTTP status code
        title: Page title and heading
        message: Body message to display
        icon: Emoji icon to display (default: ✅)
        success: If True, use green theme; if False, use red theme
    
    Returns:
        API Gateway response dict with HTML body
    """
    bg_color = "#f0fdf4" if success else "#fef2f2"
    text_color = "#166534" if success else "#991b1b"
    
    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>{title}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; 
               display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0;
               background: {bg_color}; }}
        .card {{ background: white; padding: 2rem 3rem; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.1);
                 text-align: center; max-width: 400px; }}
        .icon {{ font-size: 3rem; margin-bottom: 1rem; }}
        .title {{ font-size: 1.5rem; font-weight: 600; color: {text_color}; margin-bottom: 0.5rem; }}
        .message {{ color: #6b7280; line-height: 1.5; }}
        .close {{ margin-top: 1.5rem; padding: 0.75rem 2rem; background: #3b82f6; color: white; 
                  border: none; border-radius: 8px; cursor: pointer; font-size: 1rem; }}
        .close:hover {{ background: #2563eb; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">{icon}</div>
        <div class="title">{title}</div>
        <div class="message">{message}</div>
        <button class="close" onclick="window.close()">Close Window</button>
    </div>
</body>
</html>"""
    
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'text/html',
            'Access-Control-Allow-Origin': '*'
        },
        'body': html
    }
