#!/bin/bash

# Deploy script for Bedrock AgentCore Runtime using CLI commands

set -e  # Exit on any error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Pull the gateway URL from Terraform outputs when it is still empty after loading
# .env. Explicit .env or inline vars always win —
# this only fills the blanks. Best-effort: no terraform, uninitialised state, or a
# missing output just leaves the value empty, and the runtime falls back to local
# tools only (no hard failure). Opt out with AUTO_TF_OUTPUTS=false; point at a
# different stack with INFRA_ENV_DIR.
autofill_from_terraform() {
    [[ "${AUTO_TF_OUTPUTS:-true}" == "true" ]] || return 0

    # MCP_GATEWAY_URL already set? skip the terraform call entirely.
    if [[ -n "$MCP_GATEWAY_URL" ]]; then
        return 0
    fi

    local tf_dir="${INFRA_ENV_DIR:-$(dirname "$0")/../../infra/envs/standalone}"

    if ! command -v terraform &>/dev/null; then
        print_warning "terraform not found; leaving gateway values as-is (set MCP_GATEWAY_URL manually to use the gateway)"
        return 0
    fi
    if [[ ! -d "$tf_dir" ]]; then
        print_warning "infra dir not found ($tf_dir); skipping gateway output autofill"
        return 0
    fi

    print_status "Autofilling empty gateway URL from Terraform outputs ($tf_dir)"

    # var name here -> terraform output name. -raw so a single value prints bare;
    # 2>/dev/null so an absent output/state is a silent skip, not a crash.
    local val
    if [[ -z "$MCP_GATEWAY_URL" ]]; then
        val="$(terraform -chdir="$tf_dir" output -raw mcp_gateway_url 2>/dev/null)" \
            && [[ -n "$val" ]] && export MCP_GATEWAY_URL="$val"
    fi

    # Always succeed: a failed `terraform output` above must not trip `set -e` and
    # abort the deploy — a runtime with local tools only is the intended fallback.
    return 0
}

