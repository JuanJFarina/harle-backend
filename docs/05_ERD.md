# Entity Relationship Diagram

## Scope

This is a conceptual view of Harle's current PostgreSQL model. The base relationships appear first, followed by the implemented subscription, notification, interaction, and scheduled-message extensions. It aligns with the [SRS](03_SRS.md) and [Project Management Plan](04_PMP.md) while omitting migration-level detail.

```mermaid
erDiagram
    PLAN ||--o{ HARLE_USER : assigns
    HARLE_USER ||--o{ EXTERNAL_IDENTITY : authenticates_with
    HARLE_USER ||--o| USER_PROFILE : has
    HARLE_USER ||--o| ASSISTANT_PROFILE : configures
    HARLE_USER ||--o{ CONVERSATION : owns
    HARLE_USER ||--o{ EXPENSE_TRANSACTION : owns
    HARLE_USER ||--o{ INTERNAL_EVENT : owns
    CONVERSATION o|--o{ TELEGRAM_UPDATE_CLAIM : completes

    PLAN {
        TEXT code PK
        INTEGER monthly_request_limit
        INTEGER monthly_notification_limit
        BOOLEAN active
        TIMESTAMPTZ created_at
        TIMESTAMPTZ updated_at
    }

    HARLE_USER {
        UUID id PK
        TEXT display_name
        TEXT plan_code FK
        TEXT subscription_status
        TIMESTAMPTZ subscription_valid_until
        TIMESTAMPTZ subscription_period_starts_at
        TIMESTAMPTZ subscription_period_ends_at
        TIMESTAMPTZ subscription_synced_at
        TIMESTAMPTZ created_at
        TIMESTAMPTZ updated_at
    }

    EXTERNAL_IDENTITY {
        UUID id PK
        UUID user_id FK
        TEXT provider
        TEXT external_user_id
        TEXT display_name
        TEXT email
        TIMESTAMPTZ created_at
        TIMESTAMPTZ updated_at
    }

    USER_PROFILE {
        UUID user_id PK, FK
        TEXT preferred_name
        TEXT locale
        TEXT timezone
        NUMERIC latitude
        NUMERIC longitude
        TEXT personal_history
        TIMESTAMPTZ created_at
        TIMESTAMPTZ updated_at
    }

    ASSISTANT_PROFILE {
        UUID user_id PK, FK
        TEXT display_name
        TEXT profile_text
        TEXT interaction_frequency
        TIMESTAMPTZ created_at
        TIMESTAMPTZ updated_at
    }

    CONVERSATION {
        BIGINT id PK
        UUID user_id FK
        BIGINT telegram_chat_id
        BIGINT telegram_update_id
        TEXT prompt
        TEXT response
        TEXT model
        TEXT kind
        TEXT status
        JSONB tool_call_response
        JSONB tool_result
        SMALLINT tool_interaction_index
        TEXT failure_code
        TIMESTAMPTZ created_at
        TIMESTAMPTZ completed_at
    }

    TELEGRAM_UPDATE_CLAIM {
        BIGINT update_id PK
        BIGINT telegram_user_id
        BIGINT telegram_chat_id
        TEXT message_text
        TEXT status
        BIGINT conversation_id FK
        TIMESTAMPTZ claimed_at
        TIMESTAMPTZ updated_at
        TIMESTAMPTZ tool_started_at
        TIMESTAMPTZ delivered_at
    }

    EXPENSE_TRANSACTION {
        UUID id PK
        UUID user_id FK
        TEXT entry_type
        NUMERIC amount
        CHAR currency
        TEXT category
        DATE transaction_date
        TEXT description
        UUID installment_group_id
        SMALLINT installment_number
        SMALLINT installment_count
        TIMESTAMPTZ created_at
        TIMESTAMPTZ updated_at
    }

    INTERNAL_EVENT {
        UUID id PK
        UUID user_id FK
        TEXT title
        TEXT description
        TIMESTAMPTZ starts_at
        TIMESTAMPTZ ends_at
        TEXT timezone
        BOOLEAN all_day
        TEXT event_type
        TEXT status
        TIMESTAMPTZ notification_window_start
        TIMESTAMPTZ last_notified_at
        JSONB recurrence_rule
        TIMESTAMPTZ created_at
        TIMESTAMPTZ updated_at
    }
```

