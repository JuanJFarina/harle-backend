# Features

## Current Product

- **Prompt CLI**: A local development user can send prompts from the terminal and receive direct AI responses with file-backed conversation history.
- **Telegram assistant**: Subscribed beta users can talk to Harle through a private Telegram bot backed by one FastAPI process.
- **Shared assistant engine**: The CLI and Telegram paths share the assistant behavior, model integration, memory contract, and tool reasoning loop.
- **Multi-user access**: Every Telegram update resolves the sender to a manually provisioned internal account with an active plan and subscription.
- **User isolation**: Conversations, profiles, personal history, tools, permissions, expenses, and events are scoped to the resolved internal user UUID.
- **Conversation persistence**: Telegram conversations and tool interactions use PostgreSQL; recent user-owned context is loaded before answering.
- **User and assistant profiles**: PostgreSQL stores each user's identity, locale, timezone, location, personal history, and assistant profile separately.
- **Current context awareness**: Harle receives the current date, time, and weather for the user's local environment before responding.
- **Search-grounded answers**: Harle can use Google Search grounding when answering prompts that benefit from current information.
- **Concise conversational style**: Harle responds in the same language as the user and prefers short, natural answers unless more detail is truly needed.
- **Tool reasoning loop**: Harle can decide whether to answer directly or call an available tool, then continue reasoning with the tool result.
- **Selective tool loading**: A registry authorizes tool families per user, then selects likely expense or event families through explicit English and Spanish terms, falling back to all authorized families when no term matches.
- **Direct modification policy**: Modifying tools are instructed to execute only when the current user message directly requests the change.
- **Commercial expense tracking**: Commercial users can add expenses and refunds, split purchases into 2 to 12 installments, query a day, summarize a month, update a transaction or installment group, and permanently delete it.
- **Expense policy**: Commercial expenses use Argentine pesos and fixed categories for rent, essential services, non-essential services, home, transport, outings, shopping, and other expenses.
- **Expense date handling**: Transactions without an explicit date use the previous local day from 00:00 through 04:59, and Harle reports when this rule was applied.
- **Juan-only Google Sheets expenses**: Juan's stable internal UUID receives the existing private Google Sheets expense family instead of commercial PostgreSQL expenses.
- **Internal events**: Every entitled user can list, create, update, disable, re-enable, and permanently delete private timed or all-day `user_event` and `system_event` records.
- **Simple recurring events**: An event may repeat forever on a unique `week_days` list or `month_days` list while remaining one event record with no materialized occurrence rows.
- **Recurring-event behavior**: Recurring events preserve ordinary timed, overnight, all-day, multi-day, timezone, type, title, description, notification-lead, update, disable, re-enable, and deletion behavior. An explicit null recurrence rule converts one back to a one-time event.
- **Event notifications**: A process-local `AgentsScheduler` checks every five minutes, derives matching local occurrences, wakes the owning active user's agent without modifying tools, and stores `last_notified_at` only after successful delivery.
- **Event notification policy**: Notifications default to the event start, while positive lead times open the window earlier. The scheduler sends an unnotified occurrence any time from its window opening until one hour after it ends, retries failed delivery during that grace period, and does not consume conversation quota.
- **Scheduled-agent scope**: Scheduled runs share profiles, prior conversation loading, current time and weather, Gemini, Google Search grounding, and the core reason-and-act loop with normal conversations. Their runtime store includes every authorized read-only tool and excludes modifying tools.
- **Scheduled-message history**: Every delivered `user_event`, `system_event`, or `interaction_event` message is persisted as a standalone assistant message with no fabricated user prompt. Scheduled messages and their read-only tool interactions are available to later conversation context without consuming conversation quota.
- **Interaction events**: Every user owns one active or disabled `interaction_event` with no fixed schedule, duration, recurrence, notification quota, or deletion flow. The scheduler considers it only after the same user has no due ordinary event in that pass.
- **Configurable proactive interactions**: Interaction probability grows from the latest user or successfully delivered assistant contact using an interval-independent shape-2 Weibull policy. The assistant profile offers user-controlled high, medium, and low frequencies with 12-hour, 24-hour, and 48-hour probability scales; new profiles default to high. Interactions stop after seven days without an actual user message, automatically resume when the user returns, have no quiet period, and consume neither quota.
- **Native Telegram media input**: Harle accepts supported Telegram images, voice notes, and ordinary audio messages and provides their bytes directly to Gemini throughout the current reason-and-act loop. Voice notes are the primary audio target; audio sent as a generic document remains unsupported.
- **Recent Telegram media**: Current-message media is attached automatically. A process-local, best-effort store retains the ten newest Telegram media references per user for twelve hours so the agent can load an earlier attachment through a read-only tool.
- **Media validation and history**: Unsupported media receives a concise `Formato no soportado` response before agent execution. Combined raw attachments are limited to 12 MiB per turn. Conversation history stores a compact attachment marker, caption, and filename when available, while raw bytes and Telegram file identifiers remain outside model context and persistence.
- **Telegram deduplication**: Every Telegram `update_id` is persisted before assistant work so webhook retries do not create a second conversation or tool change.
- **Ordered message aggregation**: Consecutive messages join the active turn while reasoning is safe to restart; messages received after tool execution or delivery begins become the next turn.
- **Temporary safety bans**: The tenth valid message within two seconds triggers a per-identity cooldown that escalates from 60 seconds to 5 minutes and then 1 hour, with strike decay and at most one notice per cooldown.
- **Exact subscription-period quotas**: Manually provisioned accounts store exact current `subscription_period_starts_at` and `subscription_period_ends_at` UTC boundaries. Completed conversations and successful ordinary event notifications use those half-open boundaries with process-local in-flight reservations instead of UTC calendar months.
- **Separate event-notification quotas**: Plans limit successful `user_event` and `system_event` deliveries separately from conversations. Successful occurrences remain in a user-owned delivery ledger, failed attempts and interaction messages consume no allowance, and the first blocked occurrence in a period receives one static exhaustion notice.
- **Configured initial plans**: The internal `free`, `basic`, and `max` plans provide 60, 480, and 1,920 conversations plus 15, 60, and 240 event notifications per monthly subscription period.
- **Frontend web API**: The backend exposes an authenticated `/api` contract for the separate `harle-frontend` project while keeping account rules outside agent code.
- **Google registration**: A visitor can register or sign in through Google OpenID Connect and receive a revocable server-side session in a secure cookie.
- **Renewable free accounts**: First Google login creates a complete active free account, and its exact monthly allowance period renews automatically while the account remains active.
- **Telegram account linking**: An authenticated web user can create a short-lived, single-use bot deep link that attaches a proven Telegram identity before agent admission.
- **Web session state**: An authenticated user can inspect the current account, free-plan period, and Telegram-link state.
- **Public plan catalog**: The web API exposes active Gratuito, Básico, and Max contracts with ARS prices and separate conversation and notification allowances.
- **Mercado Pago checkout**: An authenticated free user can start one hosted Básico or Max recurring checkout without exposing card data to Harle.
- **Signed subscription webhooks**: Provider notifications are signature-verified, claimed idempotently, and reconciled against Mercado Pago resources rather than trusted directly.
- **Paid subscription lifecycle**: Approved recurring payments activate exact paid periods, rejected payments suspend access, and later approved retries recover it.
- **Period-end cancellation**: Cancelling stops future provider charges, preserves already-paid access, and returns the account to Gratuito after period end.