# Function to load configuration from .env file
load_config() {
    local env_file="$(dirname "$0")/../.env"

    if [[ -f "$env_file" ]]; then
        print_status "Loading configuration from $env_file"
        # Values passed inline (e.g. AGENT_MODULE=my_agent ./deploy.sh) must win over
        # .env, so preserve any pre-set vars and restore them after sourcing.
        local _pre_agent_name="${AGENT_NAME:-}"
        local _pre_agent_module="${AGENT_MODULE:-}"
        # MODEL_ID too, so each runtime can be deployed on its own model without
        # editing .env between deploys: `AGENT_MODULE=my_agent MODEL_ID=... ./deploy.sh`.
        local _pre_model_id="${MODEL_ID:-}"
        local _pre_max_tokens="${MAX_TOKENS:-}"
        # MEMORY_ID and LONG_TERM_RECALL too: each runtime points at its own
        # memory resource (per-agent session-summary memories), so an inline
        # MEMORY_ID must not be clobbered
        # by whatever single value .env happens to carry.
        local _pre_memory_id="${MEMORY_ID:-}"
        local _pre_long_term_recall="${LONG_TERM_RECALL:-}"
        # Extended-thinking budget differs per runtime, so an inline value must
        # survive .env like the rest.
        local _pre_reasoning_budget="${REASONING_BUDGET:-}"
        set -a
        source "$env_file"
        set +a
        [[ -n "$_pre_agent_name" ]] && export AGENT_NAME="$_pre_agent_name"
        [[ -n "$_pre_agent_module" ]] && export AGENT_MODULE="$_pre_agent_module"
        [[ -n "$_pre_model_id" ]] && export MODEL_ID="$_pre_model_id"
        [[ -n "$_pre_max_tokens" ]] && export MAX_TOKENS="$_pre_max_tokens"
        [[ -n "$_pre_memory_id" ]] && export MEMORY_ID="$_pre_memory_id"
        [[ -n "$_pre_long_term_recall" ]] && export LONG_TERM_RECALL="$_pre_long_term_recall"
        [[ -n "$_pre_reasoning_budget" ]] && export REASONING_BUDGET="$_pre_reasoning_budget"
    else
        print_warning ".env file not found at $env_file, using environment variables"
    fi

    # Set defaults if not provided
    export REGION_NAME="${REGION_NAME:-ap-northeast-1}"
    export MODEL_ID="${MODEL_ID:-global.anthropic.claude-sonnet-5-5}"
    # Which sample agent to deploy (see agents/__init__.py AGENT_REGISTRY).
    export AGENT_MODULE="${AGENT_MODULE:-default}"
    # Derive a per-sample runtime name so each AGENT_MODULE deploys separately —
    # one AgentCore runtime carries one entrypoint, so sharing a name would make
    # the newest deploy silently overwrite whichever sample was there before.
    # `bap` = bedrock agent platform. AgentRuntimeName is [a-zA-Z][a-zA-Z0-9_]{0,47},
    # so the runtime uses the underscore form `bap_` while the infra prefix is the
    # hyphenated `bap-*`; same stem, differing only by the separator the API allows.
    export AGENT_NAME="${AGENT_NAME:-bap_${AGENT_MODULE}}"
    # Cost-allocation tag value applied to the runtime after launch. Matches the
    # server's core.config.PLATFORM (Terraform passes PLATFORM=var.project); the
    # runtime name stem `bap_` shares it. Cost Explorer scopes AgentCore spend to
    # this platform by this tag, so it must equal what the Insights billing query
    # filters on.
    export PLATFORM="${PLATFORM:-bap}"
    export MCP_GATEWAY_URL="${MCP_GATEWAY_URL:-}"
    export MEMORY_ID="${MEMORY_ID:-}"
    # Session-summary long-term recall; off unless this runtime's memory has a
    # SUMMARIZATION strategy (bap_default).
    export LONG_TERM_RECALL="${LONG_TERM_RECALL:-false}"
    # Bedrock Guardrail; empty GUARDRAIL_ID disables input/output filtering.
    export GUARDRAIL_ID="${GUARDRAIL_ID:-}"
    export GUARDRAIL_VERSION="${GUARDRAIL_VERSION:-DRAFT}"
    # Output-token ceiling per model call; raise it per deploy for agents that
    # write long reports.
    export MAX_TOKENS="${MAX_TOKENS:-8192}"
    # Anthropic extended-thinking budget in tokens; 0 (default) leaves thinking
    # off and builds the model as before. Set on bap_default
    # so the model streams reasoning into the UI's reasoning box.
    export REASONING_BUDGET="${REASONING_BUDGET:-0}"
    # Platform server that owns the Agent Registry; empty disables auto-registration.
    export PLATFORM_API_URL="${PLATFORM_API_URL:-}"
    export SKIP_REGISTRY_SYNC="${SKIP_REGISTRY_SYNC:-false}"
    export PLATFORM_ADMIN_USERNAME="${PLATFORM_ADMIN_USERNAME:-}"
    export PLATFORM_ADMIN_PASSWORD="${PLATFORM_ADMIN_PASSWORD:-}"

    # Use environment variable if set, otherwise leave empty for auto-creation
    export EXECUTION_ROLE="${EXECUTION_ROLE:-}"

    # Fill an empty gateway URL from the deployed stack's outputs so a runtime
    # deploy picks up the gateway without hand-copying it.
    autofill_from_terraform

    print_status "Configuration loaded"
    print_status "Region: $REGION_NAME"
    print_status "Model ID: $MODEL_ID"
    print_status "Agent Module: $AGENT_MODULE"
    print_status "Agent Name: $AGENT_NAME"
    print_status "MCP Gateway URL: ${MCP_GATEWAY_URL:-[not configured]}"

    # Validation is optional now since MCP is optional
    if [[ -n "$MCP_GATEWAY_URL" ]]; then
        print_status "MCP server configured, will use MCP tools (gateway calls via SigV4)"
    else
        print_status "No MCP server configured, will use local tools only"
    fi
}

