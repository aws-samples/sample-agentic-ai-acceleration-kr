"""Price list for cost estimation.

**Runtime rates only, and they are measured.** The AWS Price List API
(`AmazonBedrockAgentCore`, us-east-1, effective 2026-08-01) publishes
`Runtime:Consumption-based:vCPU` at $0.0895/vCPU-hour and
`Runtime:Consumption-based:Memory` at $0.00945/GB-hour. CodeInterpreter and
BrowserTool carry the same two rates. Cost Explorer bills the same tier under
`…Runtime:Consumption-based:vCPU`, so an estimate from these rates has a billed
twin to be checked against — which the insights page does.

**Per-token model rates used to live here and no longer do.** The same API's
`model` attribute enumerates only pre-4.x Claude models, so for the models this
platform actually runs there was nothing to read and the table held Anthropic's
published list prices, transcribed by hand. Bedrock is partner-operated, prompt
caching bills input tokens at three different rates, and the usage table records
one undifferentiated input count — so "모델 비용" was a figure with no source it
could cite and an error bar nobody could state. It was removed from the API and
the dashboard rather than labelled more loudly. Bring it back only with rates
read from the Price List API and a token count that separates the cache tiers.
"""

# Measured from the AWS Price List API — see the module docstring.
RUNTIME_VCPU_HOUR_USD = 0.0895
RUNTIME_GB_HOUR_USD = 0.00945
