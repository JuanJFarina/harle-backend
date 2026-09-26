# Project Management Plan

## Scope

This plan advances Harle from the implemented controlled beta described in [Features](02_FEATURES.md) and the [SRS](03_SRS.md) to a broad commercial release. Telegram remains the first conversational channel, while the separate `harle-frontend` project consumes the authenticated web API owned by this backend.

The current beta already includes multi-user identity, user-scoped profiles and conversations, internal expenses, one-time and simple recurring events with process-local Telegram notifications, native Telegram image and audio input, a best-effort recent-media tool, Juan-only Google Sheets expenses, Telegram deduplication and ordering, temporary bans, and plan quotas.

This plan covers:

- Immediate alignment gaps in the implemented beta
- Reliability, authorization, privacy, and operational work required before broad launch
- Later product capabilities that remain part of the vision but are not broad-launch blockers

## Technology Decisions

- Telegram is the only commercial chat channel in the first version.
- The separate frontend repository owns browser presentation. This backend owns the unversioned `/api` contract, Google authentication, server-side sessions, renewable free subscription state, Telegram linking, and all business rules. Email/password and paid subscriptions are later work.
- Production uses PostgreSQL; the CLI remains a local development interface.
- FastAPI runs as one process while rate limits, in-flight quotas, per-user ordering, and the five-minute event scheduler are process-local.
- Harle and user-specific stores, profiles, context, and tool handlers remain request-scoped.
- Process-scoped repositories and clients must not retain a current user.
- Commercial users receive PostgreSQL expenses and events.
- Juan receives PostgreSQL events and private legacy Google Sheets expenses based only on his configured internal UUID.
- The initial Gratuito, Básico, and Max plans cost ARS 0, 5,000, and 15,000 per month. They include 60, 480, and 1,920 conversations plus 15, 60, and 240 event notifications per synchronized subscription period.
- The backend commerce service will synchronize each user's provider-confirmed `subscription_period_starts_at` and `subscription_period_ends_at` UTC boundaries. These boundaries, rather than account creation, an original subscription date, or UTC calendar months, will define both allowance periods.
- Event-notification allowance will be reserved before Gemini and consumed only after successful Telegram delivery for either event type.
- Current controlled-beta schema changes use ordered idempotent SQL scripts. Versioned migrations remain required before broad launch.
- The current data model is defined in the [ERD](05_ERD.md).

## Non-Functional Targets

- No user may read or modify another user's conversations, profiles, expenses, events, or tool integrations.
- Unknown, inactive, banned, duplicate, or over-quota requests must stop before Gemini and tools.
- An event occurrence without notification allowance in the user's synchronized subscription period must stop before Gemini, must not consume conversation quota, and must not be marked as delivered.
- A duplicate Telegram update must not duplicate a conversation, tool record, or side effect.
- Failed event generation or delivery must not consume notification allowance, and a successfully delivered occurrence must count at most once.
- Logs and metrics must exclude conversation bodies, profile text, financial descriptions, event descriptions, credentials, spreadsheet identifiers, and tool payloads.
- Accepted work that the product promises to complete must survive process restarts.
- Production schema changes, backups, restoration, and account deletion must be repeatable and verifiable.
- Latency and external API cost must remain suitable for frequent daily use.

## Phases and Exit Criteria

### 1. Controlled-Beta Alignment

Goals:

- Persist `telegram_update_id` with tool interactions and enforce the existing update-and-interaction uniqueness rule.
- Move assistant orchestration and failure policy out of `harle_api` so the API depends only on services and utilities.
- Run the PostgreSQL expense, event, isolation, and restart-deduplication tests in the release environment.
- Add focused tests for non-Juan denial before Google Sheets client construction and for ban escalation, notice suppression, and strike decay.
- Add exact provisioned subscription-period boundaries, plan-level event-notification quotas, successful-delivery accounting, in-flight reservations, and the one-per-period static exhaustion notice.

Exit criteria:

- Replaying an update cannot create duplicate tool-interaction rows or side effects.
- Production API modules follow the documented dependency direction.
- All release-critical PostgreSQL integration tests pass against the deployed schema.
- Google Sheets construction and execution both fail safely for every non-Juan UUID.
- All three cooldown levels and strike decay are deterministic under an injected clock.
- Free, basic, and max users are limited to 15, 60, and 240 successful event notifications per synchronized subscription period without consuming conversation quota.
- Quota admission happens before Gemini, failed attempts consume nothing, delivered occurrences count once, and the user receives at most one non-Gemini exhaustion notice per synchronized subscription period.

### 2. Web Registration, Telegram Linking, and Paid Beta

Goals:

- Implement the unversioned `/api` contract documented in the SRS for Google identity, sessions, account state, Telegram linking, public plans, and paid subscriptions.
- Add Google OpenID Connect, revocable secure-cookie sessions, CSRF protection, explicit CORS origins, and endpoint-specific abuse controls.
- Provision complete active free accounts with required profiles and automatically renewable exact monthly allowance periods.
- Add short-lived, single-use Telegram account linking through the existing bot webhook.
- Add Mercado Pago hosted checkout for Básico and Max without storing card data.
- Authenticate and claim provider webhooks before reading and reconciling subscription or authorized-payment resources.
- Suspend rejected paid accounts immediately, recover them after an approved retry, and downgrade cancelled accounts to Gratuito only after the paid period ends.
- Keep browser identity and linking services separate from agent, tool, conversation, and model code.
- Continue using one ordered idempotent schema script for this focused release; checksummed migration infrastructure remains broad-launch work.

Exit criteria:

- `harle-frontend` can complete Google registration, inspect the session, create a Telegram deep link, observe successful linking, list plans, start checkout, inspect subscription state, and cancel through `/api` without direct database access.
- Sessions are revocable, browser writes are CSRF-protected, Google OAuth state, nonce, PKCE, issuer, and audience are verified, and ownership never comes from a client-supplied user UUID.
- First login creates one complete free account; later logins by the same Google subject reuse it.
- Active free periods advance automatically before conversation and scheduled access while preserving monthly boundaries.
- One Telegram identity cannot be linked to two users, and expired or replayed link tokens cannot attach an identity.
- Link commands never invoke Gemini, and ordinary linked messages continue through the existing agent admission path.
- Browser returns never activate paid access; duplicate or out-of-order Mercado Pago events create no duplicate effects.
- Approved payments activate exact paid periods, rejected payments suspend admission, cancellation preserves already-paid access, and period expiry returns the account to Gratuito.

### 3. Privacy and Operations

Goals:

- Define retention, export, deletion, credential revocation, backup retention, and supported-region policies.
- Implement complete account export and deletion across every user-owned PostgreSQL entity.
- Add CI with PostgreSQL, readiness checks, privacy-safe metrics, alerts, backups, and restore verification.
- Establish measurable latency, reliability, and cost thresholds.

Exit criteria:

- Export and deletion cover all user-owned data according to the approved policy.
- Deleted or revoked users cannot authenticate.
- CI executes critical unit and PostgreSQL integration tests without real provider credentials.
- Readiness detects database, schema, and worker failures without calling external providers.
- Backup restoration and credential revocation have been exercised.

### 4. Product Evolution

Implemented baseline:

- Internal events have `user_event` and `system_event` types, active and disabled states, permanent deletion, a notification window, and `last_notified_at`.
- Events may remain one-time or repeat forever through one `week_days` or `month_days` rule without materialized occurrence rows.
- A process-local `AgentsScheduler` runs every five minutes, derives unnotified local occurrences from their notification-window start until one hour after their end, wakes the owning active user's agent without modifying tools or consuming conversation quota, and records successful delivery. Notification lead defaults to zero minutes.
- Supported Telegram images, voice notes, and audio files reach Gemini as native content parts. Current media is attached automatically and the ten newest references remain available through a read-only tool for twelve hours on a best-effort basis.
- Delivered ordinary and interaction-event messages are persisted as standalone assistant messages, including read-only tool interactions, without fabricated user prompts or conversation-quota usage.
- Scheduled runs receive shared profiles, conversation context, current time and weather, Google Search grounding, and every authorized read-only tool while modifying tools are absent from the runtime store.
- Every user owns one non-deletable active or disabled `interaction_event` with no fixed schedule or recurrence. It is evaluated only when the same user has no due ordinary event in the scheduler pass.
- Interaction events use an interval-independent shape-2 Weibull policy from the latest user or successful assistant contact. User-controlled assistant-profile frequencies map high, medium, and low to 12-hour, 24-hour, and 48-hour scales, with high as the default. Interactions stop after seven days without an actual user message, automatically resume when the user returns, require no pending response, and use neither quota nor a quiet period.

Remaining goals:

- Add user-controlled memory and profile inspection, correction, refinement, and deletion.
- Add optional quiet periods for ordinary event notifications only if later product policy requires them.
- Add an authorized agent tool that invokes a controlled Google expense and calendar import or synchronization service.
- Add multi-user Google Sheets and Google Calendar through least-privilege OAuth.
- Define source-of-truth, synchronization, conflict, and revocation behavior before connecting internal and Google data.
- Consider WhatsApp, broader integrations, customization UI, and model routing only after the first commercial architecture is proven.

Exit criteria:

- Each capability has explicit product policy, user ownership, authorization, privacy, and delivery behavior before implementation.
- New integrations do not expose another user's credentials or data.
- Proactive behavior remains user-controlled through the interaction event's active or disabled state and preserves user agency.
- Each interaction event is user-disableable, cannot be deleted, is suppressed only by the same user's ordinary due event, and persists successful output as a standalone assistant message.
- The interaction probability is interval-independent, failed attempts reset no state, and no interaction message is sent after seven days of user inactivity.
- A recurring event remains one row, supports ordinary event schedules, and delivers at most one notification for each matching occurrence.
- Current-message media reaches Gemini directly, recent media can be reloaded by internal attachment ID, and unsupported media never invokes the assistant.
- Images, audio, imported records, and scheduled event context remain isolated to the owning user.