# Function to check prerequisites
check_prerequisites() {
    print_status "Checking prerequisites..."
    
    # Check if agentcore CLI is available
    if ! command -v agentcore &> /dev/null; then
        print_error "agentcore CLI not found. Please install bedrock-agentcore-starter-toolkit:"
        print_error "pip install bedrock-agentcore-starter-toolkit"
        exit 1
    fi
    
    # Check if AWS CLI is configured
    if ! aws sts get-caller-identity &> /dev/null; then
        print_error "AWS CLI not configured or credentials not valid"
        exit 1
    fi
    
    print_success "All prerequisites met"
}

# Function to ensure IAM role has Bedrock permissions
ensure_bedrock_permissions() {
    print_status "Checking IAM permissions for Bedrock access..."

    local role_name="$1"

    if [[ -z "$role_name" ]]; then
        print_warning "No execution role specified, skipping permission check"
        return 0
    fi

    # Extract role name from ARN if provided
    if [[ "$role_name" == arn:aws:iam::* ]]; then
        role_name=$(echo "$role_name" | awk -F'/' '{print $NF}')
    fi

    print_status "Checking role: $role_name"

    # Check if role exists
    if ! aws iam get-role --role-name "$role_name" &>/dev/null; then
        print_warning "Role $role_name not found, will be auto-created"
        return 0
    fi

    # Check if Bedrock inference-profile permissions exist
    local has_inference_profile_access=false

    # Check attached policies for Bedrock
    local attached_policies=$(aws iam list-attached-role-policies --role-name "$role_name" --query 'AttachedPolicies[*].PolicyName' --output text 2>/dev/null)
    if echo "$attached_policies" | grep -q "Bedrock"; then
        # Could have Bedrock access, but may not include inference-profile
        print_status "Found Bedrock attached policy, checking for inference-profile access..."
    fi

    # Check inline policies for inference-profile resource
    local inline_policies=$(aws iam list-role-policies --role-name "$role_name" --query 'PolicyNames' --output text 2>/dev/null)

    # Check each inline policy for inference-profile resource
    for policy_name in $inline_policies; do
        local policy_doc=$(aws iam get-role-policy --role-name "$role_name" --policy-name "$policy_name" --output json 2>/dev/null)
        if echo "$policy_doc" | grep -q "inference-profile"; then
            has_inference_profile_access=true
            print_status "Found inference-profile access in policy: $policy_name"
            break
        fi
    done

    # Also check if our specific policy exists
    if echo "$inline_policies" | grep -q "BedrockMultiRegionAccess"; then
        has_inference_profile_access=true
    fi

    if [[ "$has_inference_profile_access" == false ]]; then
        print_warning "Bedrock inference-profile permissions not found for role $role_name"
        print_status "Creating inline policy for multi-region Bedrock access..."

        # Create inline policy for Bedrock access (JSON must not be indented)
        local policy_name="BedrockMultiRegionAccess"
        local policy_document='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"],"Resource":["arn:aws:bedrock:*:*:inference-profile/*","arn:aws:bedrock:*::foundation-model/*"]}]}'

        print_status "Adding policy: $policy_name"
        if aws iam put-role-policy \
            --role-name "$role_name" \
            --policy-name "$policy_name" \
            --policy-document "$policy_document"; then
            print_success "Added Bedrock multi-region access policy to $role_name"
        else
            print_error "Failed to add Bedrock policy automatically"
            print_warning "Please add manually:"
            print_warning "IAM Role: $role_name"
            print_warning "Policy Name: $policy_name"
            print_warning "Required permissions: bedrock:InvokeModel, bedrock:InvokeModelWithResponseStream"
            print_warning "Resource: arn:aws:bedrock:*:*:inference-profile/* and arn:aws:bedrock:*::foundation-model/*"
            return 1
        fi
    else
        print_success "Bedrock inference-profile permissions already configured"
    fi

    # AWS_IAM 게이트웨이를 SigV4로 호출하려면 실행 역할에 InvokeGateway가 필요하다.
    if ! echo "$inline_policies" | grep -q "AgentCoreGatewayAccess"; then
        local gw_policy='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["bedrock-agentcore:InvokeGateway"],"Resource":["arn:aws:bedrock-agentcore:*:*:gateway/*"]}]}'
        if aws iam put-role-policy --role-name "$role_name" \
            --policy-name "AgentCoreGatewayAccess" \
            --policy-document "$gw_policy"; then
            print_success "Added AgentCore gateway invoke policy to $role_name"
        else
            print_warning "Failed to add gateway policy; add bedrock-agentcore:InvokeGateway on gateway/* manually"
        fi
    fi
}

