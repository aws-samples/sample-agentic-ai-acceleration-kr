"""Tests for guardrail event extraction from streaming metadata.

The fixtures here mirror what Bedrock ConverseStream actually puts on the wire
(captured 2026-09-23 against guardrail 3dod99ztwhr8 with ``trace: enabled``),
not a hand-written approximation. The shape that matters:

    metadata.trace.guardrail.inputAssessment.<guardrailId>            -> assessment dict
    metadata.trace.guardrail.outputAssessments.<guardrailId>          -> LIST of assessment dicts
    assessment.contentPolicy.filters[]                                 -> {type, action, detected, ...}
    assessment.sensitiveInformationPolicy.piiEntities[] / regexes[]
    assessment.topicPolicy.topics[]
    assessment.wordPolicy.customWords[] / managedWordLists[]

The previous version of this file used ``metadata.guardrail``,
``contentPolicyAssessment`` and a dict-valued ``outputAssessment``; none of
those exist in the real response, which is why the Insights guardrail panel
stayed at zero for a month while the tests passed.
"""
import copy

from services.streaming_service import guardrail_events_from_metadata

GR = "3dod99ztwhr8"

# Verbatim (minus token counts) from a live ConverseStream whose input tripped
# the INSULTS content filter. The messageStop carried stopReason
# "guardrail_intervened".
REAL_INPUT_BLOCKED = {
    "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
    "metrics": {"latencyMs": 640},
    "trace": {
        "guardrail": {
            "inputAssessment": {
                GR: {
                    "contentPolicy": {
                        "filters": [
                            {
                                "type": "INSULTS",
                                "confidence": "HIGH",
                                "filterStrength": "HIGH",
                                "action": "BLOCKED",
                                "detected": True,
                            }
                        ]
                    },
                    "invocationMetrics": {
                        "guardrailProcessingLatency": 190,
                        "usage": {"contentPolicyUnits": 1},
                        "guardrailCoverage": {"textCharacters": {"guarded": 67, "total": 67}},
                    },
                    "appliedGuardrailDetails": {
                        "guardrailId": GR,
                        "guardrailVersion": "DRAFT",
                        "guardrailOrigin": ["REQUEST"],
                        "guardrailOwnership": "SELF",
                    },
                }
            },
            "actionReason": "Guardrail blocked.",
        }
    },
}

# Verbatim from a live call that the guardrail scanned but did not act on. Both
# stages are present, output as a list, every filter absent — must yield nothing.
REAL_NO_ACTION = {
    "usage": {"inputTokens": 13, "outputTokens": 5, "totalTokens": 18},
    "metrics": {"latencyMs": 1308},
    "trace": {
        "guardrail": {
            "inputAssessment": {
                GR: {
                    "invocationMetrics": {
                        "guardrailProcessingLatency": 207,
                        "usage": {"contentPolicyUnits": 1},
                        "guardrailCoverage": {"textCharacters": {"guarded": 22, "total": 22}},
                    },
                    "appliedGuardrailDetails": {"guardrailId": GR, "guardrailVersion": "DRAFT"},
                }
            },
            "outputAssessments": {
                GR: [
                    {
                        "invocationMetrics": {
                            "guardrailProcessingLatency": 198,
                            "usage": {"contentPolicyUnits": 1},
                            "guardrailCoverage": {"textCharacters": {"guarded": 6, "total": 6}},
                        },
                        "appliedGuardrailDetails": {"guardrailId": GR, "guardrailVersion": "DRAFT"},
                    }
                ]
            },
            "actionReason": "No action.",
        }
    },
}


def _trace(guardrail: dict) -> dict:
    """Wrap a guardrail assessment the way Bedrock nests it under metadata."""
    return {"usage": {}, "trace": {"guardrail": guardrail}}


def test_real_input_blocked_trace_yields_one_content_event():
    events = guardrail_events_from_metadata(REAL_INPUT_BLOCKED)
    assert events == [{"action": "BLOCKED", "stage": "input", "policies": ["content"],
                       "filter_types": ["INSULTS"], "confidences": ["HIGH"]}]


def test_real_no_action_trace_yields_nothing():
    assert guardrail_events_from_metadata(REAL_NO_ACTION) == []


