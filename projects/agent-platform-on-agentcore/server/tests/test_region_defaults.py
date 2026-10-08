"""Where a region default comes from when a caller does not pass one.

Every literal `"us-east-1"` in a default argument is a silent override of
`AWS_REGION` / `COGNITO_REGION`. The failure it produces is not "wrong region"
but "resource does not exist": DynamoDB answers ResourceNotFoundException for a
table that is right there in the configured region, and Cognito does the same
for the user pool. Both read as a broken deployment rather than a wrong default,
which is why these two defaults are pinned here rather than left to review.

The values are not asserted to be a specific region — they are asserted to be
*the same object the config module resolved*, so this keeps holding when the
deployment moves.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import inspect  # noqa: E402

from core import config  # noqa: E402
from models.mcp import CognitoLoginRequest  # noqa: E402
from repositories.base import DynamoDBRepository  # noqa: E402


def test_repository_region_defaults_to_the_configured_region():
    """Every production caller passes region_name=AWS_REGION; the default is the
    trap for the one that forgets, so it has to agree with them."""
    default = inspect.signature(DynamoDBRepository.__init__).parameters[
        "region_name"
    ].default

    assert default == config.AWS_REGION


def test_cognito_login_region_defaults_to_the_configured_cognito_region():
    """A request that omits `region` must reach the pool the server is
    configured for, not us-east-1."""
    request = CognitoLoginRequest(client_id="anything")

    assert request.region == config.COGNITO_REGION


def test_an_explicit_region_still_wins():
    """The field stays an override — the MCP page lets an admin point at a pool
    in another account's region."""
    request = CognitoLoginRequest(client_id="anything", region="eu-west-1")

    assert request.region == "eu-west-1"


def test_neither_default_is_a_hardcoded_literal():
    """Guards the regression directly: both defaults used to be written out as
    "us-east-1" in the source, so setting AWS_REGION changed nothing."""
    for module in ("repositories/base.py", "models/mcp.py"):
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), module)
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        assert '= "us-east-1"' not in source, f"{module} still hardcodes a region"
