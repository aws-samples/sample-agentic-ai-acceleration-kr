"""The runtime rates, and the reason nothing else is here.

The per-token model table was deleted, not moved: for the models this platform
runs, the Price List API publishes nothing, so the table held hand-transcribed
list prices and every figure derived from them was unciteable. What remains is
the pair the Price List API does publish, and Cost Explorer bills the same tier —
which is why an estimate built on these two numbers can be checked and the model
one could not.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data.model_prices as model_prices  # noqa: E402
from data.model_prices import (  # noqa: E402
    RUNTIME_GB_HOUR_USD,
    RUNTIME_VCPU_HOUR_USD,
)


def test_runtime_rates_match_the_measured_price_list():
    assert RUNTIME_VCPU_HOUR_USD == 0.0895
    assert RUNTIME_GB_HOUR_USD == 0.00945


def test_no_per_token_price_table_comes_back_unnoticed():
    """A reinstated table needs rates read from the API, not transcribed ones.

    Pinned as a test because the failure mode is silent: a plausible-looking
    dict of per-million rates renders as a dollar figure that nobody can trace
    to a tariff, which is exactly what was on the dashboard.
    """
    assert not hasattr(model_prices, "MODEL_PRICES")
    assert not hasattr(model_prices, "price_for")
