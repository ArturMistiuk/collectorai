# Corafone Collections Voice Agent

An AI voice agent that calls a consumer 180+ days delinquent on a $1,000 debt
and negotiates the highest-value agreement they'll actually honor, using a
strict 4-tier waterfall -- with the negotiation math and compliance checks
enforced by code outside the LLM, not by prompt instructions alone.

## Architecture

```
Consumer <--phone--> Vapi (STT/LLM/TTS + telephony)
                         |
                         |-- tool: validate_offer   --> FastAPI backend
                         |-- tool: check_compliance --> FastAPI backend
                         |-- tool: log_agreement    --> FastAPI backend
                         |-- tool: log_outcome      --> FastAPI backend
                         |
                         '-- end-of-call webhook     --> FastAPI backend (transcript log)
```

- **Voice platform:** [Vapi](https://vapi.ai) -- assistant config in
  `app/system-prompt.md` (system prompt) and `app/tools.json`
  (function/tool definitions pointed at the backend).
- **Backend:** `app/main.py`, FastAPI. Holds ALL business logic. The LLM
  never computes acceptability itself -- it calls these endpoints and relays
  the result.
- **Validation core:** `app/validation.py` -- pure functions, no LLM calls,
  fully unit tested (`app/test_validation.py`).
- **Storage:** SQLite (configurable via `DATABASE_PATH` env var), three tables:
  `agreements`, `calls` (full transcripts + outcomes), `compliance_flags`.

## Business rules implemented

Preference order (agent always tries the highest first):
1. **Full payment** -- $1,000, one payment.
2. **Downpayment + one more payment** -- full $1,000, highest possible
   downpayment.
3. **Settlement** -- up to 20% off (floor $800), max 3 payments.
4. **Payment plan** -- no discount, up to 3 months, biweekly/weekly/monthly.

Hard floor across every tier: no single payment below 25% of that tier's
agreed total. This is enforced in `validation.py`, not the prompt -- the model
cannot talk its way past it.

## API endpoints

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/validate-offer` | Validate a consumer's payment proposal against the waterfall |
| POST | `/check-compliance` | Screen agent speech for banned phrases before delivery |
| POST | `/log-agreement` | Record final agreed terms after consumer confirms |
| POST | `/log-outcome` | Record dispute or DNC outcomes (called before ending call) |
| POST | `/call-ended` | Vapi end-of-call webhook -- stores transcript, correlates outcome |
| GET | `/health` | Liveness check |

### `/log-outcome` details

Accepts `call_id` (required), `outcome` (required, one of `DISPUTE` or `DNC_REQUEST`),
and optional `notes`. Used by the agent when the consumer disputes the debt or
requests no further contact.

## Running locally

```bash
cd app
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

## Testing

Unit tests (validation logic):
```bash
cd app && python test_validation.py
```

API integration tests:
```bash
cd app && pytest test_api.py -v
```

Manual call scenarios: see `app/test_scenarios.md`.

## Deploying to Railway

1. Push this repo to GitHub.
2. In [Railway](https://railway.app), create a new project and connect the GitHub repo.
   Railway auto-detects the `Procfile` / `railway.json` and deploys.
3. Set environment variables in the Railway dashboard:
   - `DATABASE_PATH=/data/corafone.db`
   - `LOG_LEVEL=INFO`
4. (Optional) Attach a Railway Volume mounted at `/data` for SQLite persistence
   across deploys.
5. Verify: `curl https://<your-railway-url>/health` should return `{"status":"ok"}`.

## Setting up the Vapi assistant

1. Create an account at [vapi.ai](https://vapi.ai) and go to **Dashboard > Assistants > Create**.
2. Choose a model (GPT-4o recommended for reliable tool-calling).
3. Paste the contents of `app/system-prompt.md` as the system prompt.
4. Add the 4 tools from `app/tools.json`, replacing `YOUR-BACKEND-URL` with your
   deployed backend URL:
   - `validate_offer` -> `https://<URL>/validate-offer`
   - `check_compliance` -> `https://<URL>/check-compliance`
   - `log_agreement` -> `https://<URL>/log-agreement`
   - `log_outcome` -> `https://<URL>/log-outcome`
5. Set the **Server URL** (webhook) to `https://<URL>/call-ended` and enable
   the `end-of-call-report` event.
6. Choose a voice (calm, professional -- e.g. ElevenLabs "Rachel").
7. Go to **Phone Numbers > Buy Number** and attach it to this assistant.
8. Test: make a call, walk through a full payment happy path, then verify
   the agreement appears in the database via Railway logs.

## Key decisions

1. **External validation as an HTTP tool call, not prompt logic.** The 25%
   floor, the 20% discount cap, and the tier waterfall live in
   `validation.py` as plain Python, unit-tested independently of the voice
   agent. The LLM's job is to gather numbers from the conversation and relay
   the tool's decision -- it has no path to "agree" to something the backend
   rejected.
2. **Compliance as a callable guardrail, not just an instruction.** Rather
   than trusting the prompt to avoid threats, `check_compliance` is a tool
   the agent must call before any sentence involving consequences/urgency; a
   simple, auditable regex list blocks disallowed phrasing and logs every
   attempt, so a slipped generation is still caught before the consumer
   hears it.
3. **Settlement is a fallback, not the opener.** The prompt explicitly tells
   the agent not to lead with a discount -- it should try full payment and
   the downpayment tier first, since those are higher-value outcomes, and
   only offer the settlement/payment-plan tiers once the consumer signals
   $1,000 isn't workable.
