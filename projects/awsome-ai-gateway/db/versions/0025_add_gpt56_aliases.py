# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""add GPT-5.6 Sol / Terra / Luna aliases + official Bedrock pricing

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-11

Three aliases on the same wire as 0017's ``codex-gpt`` (BEDROCK_MANTLE_OPENAI /
OPENAI_RESPONSES, enum values committed by 0016), so no enum change is needed:

  codex-gpt-5.6-sol    -> openai.gpt-5.6-sol      coding / long-horizon agentic work
  codex-gpt-5.6-terra  -> openai.gpt-5.6-terra    balanced default
  codex-gpt-5.6-luna   -> openai.gpt-5.6-luna     cheapest, high-volume

REGION: us-east-2 (Ohio), matching 0017 and the codex routing profile.

Region choice is constrained from two sides and us-east-2 is the only value that
satisfies both:
  * Sol is available in us-east-1 and us-east-2 ONLY -- NOT us-west-2 (per the
    2026-08-06 model-card region matrix), so any all-three-model region is east.
  * ``local.mantle_regions`` in deployment/terraform/modules/irsa/main.tf is
    ["ap-northeast-1", "us-east-2"], and the IAM statements scope
    bedrock-mantle:CreateInference/GetInference per region. us-east-1 would need a
    terraform apply first; us-east-2 works with the IAM already deployed.
Adding a MODEL needs no IAM change (the resource ARNs are model-wildcarded);
adding a REGION does. Also note the bearer is region-bound via SigV4
(MantleCredentialBroker.bearer_token signs with profile.region), so an alias whose
endpoint_url region differs from routing_profiles.region gets a 401 -- keep them equal.

default_model is deliberately NOT changed. ``routing_profiles.codex.default_model``
stays 'codex-gpt' (GPT-5.5) so an operator whose account cannot yet reach the GPT-5.6
models (IAM ``bedrock-mantle:*`` not yet granted, or the region lacks the model) keeps
a working client; GPT-5.6 is opt-in per request
(``{"model": "codex-gpt-5.6-terra"}``, honored by the /v1/responses per-request
selection in routers/openai_compat.py). Switch the default with:
    UPDATE model.routing_profiles SET default_model = 'codex-gpt-5.6-terra'
     WHERE client = 'codex';
...then flush the Redis routing-profile cache or wait out its 300s TTL:
    DEL routing_profile:codex
The key is ``routing_profile:{client}`` -- NOT ``routing:{client}``. Verified against
services/routing_profile_loader.py:32 (``cache_key = f"routing_profile:{client}"``) and
admin-api services/routing_profile_service.py:20 (``_CACHE_KEY``). Deleting the wrong
key silently no-ops, leaving a stale profile served for up to 5 minutes.

PRICING is the official AWS Bedrock public pricing as of 2026-08-06 (NOT a placeholder,
unlike 0017's codex-gpt row), converted from USD/1M to the table's USD/1k:

  model  | in/1M | cache write/1M | cache read/1M | out/1M
  Sol    |  5.50 |  6.875         | 0.55          | 33.00
  Terra  |  2.20 |  2.75          | 0.22          | 13.20
  Luna   |  0.22 |  0.275         | 0.022         |  1.32

Two properties of these numbers matter for the schema:

1) 5m and 1h cache-write columns get the SAME value. Bedrock publishes ONE cache-write
   rate for the OpenAI models (deck footer: "30M CACHE WRITE / CACHE READ"); the
   5m/1h split in model_pricings exists for Anthropic's two TTLs. Putting a different
   number in the 1h column would invent a rate AWS does not publish. In practice the
   value is unreachable on this path anyway: cost_recorder picks the 1h rate only when
   usage.cache_ttl_1h is true, which is set exclusively in routers/messages.py
   (Anthropic dialect), never on the Responses path.

2) These are the SHORT-context (<=272K input) rates. Bedrock bills a request whose
   input exceeds 272K entirely at the long-context rate (2x input/cache, ~1.5x output),
   which model_pricings cannot express -- it has one rate per alias, with no input-size
   band. Long-context requests are therefore UNDER-billed by this table. Recording the
   short rate is the correct choice for the common case; an operator who expects
   routine >272K prompts should register a separate alias with the long rates.
   Cost is recorded from the provider's own usage object, so tokens are never wrong --
   only the per-token rate applied to them.

3) PRE-EXISTING, NOT introduced here: cached prompt tokens are counted TWICE on this
   dialect. OpenAI's Responses usage.input_tokens ALREADY INCLUDES
   input_tokens_details.cached_tokens, but services/cost_recorder.calculate_cost bills
   input_tokens at the full input rate AND cache_read_input_tokens at the cache-read
   rate on top. A heavily-cached request is therefore over-billed (a 90%-cached Sol
   prompt bills roughly 4.6x the correct amount).

   This is a property of the Anthropic-shaped TokenUsage/pricing model, where
   input_tokens EXCLUDES cache tokens, being reused for OpenAI accounting where it does
   not. It applies identically to 0017's codex-gpt and to every Mantle-OpenAI alias, so
   these three rows neither cause nor worsen it -- they only make it more visible,
   because Sol's rates are ~4.4x codex-gpt's. Fixing it means subtracting cached_tokens
   from the billable input (in the adapter or the cost path), which changes billing for
   an ALREADY-DEPLOYED alias and so is deliberately out of scope for this migration.

