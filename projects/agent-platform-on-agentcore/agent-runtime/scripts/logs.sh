#!/bin/bash
# View logs for deployed AgentCore Runtime
# Usage: ./logs.sh [--follow] [--since DURATION]

set -e

# Change to the agent-runtime directory
cd "$(dirname "$0")/.."

# Colors for output
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m' # No Color

print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Parse command line arguments
FOLLOW_FLAG=""
SINCE_FLAG="30m"

while [[ $# -gt 0 ]]; do
    case $1 in
        --follow|-f)
            FOLLOW_FLAG="--follow"
            shift
            ;;
        --since)
            SINCE_FLAG="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [--follow] [--since DURATION]"
            exit 1
            ;;
    esac
done

# Get log group from agentcore status
print_status "Retrieving log group from deployed agent..."

status_output=$(agentcore status 2>&1)

# Extract log group name
LOG_GROUP=$(echo "$status_output" | grep "/aws/bedrock-agentcore/runtimes/" | head -1 | tr -d ' ')

if [[ -z "$LOG_GROUP" ]]; then
    print_error "Could not find log group. Is the agent deployed?"
    print_error "Run './scripts/deploy.sh' to deploy the agent first"
    exit 1
fi

print_status "Log group: $LOG_GROUP"
print_status "Duration: $SINCE_FLAG"
if [[ -n "$FOLLOW_FLAG" ]]; then
    print_status "Following logs (Ctrl+C to stop)..."
fi
echo ""

# Tail logs
aws logs tail "$LOG_GROUP" --since "$SINCE_FLAG" $FOLLOW_FLAG
