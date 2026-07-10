# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""OIDC client for gateway-cli (PKCE + token cache + JWT→VK exchange).

Generic OIDC — works with any OIDC-compliant IDP (Keycloak / Cognito / Okta / Azure AD / IC).
Standard-library only (httpx/requests) — no authlib dependency.
"""