## Relationships

| From | To | Cardinality | Notes |
| --- | --- | --- | --- |
| Plan | Harle user | One to many | Every user references one configured plan. |
| Harle user | External identity | One to many | Provider and external user ID are unique together. Telegram is the current provider. |
| Harle user | User profile | One to zero or one | A profile is required before the runtime can serve the user. |
| Harle user | Assistant profile | One to zero or one | The assistant persona is configured independently for each user. |
| Harle user | Conversation | One to many | Conversation and tool-interaction rows are owned by one user. |
| Harle user | Expense transaction | One to many | Commercial expense data is private to its owner. |
| Harle user | Internal event | One to many | Every event query and mutation is owner-scoped. |
| Conversation | Telegram update claim | Zero or one to many | One aggregated conversation may complete multiple Telegram updates. |

## Entity Semantics

### Accounts and Profiles

- Subscription status is `active`, `inactive`, `past_due`, `cancelled`, or `revoked`.
- An external identity cannot belong to two users for the same provider and external identifier.
- Profiles store user-owned personalization separately from conversation history.
- Assistant interaction frequency is `high`, `medium`, or `low`, mapping to 12-hour, 24-hour, or 48-hour Weibull scales. New profiles default to `high`.
- Latitude and longitude are both present or both absent. Timezones use IANA names.
- Juan's Google Sheets privilege is deployment configuration keyed to his stable user UUID; it is not a database role or display-name property.

### Conversations and Telegram Claims

- Conversation rows use kind `conversation`, `tool_call`, or `scheduled_message`.
- A scheduled-message row has no user prompt and stores one successfully delivered assistant message. It is available to later context but excluded from conversation quota.
- Conversation status is `processing`, `completed`, or `failed`; subscription-period quota counts only completed `conversation` rows.
- A non-null Telegram update ID identifies a delivered conversation. Tool interactions also require an update-derived identifier and interaction index for complete idempotency.
- Telegram claim status is `received`, `processing`, `tool_started`, `delivering`, `delivered`, `failed`, `rate_limited`, `interrupted`, or `rejected`.
- Telegram update IDs are globally unique for the bot and persist deduplication state across process restarts.

### Expenses

- Amounts are positive with two decimal places and currency is `ARS`.
- Entry type is `expense` or `refund`; refunds contribute negatively to summaries.
- Categories are fixed to rent, essential services, non-essential services, home, transport, outings, shopping, and other.
- Installment fields are all absent or all present. Counts range from 2 to 12, and installment numbers are unique within one user's group.
- Expense deletion is permanent. Selecting one installment for update or deletion affects its complete group.

### Internal Events

- Event status is `active` or `disabled`.
- Event type is `user_event` for the user's agenda or `system_event` for an internal assistant reminder or task.
- Start and end are stored in UTC while the originating IANA timezone is preserved.
- End must follow start. All-day events use local-midnight boundaries.
- Notification windows may start at the event anchor or earlier. Both event types default to a zero-minute lead, and positive values configure a pre-start lead.
- `recurrence_rule` is absent for a one-time event or contains one non-empty `week_days` or `month_days` list. Recurrence is infinite and creates no occurrence rows.
- A recurring event preserves the ordinary event schedule and fields. A missing month day produces no occurrence in that month.
- Successful notification delivery updates `last_notified_at`; failure leaves it unchanged so delivery remains eligible until one hour after the occurrence ends.
- Disabling is reversible and suppresses occurrences and notifications. Deletion permanently removes the event.

## Indexes and Constraints

- Conversations are indexed by user, chat, creation time, kind, status, and monthly quota range.
- Delivered conversation update IDs and update-plus-tool-interaction identifiers are unique when present.
- Expenses are indexed by user and transaction date, category, and installment group.
- Events are indexed by user, status, and start time, with partial indexes for active one-time notifications and active recurrence definitions.
- Telegram claims use `update_id` as the primary deduplication key and are indexed by status and update time.
- User-owned entities cascade when their owning user is physically deleted, subject to the future retention and deletion policy.

Recent Telegram media remains outside the PostgreSQL ERD while its twelve-hour retention is best-effort. The process-local store contains only user-scoped Telegram references and compact metadata, never raw image or audio bytes.