## Pending Product MVP

- **Fast personal assistant**: Harle should optimize for the fastest useful response that still feels thoughtful and trustworthy.
- **Low-cost usage**: Harle should be cheap enough for frequent everyday use, choosing efficient models, prompts, memory, and tool calls.
- **User-scoped integrations**: Connected tools, credentials, polling context, stores, reminders, and permissions should belong to one user account and never leak across users.
- **Evolving user profile**: Harle should build and maintain a structured perception of the user's preferences, goals, routines, worries, communication style, and important life context.
- **Agent profile**: Harle should have its own configurable profile that can evolve under the user's direction without claiming to be human.
- **Long-term memory**: Harle should preserve all prior conversations, durable facts, user-provided personal history, and learned patterns separately from short-term conversation context.
- **Memory control**: The user should be able to inspect, correct, delete, or refine what Harle remembers.
- **Safety and privacy**: Harle should protect user data, keep personal context private, and treat safety as a core product capability.
- **Human conversation style**: Harle should feel warm, personal, and natural without becoming verbose or performative.
- **Personal finance**: Harle should help users manage personal finances through natural conversation and connected finance tools.
- **Productivity support**: Harle should build on recurring events and process-local notifications with durable delivery, quiet periods, or calendar integration when those capabilities have clear ownership and delivery guarantees.
- **General companionship**: Harle should help the user feel better, reflect, stay organized, and improve their life while staying within healthy assistant boundaries.
- **Read on request**: Harle may read or query connected tools such as expenses, reminders, or calendar data when the user asks a question.
- **User-authorized modifications**: Harle may modify expenses, reminders, calendar events, profiles, memories, or other user data when the user directly asks for that modification. If Harle infers, suggests, or initiates a modification itself, it must ask the user first.
- **Confirmation and audit**: Inferred modifications should become expiring proposed actions that the same user can confirm or cancel, and every executed modification should be auditable.
- **Automated subscription synchronization**: The backend commerce service should synchronize provider-confirmed plan, subscription state, and exact period boundaries into the account data used by assistant admission.
- **Privacy controls**: Users should be able to export and delete their data according to defined retention, backup, and deletion policies.
- **Durable accepted work**: Work accepted from Telegram should survive process restarts without duplicate side effects or duplicate responses where the provider permits it.
- **Operational readiness**: Broad release requires automated quality gates, readiness checks, safe metrics and logs, backups, and exercised restoration.

