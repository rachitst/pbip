# ADR 0036: Groq as an optional hosted router for unclear assistant questions
- Status: Accepted by product owner, 2026-11-03
- Date: 2026-11-03
- Deciders: Product owner, Architecture Team
- Builds on: [ADR-0018](0018-assistant-llm-routes-server-answers.md) (the model only chooses a tool; the server
  writes the answer)

## Context
ADR-0018 routes an unclear assistant question through a local Ollama model (`qwen2.5:1.5b-instruct` by
default) on the API host's CPU. On the reference laptop (2 CPUs, 7 GB, shared with the rest of the stack) that
call is slow and often hits the 4 s timeout, after which the keyword rules answer; small local models also pick
the wrong tool more often. The product owner asked (2026-11-03) for Groq's hosted API as an optional, faster
router for exactly this one call, and approved it explicitly because it adds an external service.

Scope is deliberately narrow: only the routing call for questions that the fast path (`fallback.clear_route`)
cannot route. Clear questions, refusals, tool execution, reply writing, fraud scoring, recommendations and
every other module never call an external model.

## Decision
`LLM_PROVIDER=ollama|groq` (default **ollama**). With `groq`, the assistant builds a provider chain per request:
**Groq → Ollama → keyword rules**. Groq sits behind the same `ChatModel` contract as Ollama
(`modules/assistant/providers.py`):

* **Same tool registry, same validation.** Groq's OpenAI-compatible `POST /openai/v1/chat/completions` with
  one function per approved tool generated from `tools.REGISTRY` (identical argument schemas to Ollama's
  grammar, `additionalProperties: false`) plus a `none` function, `tool_choice: "required"`. Exactly one call
  is accepted; its arguments go through `llm.parse_choice` and the tool's own Pydantic model
  (`extra="forbid"`, bounds). An unknown tool, an identity argument, out-of-bound values, malformed JSON, no
  call or several calls are "invalid" and the chain moves on to Ollama. `customer_id` stays server-side
  (`ToolContext` from the access token); tools stay read-only in a `READ ONLY` transaction.
* **Fixed endpoint.** `https://api.groq.com/openai/v1` is a code constant (SSRF), not configuration.
* **Configuration only via `core/config.py`:** `GROQ_API_KEY` (`SecretStr`), `GROQ_MODEL`,
  `GROQ_REASONING_EFFORT`, `GROQ_TIMEOUT_SECONDS` (default 5 s), `RATE_LIMIT_GROQ_PER_MINUTE` (default 5 per
  customer). Staging/production refuse to boot with `LLM_PROVIDER=groq` and no key or no model; locally a
  missing key or model skips Groq with a warning.
* **No retry storms.** One attempt per provider per question. A 429 starts a process-wide cooldown for
  `retry-after` seconds (capped at 60 s) during which Groq is skipped. A per-customer budget
  (`within_budget`, Redis fixed window) skips Groq once a customer has used theirs; it fails **closed** (Redis
  down → Ollama), unlike the sign-in limiter.
* **Reasoning never leaves the adapter.** GPT-OSS models get `include_reasoning: false` (Groq does not accept
  `reasoning_format` for them); other reasoning models get `reasoning_format: "hidden"`. The adapter reads only
  `tool_calls`; `content` and any `reasoning` field are ignored, so reasoning text is never shown, stored in
  `assistant_messages`, audited or logged. Only the server-written reply passes the output filter, as before.
* **Audit.** One `assistant.llm_call` row per provider tried: `{provider, outcome, latency_ms}` with outcome in
  `ok | timeout | rate_limited | error | invalid | budget | cooldown`. Never the message, prompt or answer.