## Current Subscription, Notification, and Interaction Extensions

These extensions are implemented by the ordered subscription and notification SQL scripts.

```mermaid
erDiagram
    HARLE_USER ||--|| INTERACTION_EVENT : owns
    HARLE_USER ||--o{ EVENT_NOTIFICATION_DELIVERY : owns
    INTERNAL_EVENT o|--o{ EVENT_NOTIFICATION_DELIVERY : produces
    HARLE_USER ||--o{ EVENT_NOTIFICATION_QUOTA_NOTICE : receives

    HARLE_USER {
        TIMESTAMPTZ subscription_period_starts_at
        TIMESTAMPTZ subscription_period_ends_at
    }

    PLAN {
        INTEGER monthly_notification_limit
    }

    INTERACTION_EVENT {
        UUID id PK
        UUID user_id FK
        TEXT status
        TIMESTAMPTZ last_user_message_at
        TIMESTAMPTZ last_agent_message_at
        TIMESTAMPTZ created_at
        TIMESTAMPTZ updated_at
    }

    EVENT_NOTIFICATION_DELIVERY {
        UUID id PK
        UUID user_id FK
        UUID event_id FK
        TIMESTAMPTZ occurrence_starts_at
        TIMESTAMPTZ notification_window_start
        TIMESTAMPTZ delivered_at
    }

    EVENT_NOTIFICATION_QUOTA_NOTICE {
        UUID user_id PK, FK
        TIMESTAMPTZ period_starts_at PK
        TEXT status
        TIMESTAMPTZ attempted_at
        TIMESTAMPTZ delivered_at
    }
```

- `subscription_period_starts_at` and `subscription_period_ends_at` are exact current UTC boundaries. They are manually provisioned in the controlled beta and will be synchronized from provider-confirmed commerce state. The start is inclusive, the end is exclusive, and `subscription_valid_until` remains a separate access-expiration field.
- Conversation and event-notification usage use the synchronized boundaries. Harle does not derive allowance periods from account creation, an original subscription date, or UTC calendar months.
- `PLAN.monthly_notification_limit` is separate from `monthly_request_limit`; the initial free, basic, and max values are 15, 60, and 240 per synchronized subscription period. Their conversation limits are 60, 480, and 1,920.
- A delivery row represents one successfully delivered `user_event` or `system_event` occurrence. Period usage counts `delivered_at` within the owning user's synchronized boundaries.
- The event identifier, occurrence start, and notification window are unique together so a successful occurrence cannot consume allowance twice.
- Deleting an event sets the ledger's event reference to null instead of deleting its usage. Deleting the owning account removes its delivery and notice records under the account-deletion policy.
- The notice marker permits at most one static, non-Gemini quota-exhausted notice attempt per user and synchronized subscription period. It consumes neither conversation nor event-notification allowance.
- Process-local in-flight reservations remain outside PostgreSQL while deployment is limited to one process. Durable distributed reservations belong with later queue and worker design.
- Every user owns exactly one interaction event. Its status is `active` or `disabled`; the user may change that status but cannot delete the row.
- An interaction event has no start, end, notification window, recurrence, or materialized occurrence. `last_user_message_at` tracks actual inbound activity for the seven-day cutoff, and `last_agent_message_at` advances only after a successful assistant delivery.
- The scheduler evaluates an active interaction event only after the same user has no due ordinary event. Its probability uses the later contact timestamp, resets after successful assistant delivery, and consumes no conversation or event-notification allowance.

## Current Scheduled-Message Extension

The conversation model supports `kind = 'scheduled_message'` in addition to `conversation` and `tool_call`.

- A scheduled-message row stores the assistant text delivered for a `user_event`, `system_event`, or `interaction_event`.
- Its user prompt is absent rather than synthesized, and no pending or expected user response is represented.
- The row is included in later conversation context but excluded from conversation quota.
- Read-only tool interactions from the scheduled run remain separate `tool_call` history rows under the existing interaction contract.
- Scheduled-message persistence occurs only after successful Telegram delivery. Failed generation or delivery creates no history row and does not advance interaction contact state.

## Current Free Web Registration Extension

This implemented model supports Google registration, renewable free accounts, browser sessions, and Telegram linking.