# Function to configure the agent
configure_agent() {
    print_status "Configuring Bedrock AgentCore Runtime..."
    
    local entrypoint="main.py"
    # load_config resolved this already; re-deriving here risks the two rules drifting.
    local agent_name="$AGENT_NAME"
    local requirements_file="requirements.txt"
    
    # Check if entrypoint exists
    if [[ ! -f "$entrypoint" ]]; then
        print_error "Entrypoint file $entrypoint not found"
        exit 1
    fi
    
    # Check if requirements.txt exists
    if [[ ! -f "$requirements_file" ]]; then
        print_error "Requirements file $requirements_file not found"
        exit 1
    fi
    
    # Configure the agent. `--ecr auto` skips the ECR prompt; the remaining
    # prompts (authorization config, etc.) accept their defaults on a blank line,
    # so feed newlines via `yes ''` to run non-interactively in a background shell.
    if [[ -n "$EXECUTION_ROLE" ]]; then
        print_status "Using execution role from config: $EXECUTION_ROLE"
        yes '' | agentcore configure \
            --entrypoint "$entrypoint" \
            --name "$agent_name" \
            --requirements-file "$requirements_file" \
            --region "$REGION_NAME" \
            --ecr auto \
            --execution-role "$EXECUTION_ROLE"
    else
        print_status "No execution role specified, letting agentcore auto-create"
        yes '' | agentcore configure \
            --entrypoint "$entrypoint" \
            --name "$agent_name" \
            --requirements-file "$requirements_file" \
            --region "$REGION_NAME" \
            --ecr auto
    fi
    
    print_success "Agent configuration completed"
}

# Function to launch the agent
launch_agent() {
    print_status "Launching Bedrock AgentCore Runtime..."
    
    # Export environment variables for the runtime
    export MODEL_ID
    export REGION_NAME
    export AGENT_MODULE
    export MCP_GATEWAY_URL
    export GUARDRAIL_ID
    export GUARDRAIL_VERSION

    # Launch the agent with environment variables
    print_status "Launching with environment variables:"
    print_status "  MODEL_ID=$MODEL_ID"
    print_status "  REGION_NAME=$REGION_NAME"
    print_status "  AGENT_MODULE=$AGENT_MODULE"
    print_status "  MCP_GATEWAY_URL=${MCP_GATEWAY_URL:-[not set]}"

    # Launch with explicit environment variable passing using --env flag.
    # `agentcore configure` rewrites .bedrock_agentcore.yaml and clears the
    # recorded agent_id, so every redeploy looks like a fresh create and
    # conflicts with the live runtime — update it in place instead.
    agentcore launch \
        --auto-update-on-conflict \
        --env MODEL_ID="$MODEL_ID" \
        --env REGION_NAME="$REGION_NAME" \
        --env AGENT_MODULE="$AGENT_MODULE" \
        --env MCP_GATEWAY_URL="$MCP_GATEWAY_URL" \
        --env MEMORY_ID="$MEMORY_ID" \
        --env LONG_TERM_RECALL="$LONG_TERM_RECALL" \
        --env GUARDRAIL_ID="$GUARDRAIL_ID" \
        --env GUARDRAIL_VERSION="$GUARDRAIL_VERSION" \
        --env MAX_TOKENS="$MAX_TOKENS" \
        --env REASONING_BUDGET="$REASONING_BUDGET"
    
    print_success "Agent launch initiated"
}