def test_output_assessments_is_a_list_per_guardrail_id():
    """Bedrock keys outputAssessments by guardrail id and the value is a list —
    one entry per scanned chunk. Each list entry is its own assessment."""
    metadata = _trace(
        {
            "outputAssessments": {
                GR: [
                    {
                        "sensitiveInformationPolicy": {
                            "piiEntities": [
                                {"type": "PHONE", "match": "+1234567890",
                                 "action": "ANONYMIZED", "detected": True}
                            ]
                        }
                    }
                ]
            }
        }
    )
    events = guardrail_events_from_metadata(metadata)
    assert events == [{"action": "ANONYMIZED", "stage": "output", "policies": ["pii"],
                       "filter_types": ["PHONE"], "confidences": []}]


def test_input_and_output_generate_separate_events():
    metadata = _trace(
        {
            "inputAssessment": {
                GR: {
                    "topicPolicy": {
                        "topics": [{"name": "Weapons", "type": "DENY",
                                    "action": "BLOCKED", "detected": True}]
                    }
                }
            },
            "outputAssessments": {
                GR: [
                    {
                        "wordPolicy": {
                            "customWords": [{"match": "badword", "action": "BLOCKED",
                                             "detected": True}]
                        }
                    }
                ]
            },
        }
    )
    events = guardrail_events_from_metadata(metadata)
    assert len(events) == 2
    assert {e["stage"] for e in events} == {"input", "output"}
    assert all(e["action"] == "BLOCKED" for e in events)


def test_all_four_policies_in_one_assessment():
    metadata = _trace(
        {
            "inputAssessment": {
                GR: {
                    "contentPolicy": {"filters": [{"type": "HATE", "action": "BLOCKED", "detected": True}]},
                    "sensitiveInformationPolicy": {
                        "piiEntities": [{"type": "US_SOCIAL_SECURITY_NUMBER", "action": "BLOCKED",
                                         "detected": True}]
                    },
                    "topicPolicy": {"topics": [{"name": "Illegal", "action": "BLOCKED", "detected": True}]},
                    "wordPolicy": {"customWords": [{"match": "slur", "action": "BLOCKED", "detected": True}]},
                }
            }
        }
    )
    events = guardrail_events_from_metadata(metadata)
    assert len(events) == 1
    assert events[0]["action"] == "BLOCKED"
    assert events[0]["policies"] == ["content", "pii", "topic", "word"]


def test_pii_regexes_and_managed_word_lists_count_too():
    """sensitiveInformationPolicy has two lists (piiEntities, regexes) and
    wordPolicy has two (customWords, managedWordLists); any of them can carry
    the action."""
    metadata = _trace(
        {
            "inputAssessment": {
                GR: {
                    "sensitiveInformationPolicy": {
                        "piiEntities": [],
                        "regexes": [{"name": "employee-id", "action": "ANONYMIZED", "detected": True}],
                    },
                    "wordPolicy": {
                        "customWords": [],
                        "managedWordLists": [{"type": "PROFANITY", "action": "BLOCKED", "detected": True}],
                    },
                }
            }
        }
    )
    events = guardrail_events_from_metadata(metadata)
    assert events == [{"action": "BLOCKED", "stage": "input", "policies": ["pii", "word"],
                       "filter_types": ["PROFANITY", "employee-id"], "confidences": []}]


def test_blocked_wins_over_anonymized():
    metadata = _trace(
        {
            "inputAssessment": {
                GR: {
                    "contentPolicy": {"filters": [{"type": "VIOLENCE", "action": "BLOCKED", "detected": True}]},
                    "sensitiveInformationPolicy": {
                        "piiEntities": [{"type": "EMAIL", "action": "ANONYMIZED", "detected": True}]
                    },
                }
            }
        }
    )
    events = guardrail_events_from_metadata(metadata)
    assert events == [{"action": "BLOCKED", "stage": "input", "policies": ["content", "pii"],
                       "filter_types": ["EMAIL", "VIOLENCE"], "confidences": []}]


def test_action_none_is_ignored():
    metadata = _trace(
        {
            "inputAssessment": {
                GR: {"contentPolicy": {"filters": [{"type": "VIOLENCE", "action": "NONE", "detected": False}]}}
            }
        }
    )
    assert guardrail_events_from_metadata(metadata) == []


def test_detected_false_is_ignored_even_with_block_action():
    """Bedrock reports every configured filter with `detected`; only detected
    ones intervened. `action` alone is not enough."""
    metadata = _trace(
        {
            "inputAssessment": {
                GR: {"contentPolicy": {"filters": [{"type": "HATE", "action": "BLOCKED", "detected": False}]}}
            }
        }
    )
    assert guardrail_events_from_metadata(metadata) == []