```mermaid
erDiagram
    HARLE_USER ||--o{ WEB_SESSION : owns
    HARLE_USER ||--o{ TELEGRAM_LINK_TOKEN : creates

    WEB_SESSION {
        UUID id PK
        UUID user_id FK
        TEXT session_token_hash UK
        TIMESTAMPTZ expires_at
        TIMESTAMPTZ revoked_at
        TIMESTAMPTZ last_used_at
        TIMESTAMPTZ created_at
    }

    TELEGRAM_LINK_TOKEN {
        UUID id PK
        UUID user_id FK
        TEXT token_hash UK
        TIMESTAMPTZ expires_at
        TIMESTAMPTZ consumed_at
        TIMESTAMPTZ created_at
    }
```

- Google authentication reuses `EXTERNAL_IDENTITY` with provider `google` and a stable provider subject. Telegram continues to use provider `telegram`.
- A user may own at most one identity for each provider, and one provider identity may belong to at most one user.
- A web session stores only a hash of the opaque cookie value and supports independent expiry and revocation.
- A Telegram link token is account-bound, short-lived, single-use, and stored only as a hash. Issuing one invalidates earlier pending tokens for the user.
- Successful bot proof consumes the token and creates the Telegram external identity atomically. It never moves an identity from another user.
- First Google login creates a complete active free user, both required profiles, and exact allowance boundaries. The existing user-insert trigger creates the interaction event.
- Free renewal advances both exact period boundaries by calendar months before conversation or scheduled access while preserving prior boundaries.

## Current Paid Subscription Beta Extension

```mermaid
erDiagram
    PLAN ||--o{ PAYMENT_SUBSCRIPTION : selected_for
    HARLE_USER ||--o{ PAYMENT_SUBSCRIPTION : owns
    PAYMENT_SUBSCRIPTION ||--o{ SUBSCRIPTION_PAYMENT : produces

    PAYMENT_SUBSCRIPTION {
        UUID id PK
        UUID user_id FK
        TEXT plan_code FK
        TEXT provider
        TEXT provider_subscription_id UK
        TIMESTAMPTZ provider_updated_at
        TEXT payer_email
        TEXT status
        TEXT provider_status
        TEXT checkout_url
        TIMESTAMPTZ period_starts_at
        TIMESTAMPTZ period_ends_at
        TIMESTAMPTZ next_payment_at
        TIMESTAMPTZ latest_payment_updated_at
        BOOLEAN cancel_at_period_end
        TIMESTAMPTZ created_at
        TIMESTAMPTZ updated_at
    }

    SUBSCRIPTION_PAYMENT {
        UUID id PK
        UUID subscription_id FK
        TEXT provider_invoice_id UK
        TEXT provider_payment_id
        TEXT status
        NUMERIC amount
        CHAR currency
        TIMESTAMPTZ debit_at
        TIMESTAMPTZ updated_at
    }

    PAYMENT_WEBHOOK_CLAIM {
        TEXT provider_event_id PK
        TEXT provider_request_id
        TEXT topic
        TEXT resource_id
        TEXT status
        TIMESTAMPTZ received_at
        TIMESTAMPTZ processed_at
    }
```

- `PLAN` gains public display name, monthly ARS price, currency, and billing interval fields and remains the source of allowance limits.
- A user may have historical subscriptions but at most one open creating, pending, active, or past-due subscription.
- The internal subscription UUID is sent to Mercado Pago as `external_reference`; browser state never selects the owning user during reconciliation.
- A signed webhook claim is persisted before provider reads. Reconciliation remains idempotent even when Mercado Pago retries or sends state out of order.
- Approved authorized payments define paid access periods. Rejected payments set the subscription and account to past due.
- Cancelling sets the provider subscription to its irreversible cancelled state, retains local access through the current paid period, and then returns the user to a new free period.
- Existing manually provisioned paid accounts have no `PAYMENT_SUBSCRIPTION` row and remain unchanged until explicitly migrated.

## Out Of Scope For This ERD

- Proposed actions and action audits
- Durable Telegram inbox and outbox queues
- Ordinary-event notification preferences and durable scheduler queues or outbox records
- OAuth credentials and multi-user Google integrations
- Email/password credentials and verification or recovery tokens
- Browser-local UI state and frontend analytics
