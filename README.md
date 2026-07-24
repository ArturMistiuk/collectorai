# Corafone Collections Voice Agent

AI voice agent for debt repayment negotiation. Calls a consumer 180+ days delinquent on a $1,000 balance and closes the highest-value agreement using a 4-tier payment waterfall.

**Try it:** call **+1 (517) 514-5413** (inbound, US number).

## How it works

```
Consumer <-- phone --> Vapi (STT / LLM / TTS)
                          |
                          |-- validate_offer   --> FastAPI (waterfall logic)
                          |-- check_compliance --> FastAPI (compliance filter)
                          |-- log_agreement    --> FastAPI (store deal)
                          |-- log_outcome      --> FastAPI (dispute / DNC)
                          |
                          '-- call-ended webhook --> FastAPI (transcript + outcome)
```

The LLM handles the conversation. **All business decisions happen in code:**

- `validate_offer` checks every proposal against the waterfall rules and returns ACCEPT or COUNTER with corrected terms. The LLM cannot accept an offer the backend rejected.
- `check_compliance` screens every sentence involving consequences or urgency against a regex blocklist. Flagged phrases are blocked before the consumer hears them.

## Payment waterfall

| Tier | Total | Payments | Discount |
|------|-------|----------|----------|
| 1. Full payment | $1,000 | 1 | None |
| 2. Downpayment + one | $1,000 | 2 | None |
| 3. Settlement | $800+ | up to 3 | up to 20% |
| 4. Payment plan | $1,000 | up to 4 | None, max 3 months |

Every individual payment must be at least **25% of the agreed total**. This is enforced in `validation.py`, not the prompt.

## Compliance guardrails

Regex filter blocks phrases before they reach the consumer:

- Threats: jail, arrest, sue, lawsuit, court, criminal, warrant, police
- False consequences: wage garnish, seize, repossess, destroy/ruin your credit
- False urgency: "only today", "last chance", "final warning", "within the next X minutes"

Every blocked phrase is logged to `compliance_flags` for audit.

## Project structure

```
app/
  main.py              FastAPI server (6 endpoints)
  validation.py         Waterfall logic (pure Python, no LLM)
  system-prompt.md      Voice agent instructions
  tools.json            Vapi tool definitions (4 tools)
  test_validation.py    Unit tests for waterfall logic
  test_api.py           Integration tests for all endpoints
  test_scenarios.md     Manual call test checklist
  requirements.txt      Python dependencies
Dockerfile              Container config for Railway
```

## Running locally

```bash
cd app
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

## Tests

```bash
cd app
python test_validation.py        # 10 unit tests
pytest test_api.py -v            # 13 integration tests
```

## Deployment

Deployed on Railway via Docker. Environment variables:
- `DATABASE_PATH` -- SQLite file location (default: `./corafone.db`)
- `LOG_LEVEL` -- logging verbosity (default: `INFO`)

## Key decisions

1. **Validation outside the LLM.** The 25% payment floor, 20% discount cap, and tier waterfall are plain Python in `validation.py`, unit-tested independently. The LLM gathers numbers from the conversation and relays the tool's decision -- it has no path to accept something the backend rejected.

2. **Compliance as a callable guardrail.** Instead of relying on prompt instructions alone, `check_compliance` runs a server-side regex filter before any sentence involving consequences reaches the consumer. Every flagged attempt is logged for audit. The prompt can be ignored; the regex cannot.

3. **Settlement as a fallback, not the opener.** The agent tries full payment and downpayment tiers first. Settlement is only offered once the consumer signals $1,000 isn't workable -- this maximizes deal value without starting from a discount position.