def test_multiple_output_chunks_each_count():
    """Async stream mode scans output in windows; two flagged windows are two
    interventions, since that's how many times the guardrail acted."""
    chunk = {"contentPolicy": {"filters": [{"type": "SEXUAL", "action": "BLOCKED", "detected": True}]}}
    metadata = _trace({"outputAssessments": {GR: [copy.deepcopy(chunk), {}, copy.deepcopy(chunk)]}})
    events = guardrail_events_from_metadata(metadata)
    assert len(events) == 2
    assert all(e == {"action": "BLOCKED", "stage": "output", "policies": ["content"],
                     "filter_types": ["SEXUAL"], "confidences": []} for e in events)


def test_multiple_guardrail_ids_each_count():
    metadata = _trace(
        {
            "inputAssessment": {
                "gr-1": {"sensitiveInformationPolicy": {"piiEntities": [{"type": "EMAIL", "action": "BLOCKED",
                                                                          "detected": True}]}},
                "gr-2": {"contentPolicy": {"filters": [{"type": "VIOLENCE", "action": "BLOCKED",
                                                        "detected": True}]}},
            }
        }
    )
    events = guardrail_events_from_metadata(metadata)
    assert len(events) == 2
    assert {p for e in events for p in e["policies"]} == {"pii", "content"}


def test_no_trace_or_empty_metadata():
    assert guardrail_events_from_metadata({"usage": {"inputTokens": 10}}) == []
    assert guardrail_events_from_metadata({}) == []
    assert guardrail_events_from_metadata(None) == []


def test_malformed_shapes_return_empty():
    assert guardrail_events_from_metadata({"trace": "invalid"}) == []
    assert guardrail_events_from_metadata({"trace": {"guardrail": "invalid"}}) == []
    assert guardrail_events_from_metadata(_trace({"inputAssessment": {}})) == []
    assert guardrail_events_from_metadata(_trace({"inputAssessment": {GR: "oops"}})) == []
    assert guardrail_events_from_metadata(_trace({"outputAssessments": {GR: "not-a-list"}})) == []
    assert guardrail_events_from_metadata(_trace({"outputAssessments": {GR: [None, 3]}})) == []
    assert guardrail_events_from_metadata(_trace({"inputAssessment": {GR: {"contentPolicy": {"filters": "x"}}}})) == []


# --- what each intervention was, beyond which policy -------------------------


def test_event_names_the_filter_types_that_fired():
    """Bedrock names the filter (INSULTS, PROMPT_ATTACK, ...) — the policy alone
    cannot tell a jailbreak attempt from a rude user. Never the matched text."""
    events = guardrail_events_from_metadata(REAL_INPUT_BLOCKED)
    assert events[0]["filter_types"] == ["INSULTS"]


def test_event_carries_confidence_of_content_filters():
    """Content filters report confidence; a run of LOW-confidence blocks is the
    signal that a filter strength is set too high."""
    events = guardrail_events_from_metadata(REAL_INPUT_BLOCKED)
    assert events[0]["confidences"] == ["HIGH"]


def test_filter_type_labels_per_policy_never_include_matched_text():
    metadata = _trace(
        {
            "inputAssessment": {
                GR: {
                    "sensitiveInformationPolicy": {
                        "piiEntities": [{"type": "EMAIL", "match": "a@b.c", "action": "ANONYMIZED",
                                         "detected": True}],
                        "regexes": [{"name": "employee-id", "match": "E-123", "action": "BLOCKED",
                                     "detected": True}],
                    },
                    "topicPolicy": {"topics": [{"name": "Weapons", "type": "DENY", "action": "BLOCKED",
                                                "detected": True}]},
                    "wordPolicy": {
                        "customWords": [{"match": "secretword", "action": "BLOCKED", "detected": True}],
                        "managedWordLists": [{"match": "damn", "type": "PROFANITY", "action": "BLOCKED",
                                              "detected": True}],
                    },
                }
            }
        }
    )
    [event] = guardrail_events_from_metadata(metadata)
    assert event["filter_types"] == ["CUSTOM_WORD", "EMAIL", "PROFANITY", "Weapons", "employee-id"]
    assert "secretword" not in str(event) and "E-123" not in str(event) and "damn" not in str(event)
    # No content filter fired, so nothing reported a confidence.
    assert event["confidences"] == []