# Tag the deployed runtime for cost attribution.
#
# AgentCore runtimes are created by the starter toolkit (`agentcore launch`),
# which passes no tags, and the Terraform provider's `default_tags` only reach
# provider-managed resources — so the runtime is born untagged. In a shared
# account (other teams' AgentCore runtimes bill under the same service) the
# Insights "AgentCore 청구액" reads Cost Explorer filtered on `Platform`, so an
# untagged runtime contributes $0 no matter how much it spends. This step is the
# fix: it stamps the runtime so its usage lands in this platform's bucket.
#
# The tag is NOT retroactive — only usage after this call is attributed, and Cost
# Explorer takes up to 24-48h to surface tagged usage. `resourcegroupstaggingapi`
# is the only write path: the bedrock-agentcore-control API exposes no
# TagResource and `create-agent-runtime` accepts no tags. The deployer identity
# needs `tag:TagResources` and `bedrock-agentcore:ListAgentRuntimes`.
tag_runtime() {
    print_status "Tagging runtime for cost attribution (Platform=$PLATFORM, AgentName=$AGENT_NAME)..."

    local arn
    arn=$(aws bedrock-agentcore-control list-agent-runtimes \
        --region "$REGION_NAME" \
        --query "agentRuntimes[?agentRuntimeName=='$AGENT_NAME'].agentRuntimeArn | [0]" \
        --output text 2>/dev/null)

    if [[ -z "$arn" || "$arn" == "None" ]]; then
        print_warning "Could not resolve runtime ARN for '$AGENT_NAME'; skipping cost tags."
        return 0
    fi

    # A tagging failure must never fail the deploy: the runtime is live and
    # serving, and the only loss is cost attribution, which the Insights page
    # already flags on its own.
    local result
    if ! result=$(aws resourcegroupstaggingapi tag-resources \
        --region "$REGION_NAME" \
        --resource-arn-list "$arn" \
        --tags "Platform=$PLATFORM,AgentName=$AGENT_NAME" 2>&1); then
        print_warning "Cost tagging call failed (deploy continues): $result"
        return 0
    fi
    if echo "$result" | grep -q '"ErrorCode"'; then
        print_warning "Cost tagging returned per-resource failures (deploy continues): $result"
    else
        print_success "Runtime tagged for cost attribution: $arn"
    fi
}

# Function to check deployment status
check_status() {
    print_status "Checking deployment status..."

    local max_attempts=30
    local attempt=1

    while [[ $attempt -le $max_attempts ]]; do
        print_status "Status check attempt $attempt/$max_attempts"

        local status_output
        if status_output=$(agentcore status 2>&1); then
            # Check for READY status in the endpoint section
            if echo "$status_output" | grep -q "STATUS: READY"; then
                print_success "Deployment completed successfully!"
                print_success "Agent endpoint is READY"

                # Extract agent info from the status output
                local agent_id=$(echo "$status_output" | grep "Agent ID:" | sed 's/.*Agent ID: //' | tr -d '│ ')
                local agent_arn=$(echo "$status_output" | grep "Agent Arn:" -A 1 | tail -1 | tr -d '│ ')

                if [[ -n "$agent_id" ]]; then
                    print_success "Agent ID: $agent_id"
                fi

                # Extract log group name
                local log_group=$(echo "$status_output" | grep "/aws/bedrock-agentcore/runtimes/" | head -1 | tr -d ' ')
                if [[ -n "$log_group" ]]; then
                    print_status "Logs available at: $log_group"
                fi

                return 0
            elif echo "$status_output" | grep -qi "STATUS:.*FAILED"; then
                print_error "Deployment failed"
                echo "$status_output"
                return 1
            elif echo "$status_output" | grep -qi "STATUS:.*CREATING\|STATUS:.*UPDATING\|STATUS:.*PENDING"; then
                print_status "Current status: Creating/Updating (waiting...)"
                sleep 10
            else
                # Check if agent exists but endpoint is not ready yet
                if echo "$status_output" | grep -q "Agent ID:"; then
                    print_status "Agent created, waiting for endpoint to be ready..."
                    sleep 10
                else
                    print_status "Waiting for agent deployment..."
                    sleep 10
                fi
            fi
        else
            print_warning "Status check failed, retrying in 10 seconds..."
            sleep 10
        fi

        ((attempt++))
    done

    print_error "Deployment status check timed out after $max_attempts attempts"
    return 1
}