## Infrastructure and Cost

- Keep one FastAPI process until distributed rate limiting, quota reservations, ordering, and workers are implemented.
- Reuse process-wide PostgreSQL pools and safe provider clients.
- Prefer bounded context, selective tools, concise prompts, caching, and efficient models.
- Introduce infrastructure only when required for durability, isolation, or measurable operating cost.
- Do not add dates, capacity commitments, or plan prices until product and operational evidence supports them.

## Risk Management

- **Cross-user exposure**: Require UUID ownership in every repository operation and maintain two-user integration tests.
- **Legacy Sheets privilege leakage**: Keep authorization, injection, construction, and execution guards tied to Juan's internal UUID.
- **Ambiguous external writes**: Do not retry an uncertain Google Sheets modification automatically; require reconciliation.
- **Duplicate or lost Telegram work**: Use stable update-derived keys, durable queues, leases, and per-user ordering.
- **Process-count drift**: Enforce one-process deployment until coordination state becomes distributed.
- **Sensitive logging**: Use structured allowlisted fields and test that protected content is absent.
- **Recurring notification duplication**: Compare `last_notified_at` with the computed occurrence window and update it only after successful Telegram delivery.
- **Notification quota drift**: Use a successful-delivery ledger plus process-local in-flight reservations, retain consumed usage when an event is deleted, and reconcile quota persistence with durable outbox work before multiple workers are allowed.
- **Subscription-period drift**: Treat synchronized current-period boundaries as provider-confirmed commerce data, validate that start precedes end, and never infer billing periods from local account timestamps.
- **Web account takeover**: Use current password hashing, short-lived one-time tokens, revocable sessions, Google OAuth state and nonce validation, CSRF protection, rate limits, and recent-authentication checks for destructive actions.
- **Payment-state forgery**: Trust only authenticated Mercado Pago reconciliation and webhook data; never activate a subscription from a browser redirect.
- **Account-link takeover**: Bind Telegram link tokens to one authenticated account, expire and consume them once, and atomically enforce provider-identity uniqueness.
- **Contract drift**: Keep the backend SRS endpoint catalog authoritative, version breaking changes, and run consumer-contract tests with `harle-frontend`.
- **Excessive proactive messaging**: Evaluate interaction events only after same-user ordinary events, apply the user's explicit high, medium, or low frequency with interval-independent probability, reset it after every successful assistant delivery, and enforce the seven-day user-inactivity cutoff.
- **Scheduled context loss**: Persist each successfully delivered scheduled message without fabricating a user prompt so a later reply has the assistant-initiated context.
- **Sensitive media references**: Keep Telegram file identifiers out of logs and model context, retain no raw bytes after active use, and scope every recent-media lookup by internal user UUID.
- **Ephemeral media loss**: Treat the twelve-hour process-local media window as best-effort and allow restart to discard it.
- **Unresolved policy implemented as code**: Block the affected phase until the product decision is recorded in Features or the SRS.

## Accepted MVP Limitations

- A successful Telegram event notification followed by failure to persist `last_notified_at` may be delivered again. Durable outbox delivery is deferred.
- A successful Telegram delivery followed by failure to persist its notification-usage record may temporarily undercount quota or be retried. Durable transactional outbox delivery is deferred.
- A successful scheduled Telegram delivery followed by failure to persist its standalone history row may leave the next conversation without that outbound context. Durable transactional outbox delivery is deferred.
- A zero-minute notification may arrive up to one scheduler interval after event start because the process-local scheduler runs every five minutes.
- The single-process scheduler may scan every active recurring definition on each five-minute pass. Distributed or indexed recurrence scheduling is deferred until measured load requires it.
- Voice notes are the primary audio target. Audio uploaded as a generic Telegram document is unsupported.
- The Gemini inline request uses a conservative 12 MiB combined raw-media limit to remain below its total request limit after encoding and prompt overhead.
- PostgreSQL integration tests may skip in local environments without `TEST_POSTGRES_DATABASE_URL`; the release environment must execute them against the deployed schema.

## Open Product Decisions

- Trials, upgrades, downgrades, proration, carry-over, refunds, taxes, cancellation timing, and failed-payment grace behavior
- Transactional email provider, password policy, session lifetime, and Google-account linking policy
- Telegram link lifetime, relinking, unlinking, and account-recovery behavior
- Proposed-action lifetime and Telegram confirmation experience
- Conversation, inbox, outbox, expense, event, audit, and backup retention
- Export format, deletion SLA, credential revocation, and supported operating region
- Future expense currencies, category customization, and export semantics
- Whether Juan may move from legacy Sheets to internal expenses and how existing data would be handled
- Memory consent and automatic learning policy
- System-event creation permissions and supported task payloads
- The long-term Telegram image and audio MIME allowlist beyond the voice-note-first beta
- Internal versus Google source-of-truth and synchronization rules
- Exact service-level targets, metrics, and alert thresholds