def test_undetected_filters_contribute_no_type_or_confidence():
    metadata = _trace(
        {
            "inputAssessment": {
                GR: {"contentPolicy": {"filters": [
                    {"type": "HATE", "confidence": "LOW", "action": "BLOCKED", "detected": False},
                    {"type": "INSULTS", "confidence": "MEDIUM", "action": "BLOCKED", "detected": True},
                ]}}
            }
        }
    )
    [event] = guardrail_events_from_metadata(metadata)
    assert event["filter_types"] == ["INSULTS"]
    assert event["confidences"] == ["MEDIUM"]


# --- was the guardrail even there ---------------------------------------------


def test_scanned_is_true_whenever_a_guardrail_trace_is_present():
    from services.streaming_service import guardrail_scanned

    assert guardrail_scanned(REAL_NO_ACTION) is True
    assert guardrail_scanned(REAL_INPUT_BLOCKED) is True


def test_scanned_is_false_without_a_trace():
    from services.streaming_service import guardrail_scanned

    assert guardrail_scanned({"usage": {"inputTokens": 3}}) is False
    assert guardrail_scanned({"trace": {}}) is False
    assert guardrail_scanned({}) is False
    assert guardrail_scanned(None) is False


# --- fallback paths for when metadata.trace.guardrail doesn't arrive --------


def test_bedrock_trace_guardrail_path():
    """Bedrock ConverseStream puts the assessment under metadata.trace.guardrail.

    Before the P1 fix the parser only read metadata.guardrail and returned [],
    so Insights stayed at 0 after a visible chat BLOCK. Both shapes must work.
    """
    metadata = {
        "usage": {"inputTokens": 1, "outputTokens": 0, "totalTokens": 1},
        "trace": {
            "guardrail": {
                "inputAssessment": {
                    "kuwzni7qo5a9": {
                        "contentPolicy": {
                            "filters": [
                                {
                                    "type": "HATE",
                                    "confidence": "HIGH",
                                    "action": "BLOCKED",
                                    "detected": True,
                                }
                            ]
                        }
                    }
                }
            }
        },
    }
    events = guardrail_events_from_metadata(metadata)
    assert len(events) == 1
    assert events[0]["action"] == "BLOCKED"
    assert events[0]["stage"] == "input"
    assert "content" in events[0]["policies"]


def test_flat_guardrail_still_preferred_over_empty_trace():
    """Harness-style flat metadata.guardrail must keep working after the P1 fix."""
    metadata = {
        "guardrail": {
            "inputAssessment": {
                "gid": {
                    "contentPolicy": {
                        "filters": [{"type": "HATE", "action": "BLOCKED", "detected": True}]
                    }
                }
            }
        },
        "trace": {},
    }
    events = guardrail_events_from_metadata(metadata)
    assert len(events) == 1
    assert events[0]["action"] == "BLOCKED"


def test_is_guardrail_intervened_stop():
    """Strands BLOCK ends with stopReason=guardrail_intervened (P2 fallback key)."""
    from services.streaming_service import is_guardrail_intervened_stop

    assert is_guardrail_intervened_stop("guardrail_intervened") is True
    assert is_guardrail_intervened_stop("guardrailIntervened") is True
    assert is_guardrail_intervened_stop("GUARDRAIL_INTERVENED") is True
    assert is_guardrail_intervened_stop("end_turn") is False
    assert is_guardrail_intervened_stop(None) is False
    assert is_guardrail_intervened_stop("") is False


def test_guardrail_stage_from_blocked_text():
    """Exact Guardrail blockedMessaging strings map to input/output stages."""
    from services.streaming_service import (
        _GUARDRAIL_BLOCKED_INPUT_MSG,
        _GUARDRAIL_BLOCKED_OUTPUT_MSG,
        guardrail_stage_from_blocked_text,
    )

    assert guardrail_stage_from_blocked_text(_GUARDRAIL_BLOCKED_INPUT_MSG) == "input"
    assert guardrail_stage_from_blocked_text(
        f"  {_GUARDRAIL_BLOCKED_OUTPUT_MSG}  "
    ) == "output"
    assert guardrail_stage_from_blocked_text("hello") is None
    assert guardrail_stage_from_blocked_text(None) is None