## Possible Later Features

- **Email and password authentication**: Users may register with a verified email and password, recover access, and safely link that credential to an existing account.
- **Account management API**: Authenticated users may inspect and update their user profile, assistant profile, proactive-interaction settings, current plan, subscription period, and usage.
- **Expense management API**: Authenticated users may list, summarize, create, update, and permanently delete their own internal expenses through the same domain rules used by assistant tools.
- **Event management API**: Authenticated users may list, create, update, disable, re-enable, and permanently delete their own `user_event` records through the same domain rules used by assistant tools.
- **External context injectors**: Harle may use cached or polled context providers for data such as weather, location, reminders, calendars, or other user-authorized topics.
- **Durable background queues**: Harle may use durable queues for scheduled agent wakeups, outbound messages, proposed actions, and integration polling when reliability requires it.
- **Google import and synchronization**: An authorized agent tool may invoke a controlled migration or synchronization service that imports Google Sheets expenses and Google Calendar events into Harle's internal systems.
- **Multi-user Google integrations**: Users may connect Google Sheets or Google Calendar through OAuth with encrypted, revocable credentials after source-of-truth, import, and synchronization rules are defined.
- **WhatsApp integration**: Harle should eventually support WhatsApp because of its broader market reach.
- **Additional communication channels**: Harle may later support voice, email, or native mobile surfaces if they improve everyday access.
- **Broader personal integrations**: Harle may integrate with email, notes, documents, task managers, banking exports, health data, or other services that help manage the user's life.
- **User-specific customization UI**: Harle may include a simple interface for editing preferences, memories, integrations, and notification rules.
- **Model routing**: Harle may route work across different models based on cost, latency, complexity, and required quality.

## Intentionally Out Of Scope

- **Generic model playground**: Harle should not become a tool for comparing models, tweaking prompts, or experimenting with AI APIs as the main product experience.
- **Cluttered productivity dashboard**: Harle should not become a heavy dashboard where the user has to manage the assistant manually.
- **Web UI inside this repository**: Landing pages and browser interfaces belong to the separate frontend repository. This backend owns their API, authentication, payment integration, and business rules.
- **Commercial CLI access**: The CLI remains a development interface rather than a subscribed product channel.
- **Feature volume for its own sake**: New integrations should not be added unless they make the assistant more useful in real life.
- **Manipulative human simulation**: Harle should not hide that it is AI, create dependency, or use human-like behavior in ways that reduce the user's agency.
- **Unbounded autonomy**: Harle should not take important actions without appropriate user control, authorization, or recoverability.
- **Medical or psychological authority**: Harle should not act as a doctor, psychologist, therapist, or clinical authority.

## Needs Product Discovery

- **Privacy requirements**: Define the technical, legal, and product requirements needed to make user data safe and private.
- **Subscription lifecycle**: Define trials, plan changes, proration, refunds, failed-payment grace, taxes, and cancellation timing.
- **Confirmation flow**: Define the exact user experience for approving environment modifications from Telegram.
- **Memory policy**: Define what Harle stores automatically, what requires explicit consent, and how users can review or delete memory.
- **Data lifecycle**: Define retention periods, deletion SLA, export format, backup retention, supported operating region, and credential revocation.
- **Quota evolution**: Define carry-over behavior and how future price or allowance changes affect existing subscriptions.
- **Google source of truth**: Decide whether internal expenses and events remain authoritative after multi-user Google integrations are introduced.
