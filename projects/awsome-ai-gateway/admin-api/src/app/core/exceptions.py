# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations


class AppError(Exception):
    """Base application error."""

    def __init__(self, message: str, code: str = "internal_error"):
        self.message = message
        self.code = code
        super().__init__(message)


class NotFoundError(AppError):
    def __init__(self, resource: str, identifier: str):
        super().__init__(
            message=f"{resource} not found: {identifier}",
            code="not_found",
        )


class ConflictError(AppError):
    def __init__(self, message: str):
        super().__init__(message=message, code="conflict")


class ForbiddenError(AppError):
    def __init__(self, message: str = "Insufficient permissions"):
        super().__init__(message=message, code="forbidden")


class ValidationError(AppError):
    def __init__(self, message: str):
        super().__init__(message=message, code="validation_error")


class BudgetExceededError(AppError):
    def __init__(self, message: str = "Budget limit exceeded"):
        super().__init__(message=message, code="budget_exceeded")


class STSVerificationError(AppError):
    def __init__(self, message: str = "STS identity verification failed"):
        super().__init__(message=message, code="sts_verification_error")
