"""
Standardised API response helpers for Huddle.

Usage:
    from helpers.response import success_response, error_response, list_response

TARGET RESPONSE FORMAT (all new endpoints MUST use this):
    Success (single item):
        {"success": true, "data": {...}, "message": "Success"}

    Success (list):
        {"success": true, "data": [...], "total": 42}

    Error:
        {"success": false, "error": "Human-readable message", "error_id": "optional-code"}

    HTTP status codes should be set via JSONResponse (error_response handles this).
    The "success" boolean lets clients branch without checking status codes.

DEPRECATION NOTICE — legacy response patterns:
    Many existing endpoints return domain-specific keys instead of wrapping in
    the standard envelope, e.g.:
        {"chores": [...]}
        {"tasks": [...], "completed": [...]}
        {"ok": true, "message": "..."}

    These patterns are DEPRECATED for new development. Existing endpoints will
    be migrated gradually — each migration requires updating the corresponding
    JavaScript in mobile.html, kiosk.html, and any other consuming templates.
    See the migration checklist below.

    When migrating an existing endpoint:
    1. Update the endpoint to use these helpers
    2. Update ALL JavaScript fetch calls in mobile.html, kiosk.html, and any other templates
       that consume that endpoint
    3. Test both kiosk and mobile interfaces
"""

from fastapi.responses import JSONResponse


def success_response(data=None, message="Success"):
    """Return a standardised success response.

    Target format: {"success": true, "data": <any>, "message": "Success"}
    """
    return {"success": True, "data": data, "message": message}


def error_response(message="Something went wrong", status_code=500, error_id=None):
    """Return a standardised error response as a JSONResponse with status code.

    Target format: {"success": false, "error": "...", "error_id": "..."}
    """
    content = {"success": False, "error": message}
    if error_id:
        content["error_id"] = error_id
    return JSONResponse(status_code=status_code, content=content)


def list_response(items, total=None):
    """Return a standardised list response.

    Target format: {"success": true, "data": [...], "total": <int>}
    """
    return {"success": True, "data": items, "total": total if total is not None else len(items)}
