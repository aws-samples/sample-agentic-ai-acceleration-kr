"""Registry-off harness composition can still attach uploaded skills.

When the registry is off there are no AGENT_SKILLS records to pick from, but the
skill bundles a user uploaded still live under `s3://SKILLS_BUCKET/skills/<name>/`.
The catalog surfaces them so the compose UI can offer the same skill picker it
shows registry-on, and compose attaches the chosen ones by their S3 prefix — the
one skill source the harness API takes that needs no registry at all.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import services.harness_service as hs  # noqa: E402
from models.harness import ComposeHarnessRequest  # noqa: E402
from services.harness_service import HarnessNotConfigured  # noqa: E402
from services.skill_bundle_service import DiscoveredSkill  # noqa: E402

BUCKET = "skills-bucket"


class RecordingControl:
    def __init__(self):
        self.params = None

    def create_harness(self, **params):
        self.params = params
        return {
            "harness": {
                "harnessId": "h1",
                "arn": "arn:aws:bedrock-agentcore:us-east-1:1:harness/h1",
                "harnessName": params["harnessName"],
                "status": "CREATING",
            }
        }


def _service(skills_bucket=BUCKET):
    svc = hs.HarnessService(
        registry=object(),
        region="us-east-1",
        execution_role_arn="arn:aws:iam::1:role/harness",
        skills_bucket=skills_bucket,
    )
    svc._control = RecordingControl()
    svc._resolve_mcp_tools = lambda ids: []
    # Isolate the bucket-skill path: the registry stub has no get_record, and
    # _resolve_skills touches it even for empty inputs.
    svc._resolve_skills = lambda record_ids, paths: []
    return svc


def test_catalog_surfaces_bucket_skills(monkeypatch):
    monkeypatch.setattr(hs, "registry_enabled", lambda: False)
    svc = _service()
    monkeypatch.setattr(
        svc,
        "_list_bucket_skills",
        lambda: [
            DiscoveredSkill(
                name="Weather", description="forecasts",
                uri=f"s3://{BUCKET}/skills/weather/",
            )
        ],
    )
    cat = svc.catalog()
    assert [s.name for s in cat.bucket_skills] == ["Weather"]
    assert cat.bucket_skills[0].uri == f"s3://{BUCKET}/skills/weather/"


def test_compose_attaches_bucket_skills_by_s3_uri():
    svc = _service()
    uri = f"s3://{BUCKET}/skills/weather/"
    svc.create_harness(
        ComposeHarnessRequest(name="agent", skill_bucket_uris=[uri])
    )
    skills = svc._control.params.get("skills")
    assert {"s3": {"uri": uri}} in skills


def test_compose_bucket_skill_uri_must_be_in_our_bucket():
    svc = _service()
    with pytest.raises(ValueError):
        svc.create_harness(
            ComposeHarnessRequest(
                name="agent",
                skill_bucket_uris=["s3://someone-elses-bucket/skills/x/"],
            )
        )


def test_compose_bucket_skills_need_a_configured_bucket():
    # "" (not None) so the constructor keeps it unset instead of falling back to
    # the SKILLS_BUCKET env default.
    svc = _service(skills_bucket="")
    with pytest.raises(HarnessNotConfigured):
        svc.create_harness(
            ComposeHarnessRequest(
                name="agent", skill_bucket_uris=["s3://b/skills/x/"]
            )
        )
