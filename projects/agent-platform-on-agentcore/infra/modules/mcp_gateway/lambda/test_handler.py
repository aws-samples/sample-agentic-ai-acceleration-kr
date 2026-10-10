"""The two workshop demo tools return mock data and never call anything."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import handler  # noqa: E402


def test_approve_expense_returns_a_mock_approval_and_echoes_the_amount():
    out = handler._approve_expense({"amount": 4200, "memo": "team offsite"})
    assert out["approved"] is True
    assert out["amount"] == 4200 and out["memo"] == "team offsite"
    assert out["approval_id"].startswith("EXP-")
    assert out["mock"] is True


def test_approve_expense_rejects_non_integer_amounts():
    assert "error" in handler._approve_expense({"amount": "lots"})
    assert "error" in handler._approve_expense({})


def test_lookup_salary_returns_a_band_not_a_number():
    out = handler._lookup_salary({"employee_id": "E-1042"})
    assert out["employee_id"] == "E-1042"
    assert out["band"] in handler._SALARY_BANDS
    assert out["mock"] is True
    assert "error" in handler._lookup_salary({})


def test_tools_are_dispatchable_by_suffix():
    assert handler.TOOLS["approve_expense"] is handler._approve_expense
    assert handler.TOOLS["lookup_salary"] is handler._lookup_salary