# Function to publish the deployed runtime to the Agent Registry
#
# AgentCore has no publish-on-deploy hook, so a runtime deployed here would stay
# invisible to the platform's Registry until someone registered it by hand. The
# platform server owns the descriptor format, so registration goes through its
# sync endpoint by agent name rather than being rebuilt in bash.
register_in_registry() {
    local agent_name="$AGENT_NAME"

    if [[ "${SKIP_REGISTRY_SYNC:-false}" == "true" ]]; then
        print_status "SKIP_REGISTRY_SYNC=true, skipping Agent Registry registration"
        return 0
    fi

    if [[ -z "${PLATFORM_API_URL:-}" ]]; then
        print_warning "PLATFORM_API_URL not set, skipping Agent Registry registration"
        print_warning "Set PLATFORM_API_URL (e.g. http://localhost:8000) in .env, or"
        print_warning "register from the Registry page with the Sync deployed button"
        return 0
    fi

    # The registry sync path now requires an admin token. Failing to get one
    # must be reported as such rather than surfacing later as a vague HTTP error.
    if [[ -z "${PLATFORM_ADMIN_USERNAME:-}" || -z "${PLATFORM_ADMIN_PASSWORD:-}" ]]; then
        print_warning "PLATFORM_ADMIN_USERNAME / PLATFORM_ADMIN_PASSWORD not set, skipping Agent Registry registration"
        print_warning "The sync endpoint requires an admin token. Set both in .env, or"
        print_warning "register from the Registry page with the Sync deployed button"
        return 0
    fi

    local access_token
    # The password goes in on stdin rather than as a curl argument: argv is
    # world-readable through /proc, and this script runs in deploy pipelines.
    access_token=$(printf '{"username":"%s","password":"%s"}' \
        "$PLATFORM_ADMIN_USERNAME" "$PLATFORM_ADMIN_PASSWORD" \
        | curl -sS -m 60 \
            -X POST "${PLATFORM_API_URL%/}/api/auth/login" \
            -H 'Content-Type: application/json' \
            -d @- \
            2>/dev/null | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get("access_token") or "")
except Exception:
    print("")
')

    if [[ -z "$access_token" ]]; then
        print_warning "Could not log in to $PLATFORM_API_URL as $PLATFORM_ADMIN_USERNAME, skipping registration"
        print_warning "Register from the Registry page with the Sync deployed button"
        return 0
    fi

    print_status "Registering '$agent_name' in the Agent Registry via $PLATFORM_API_URL"

    local response http_code body
    # `set -e` is on, so a non-2xx must not abort a deployment that already succeeded.
    if ! response=$(curl -sS -m 120 -w '\n%{http_code}' \
        -X POST "${PLATFORM_API_URL%/}/api/registry/sync" \
        -H 'Content-Type: application/json' \
        -H "Authorization: Bearer $access_token" \
        -d "{\"targets\":[\"$agent_name\"]}" 2>&1); then
        print_warning "Registry sync request failed: $response"
        return 0
    fi

    http_code=$(echo "$response" | tail -1)
    body=$(echo "$response" | sed '$d')

    if [[ "$http_code" != "200" ]]; then
        print_warning "Registry sync returned HTTP $http_code: $body"
        return 0
    fi

    # registered/skipped/failed counts tell apart "newly added" from "already there".
    local counts
    counts=$(echo "$body" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except ValueError:
    print("unparseable")
    sys.exit(0)
print(
    len(data.get("registered") or []),
    len(data.get("skipped") or []),
    len(data.get("failed") or []),
)
' 2>/dev/null || echo "unparseable")

    if [[ "$counts" == "unparseable" ]]; then
        print_warning "Could not parse registry sync response: $body"
        return 0
    fi

    read -r registered skipped failed <<< "$counts"
    if [[ "$registered" -gt 0 ]]; then
        print_success "Registered in Agent Registry (pending approval unless auto-approval is on)"
    elif [[ "$failed" -gt 0 ]]; then
        print_warning "Agent Registry registration failed: $body"
    elif [[ "$skipped" -gt 0 ]]; then
        print_status "Already present in the Agent Registry, nothing to do"
    else
        print_warning "Agent '$agent_name' not found among deployed targets yet"
        print_warning "Retry from the Registry page with the Sync deployed button"
    fi
}

# Function to print deployment summary
print_summary() {
    local agent_name="$AGENT_NAME"
    print_status "=== Deployment Summary ==="
    print_status "Region: $REGION_NAME"
    print_status "Model: $MODEL_ID"
    print_status "Agent Name: $agent_name"
    print_status "Entrypoint: main.py"
    print_status "Configuration: .env"
    if [[ -n "$MCP_GATEWAY_URL" ]]; then
        print_status "MCP Server: Enabled"
    else
        print_status "MCP Server: Disabled (local tools only)"
    fi
}

# Main deployment function
main() {
    print_status "Starting Strands Agent Runtime Deployment..."
    
    # Change to the agent-runtime directory
    cd "$(dirname "$0")/.."
    
    # Load configuration
    load_config
    
    # Check prerequisites
    check_prerequisites
    
    # Check if configuration needs to be updated
    local should_reconfigure=false
    local agent_name="$AGENT_NAME"

    if [[ -f ".bedrock_agentcore.yaml" ]]; then
        print_status "Existing configuration found (.bedrock_agentcore.yaml)"

        # Check if region or agent name has changed
        local current_region=$(grep "region:" .bedrock_agentcore.yaml | head -1 | awk '{print $2}')
        local current_agent=$(grep "default_agent:" .bedrock_agentcore.yaml | awk '{print $2}')

        if [[ "$current_region" != "$REGION_NAME" ]]; then
            print_warning "Region mismatch: config has '$current_region' but .env has '$REGION_NAME'"
            should_reconfigure=true
        fi

        if [[ "$current_agent" != "$agent_name" ]]; then
            print_warning "Agent name mismatch: config has '$current_agent' but .env has '$agent_name'"
            should_reconfigure=true
        fi

        if [[ "$should_reconfigure" == true ]]; then
            print_warning "Configuration needs update. Removing old config..."
            rm .bedrock_agentcore.yaml
            print_status "Running configure with new settings..."
            configure_agent
        else
            print_status "Configuration is up to date, skipping configure step"
        fi
    else
        print_status "No existing configuration found, running configure..."
        configure_agent
    fi

    # Check and update IAM permissions for Bedrock access
    if [[ -f ".bedrock_agentcore.yaml" ]]; then
        local execution_role=$(grep "execution_role:" .bedrock_agentcore.yaml | head -1 | awk '{print $2}')
        if [[ -n "$execution_role" ]]; then
            ensure_bedrock_permissions "$execution_role"
        fi
    fi

    # Launch the agent
    launch_agent
    
    # Check deployment status
    if check_status; then
        # Tag once the runtime exists and is READY, so the ARN resolves.
        tag_runtime
        register_in_registry
        print_summary
        print_success "Deployment completed successfully!"
        exit 0
    else
        print_error "Deployment failed!"
        exit 1
    fi
}

# Run main function
main "$@"
