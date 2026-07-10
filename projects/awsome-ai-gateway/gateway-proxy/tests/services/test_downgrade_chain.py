# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from app.services.downgrade_loader import DowngradeRule, apply_chain


def make(rules):
    return [DowngradeRule(*r) for r in rules]


def test_no_rules_returns_original():
    assert apply_chain("opus", [], current_pct=99)[0] == "opus"


def test_threshold_not_met_returns_original():
    rules = make([("opus", "sonnet", 80)])
    assert apply_chain("opus", rules, current_pct=70)[0] == "opus"


def test_single_match_applied():
    rules = make([("opus", "sonnet", 80)])
    assert apply_chain("opus", rules, current_pct=85)[0] == "sonnet"


def test_chain_applied_iteratively():
    rules = make(
        [
            ("opus", "sonnet", 70),
            ("sonnet", "haiku", 90),
        ]
    )
    result, hops = apply_chain("opus", rules, current_pct=95)
    assert (result, hops) == ("haiku", 2)


def test_chain_stops_when_no_further_rule():
    rules = make(
        [
            ("opus", "sonnet", 70),
            ("sonnet", "haiku", 99),  # current_pct=80 < 99, 멈춤
        ]
    )
    assert apply_chain("opus", rules, current_pct=80)[0] == "sonnet"


def test_cycle_prevented_by_visited_set():
    rules = make(
        [
            ("opus", "sonnet", 50),
            ("sonnet", "opus", 50),
        ]
    )
    # opus -> sonnet (visited={opus,sonnet}) -> next would be opus, blocked
    assert apply_chain("opus", rules, current_pct=99)[0] == "sonnet"


def test_max_depth_safety():
    # 6단 체인 (max_depth=5)이면 5번까지만
    rules = make(
        [
            ("a", "b", 1),
            ("b", "c", 1),
            ("c", "d", 1),
            ("d", "e", 1),
            ("e", "f", 1),
            ("f", "g", 1),
        ]
    )
    assert apply_chain("a", rules, current_pct=99, max_depth=5)[0] == "f"


def test_no_match_for_unknown_alias():
    rules = make([("opus", "sonnet", 50)])
    assert apply_chain("haiku", rules, current_pct=99)[0] == "haiku"