reasoning_tokens are billed inside output_tokens (OpenAI accounting), so there is no
separate reasoning price column -- reasoning stays a visibility submetric (see
providers/mantle_openai_adapter._extract_responses_usage).
"""
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

SYSTEM_USER = "00000000-0000-4000-a000-000000000010"
ENDPOINT = "https://bedrock-mantle.us-east-2.api.aws/openai"
EFFECTIVE_FROM = "2026-08-06T00:00:00Z"  # date of the AWS pricing page these rates come from

# (alias, provider_model_id, display_name, description,
#  input/1k, cache_write/1k, cache_read/1k, output/1k)
MODELS = [
    (
        "codex-gpt-5.6-sol",
        "openai.gpt-5.6-sol",
        "Codex · GPT-5.6 Sol",
        "Codex -> 859 Bedrock Mantle GPT-5.6 Sol (Ohio, Responses API) — coding / agentic",
        "0.005500", "0.006875", "0.000550", "0.033000",
    ),
    (
        "codex-gpt-5.6-terra",
        "openai.gpt-5.6-terra",
        "Codex · GPT-5.6 Terra",
        "Codex -> 859 Bedrock Mantle GPT-5.6 Terra (Ohio, Responses API) — balanced",
        "0.002200", "0.002750", "0.000220", "0.013200",
    ),
    (
        "codex-gpt-5.6-luna",
        "openai.gpt-5.6-luna",
        "Codex · GPT-5.6 Luna",
        "Codex -> 859 Bedrock Mantle GPT-5.6 Luna (Ohio, Responses API) — high volume",
        "0.000220", "0.000275", "0.000022", "0.001320",
    ),
]


def upgrade() -> None:
    for alias, model_id, display_name, description, p_in, p_cw, p_cr, p_out in MODELS:
        op.execute(
            f"""
            INSERT INTO model.model_aliases
                (alias, provider, provider_model_id, endpoint_url, api_format, status,
                 description, display_name, created_by)
            VALUES
                ('{alias}', 'BEDROCK_MANTLE_OPENAI', '{model_id}',
                 '{ENDPOINT}', 'OPENAI_RESPONSES', 'ACTIVE',
                 '{description}', '{display_name}', '{SYSTEM_USER}')
            ON CONFLICT (alias) DO NOTHING
            """
        )

        # model_pricings PK is gen_random_uuid(), so ON CONFLICT cannot dedupe;
        # guard on (model_alias, effective_from) like 03_seed_data.sql does. Keying on
        # effective_from (not just the alias) keeps a future price revision insertable.
        op.execute(
            f"""
            INSERT INTO model.model_pricings
                (id, model_alias, input_price_per_1k_tokens, output_price_per_1k_tokens,
                 cache_creation_5m_price_per_1k_tokens, cache_creation_1h_price_per_1k_tokens,
                 cache_read_price_per_1k_tokens, effective_from, created_by)
            SELECT gen_random_uuid(), '{alias}',
                   {p_in}, {p_out}, {p_cw}, {p_cw}, {p_cr},
                   '{EFFECTIVE_FROM}', '{SYSTEM_USER}'
            WHERE NOT EXISTS (
                SELECT 1 FROM model.model_pricings
                 WHERE model_alias = '{alias}' AND effective_from = '{EFFECTIVE_FROM}'
            )
            """
        )


def downgrade() -> None:
    """Remove the three aliases and everything that references them.

    SIX tables carry a FK to model_aliases.alias and NONE of them cascade
    (all ON DELETE NO ACTION, verified against the live catalog):

        model.model_pricings        .model_alias
        model.team_allowed_models   .model_alias      <- grants, written by admin-api
        model.user_allowed_models   .model_alias      <- grants, written by admin-api
        model.rate_limit_configs    .model_alias
        budget.downgrade_policies   .from_model_alias
        budget.downgrade_policies   .to_model_alias

    Deleting only the pricing row is not enough: as soon as an operator grants one of
    these models to a team or user (the normal way to make it usable), downgrade dies
    with ForeignKeyViolationError. Because env.py runs with transaction_per_migration,
    that rollback leaves the DB pinned at 0025 with no way down -- reproduced against
    real Postgres with a single team_allowed_models row.

    These child rows are all configuration ABOUT an alias that is going away, so
    removing them is the correct inverse of upgrade(), not collateral damage. Deletes
    are ordered children-before-parent.

    routing_profiles has no FK (default_model is a plain column), so it is repaired by
    UPDATE rather than DELETE -- dropping an alias that a profile still points at would
    leave a dangling default and 404 every codex request.
    """
    aliases = ", ".join(f"'{m[0]}'" for m in MODELS)

    op.execute(
        f"""
        UPDATE model.routing_profiles
           SET default_model = 'codex-gpt'
         WHERE client = 'codex' AND default_model IN ({aliases})
        """
    )

    for table, column in (
        ("model.model_pricings", "model_alias"),
        ("model.team_allowed_models", "model_alias"),
        ("model.user_allowed_models", "model_alias"),
        ("model.rate_limit_configs", "model_alias"),
        ("budget.downgrade_policies", "from_model_alias"),
        ("budget.downgrade_policies", "to_model_alias"),
    ):
        op.execute(f"DELETE FROM {table} WHERE {column} IN ({aliases})")

    op.execute(f"DELETE FROM model.model_aliases WHERE alias IN ({aliases})")