### What leaves the stack (data minimisation)
Per routed question, only: the static system prompt (rules and tool list, no customer data), the static
function schemas, and the customer's question after `sanitize.redact_for_provider`, which removes UUIDs
(customer, user, account ids), 12-19 digit account/card numbers (masked to the last four), Indian mobile
numbers, emails, JWTs and the signed-in customer's own name. **No tool results, retrieved text (merchant names,
references), balances, conversation history or identity are ever sent**: under ADR-0018 the model never sees
them on any provider. (The owner's brief allowed redacted tool results; the design does not need them, so none
are sent.) Groq's `user` field is not sent. Residual risk: free text the customer types about a third party
(another person's name) cannot be recognised reliably and may be sent; the guard refuses most "other person"
phrasings before any model call.

### Provider terms, read from Groq's current documentation on 2026-11-03
| Topic | What the documentation says | Source |
|---|---|---|
| Retention | "By default, Groq does not retain customer data for inference requests." Inputs/outputs may be logged temporarily only to troubleshoot reliability or investigate abuse, "retained for up to 30 days"; opt-out via Zero Data Retention in Data Controls | https://console.groq.com/docs/your-data |
| Zero Data Retention | Any customer may enable ZDR in Data Controls; then no retention for reliability/abuse monitoring | https://console.groq.com/docs/your-data |
| Data location | "All customer data is retained in Google Cloud Platform (GCP) buckets located in the United States"; SCCs available for transfers. The page does not say where inference runs: we treat it as outside India | https://console.groq.com/docs/your-data |
| Training | "Groq is not permitted to use Inputs or Outputs for training or fine-tuning any AI Model Services or other models, unless explicitly granted permission or instructed by Customer" (Services Agreement, last modified 22 June 2026) | https://console.groq.com/docs/legal/services-agreement |
| Tool use | GPT-OSS 20B/120B, GPT-OSS-Safeguard 20B, Qwen 3.8 27B and others support tool use; GPT-OSS does not support parallel tool calls; `tool_choice` accepts `required` | https://console.groq.com/docs/tool-use, https://console.groq.com/docs/api-reference |
| Strict schemas | `strict: true` constrained decoding exists for Structured Outputs, but "tool use [is] not currently supported with Structured Outputs" - hence server-side validation stays the guarantee | https://console.groq.com/docs/structured-outputs |
| Reasoning | GPT-OSS: `reasoning_effort` low/medium/high (default medium), `reasoning_format` unsupported, `include_reasoning: false` excludes reasoning | https://console.groq.com/docs/reasoning, https://console.groq.com/docs/api-reference |
| OpenAI compatibility | Base URL `https://api.groq.com/openai/v1`; `temperature: 0` is converted to 1e-8; `messages[].name`, `logprobs`, `logit_bias` unsupported | https://console.groq.com/docs/openai |
| Rate limits | Organisation-wide. Free tier for `openai/gpt-oss-20b` and `-120b`: 30 RPM, 1K RPD, 8K TPM, 200K TPD; 429 with `retry-after` | https://console.groq.com/docs/rate-limits |
| Price (on demand) | `gpt-oss-20b` $0.075 / $0.30 per 1M input/output tokens; `gpt-oss-120b` $0.15 / $0.60; `qwen3.8-27b` $0.80 / $4.00; server errors are not charged | https://console.groq.com/docs/models, https://console.groq.com/docs/errors |

### Failure modes
| Failure | Behaviour |
|---|---|
| Timeout (> `GROQ_TIMEOUT_SECONDS`) | outcome `timeout`, Ollama answers, then rules |
| 429 from Groq | outcome `rate_limited`, cooldown starts, Ollama answers |
| 5xx / 4xx (bad key, `tool_use_failed`) | outcome `error`, Ollama answers |
| Malformed or injected tool call | outcome `invalid`, Ollama answers |
| Per-customer budget used / cooldown active | outcome `budget` / `cooldown`, no request is made |
| Groq and Ollama both fail | keyword rules answer (`engine: rules`); the customer always gets an answer |
| Key leaked | rotate (runbook "Groq API key"); the key is never logged, echoed in errors or sent anywhere but the `Authorization` header to the fixed URL |

## Model choice (addendum: do not hard-code a guess)
`GROQ_MODEL` has no default in code. The choice comes from `scripts/benchmark_assistant_llm.py` run with the
owner's key on the committed question set (`scripts/assistant_llm_benchmark_questions.json`: 10 normal unclear
questions, 15 adversarial probes sent straight to the router, guard reported separately). Rule: best
tool-calling accuracy with zero leaks; ties by latency; lowest reasoning effort that keeps accuracy.

**Pending live run.** The comparison table, the chosen model and reasoning effort, and the Ollama baseline are
filled in from a real run in a follow-up commit on this branch. No numbers are recorded here until then.

## Alternatives considered
- **Groq SDK / OpenAI SDK** - a new dependency for one POST; `httpx` is already locked and used for Ollama.
- **Structured Outputs (`response_format` json_schema, strict) instead of tool calling** - constrained
  decoding like Ollama's grammar, but strict mode requires every property `required`, which our optional,
  defaulted arguments are not, and the owner asked for tool calling. Kept as a possible later change.
- **Groq as the only model (no Ollama fallback)** - an outage or 429 would drop routing quality for everyone;
  the chain keeps the local path.
- **Retry on 429/5xx** - adds latency and load on a limited org; the fallback is cheaper and always available.
- **Sending tool results for LLM-worded replies** - rejected by ADR-0018; would send balances and third-party
  text out of the stack.

## Consequences
- Faster, more accurate routing when enabled (to be quantified by the pending run); the default stays local,
  so no data leaves the stack unless an operator sets `LLM_PROVIDER=groq`.
- A new data flow to a US-hosted processor. Acceptable for this simulated bank with the minimisation above; a
  real deployment would need a legal review (cross-border transfer, outsourcing rules) and ZDR enabled.
- Free-tier org limits (8K tokens/min) cap throughput at a few routed questions per minute for the whole
  deployment; beyond that the cooldown and Ollama take over. A paid tier is needed for real traffic.
- New operational secret (`GROQ_API_KEY`) with a rotation procedure (runbook). Threat model T55.
- Audit volume: one extra row per model attempt (at most two per unclear question).
