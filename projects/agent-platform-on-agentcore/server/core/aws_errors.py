"""An AWS ClientError as the HTTP status that says whose problem it is.

Every route used to answer any ClientError with 502, which the web reads as "the
server is broken". Most are not: a duplicate name, an edit while the resource is
still CREATING/UPDATING, a second delete while the first is DELETING are the
caller's to retry, a missing resource is a 404, a malformed request a 400, and a
throttle is temporary. Registry conflicts (bst T1 smoke, 2026-10-18) and a
harness deleted twice (same run, s15) both came back as 502.
"""
from botocore.exceptions import ClientError
from fastapi import HTTPException

_THROTTLED = ("ThrottlingException", "TooManyRequestsException", "ServiceQuotaExceededException")
_IN_FLIGHT = ("CREATING", "UPDATING", "DELETING")


def client_error_http(exc: ClientError) -> HTTPException:
    error = exc.response.get("Error", {})
    code = error.get("Code", "Unknown")
    message = str(error.get("Message", str(exc)))
    detail = f"AWS error ({code}): {message}"
    if code == "ConflictException":
        if any(state in message for state in _IN_FLIGHT):
            detail = "아직 처리 중입니다. 잠시 후 다시 시도하세요. " + detail
        return HTTPException(status_code=409, detail=detail)
    if code == "ResourceNotFoundException":
        return HTTPException(status_code=404, detail=detail)
    if code == "ValidationException":
        return HTTPException(status_code=400, detail=detail)
    if code in _THROTTLED:
        return HTTPException(status_code=503, detail=detail, headers={"Retry-After": "2"})
    return HTTPException(status_code=502, detail=detail)
