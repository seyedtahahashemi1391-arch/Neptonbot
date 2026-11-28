# NeptonBot platform portability assessment

**Status:** architecture assessment only; no runtime refactor or additional platform integration is included.

## 1. Current boundary

NeptonBot currently runs against Soroush Plus. The application is not yet transport-neutral:

- `bot.py` constructs the Soroush API base URL and sends provider method names such as `getUpdates`, `sendMessage`, and `setMyCommands`.
- Polling and response delivery call the Soroush transport directly.
- `extract_updates()`, `get_update_id()`, and `extract_message()` interpret provider-shaped update payloads.
- Response plans persist provider-shaped `reply_markup` JSON.
- Queue and persistence identity is not platform-scoped: `processed_updates.update_id` is globally unique, offsets use one `polling_offset` key, and message/history/feedback records use `chat_id` without a platform namespace.

These are concrete blockers to safely running more than one messaging platform against the same database. Two providers can reuse update IDs or chat IDs, and their polling offsets, reply markup, message IDs, and rate-limit semantics are not interchangeable.

## 2. Recommended target architecture

Use a shared application core with a platform adapter boundary and isolated platform pollers/worker capacity where operational limits differ.

### Shared core responsibilities

- Canonical inbound event validation and normalization.
- Durable inbox, deduplication, queue ordering, retry scheduling, and Dead Letter handling.
- AI orchestration, conversation policy, history, and response planning.
- Platform-neutral response objects and delivery state transitions.
- Admin authorization and platform-scoped operational diagnostics.

### Adapter responsibilities

Each adapter should implement a small contract:

1. `platform_id`: stable identifier such as `soroush`.
2. `fetch_updates(offset)`: retrieve provider updates and return a provider cursor.
3. `normalize_update(raw_update)`: map provider data to a canonical inbound event.
4. `send_message(outbound_message)`: deliver one canonical message and return a normalized delivery result.
5. `register_commands(commands)`: register supported commands or report that the capability is unavailable.
6. `capabilities`: declare support for reply context, buttons, edits, media, callbacks, and webhook/polling modes.

Adapters must translate provider errors into the core's existing distinction between definite rejection, retryable rejection, and ambiguous delivery. An ambiguous send must remain quarantined; a new adapter must not silently turn an uncertain outcome into an automatic resend.

## 3. Canonical data contracts

The core should not store raw provider update objects as its application model.

### Inbound event

- `platform_id`
- `external_update_id` (opaque string; not assumed numeric)
- `chat_id` (opaque string)
- `sender_id` (optional opaque string)
- `message_id` (optional opaque string)
- `text` (optional)
- `reply_to_text` (optional, bounded and untrusted)
- `chat_type` (optional normalized value)
- `received_at`
- `provider_payload` (optional, bounded diagnostic data; never trusted as instructions)

### Outbound message

- `platform_id`
- `chat_id`
- `text`
- `reply_to_message_id` (optional)
- `components` (neutral buttons/actions, not provider JSON)
- `metadata` (small adapter-owned delivery metadata)

The adapter is responsible for converting neutral components into provider-specific keyboards or controls.

## 4. Persistence changes required before enabling a second platform

A later implementation should add a non-null `platform_id` with a backwards-compatible default of `soroush` to platform-owned records and scope identity constraints accordingly.

At minimum:

- `processed_updates`: unique key becomes `(platform_id, external_update_id)`.
- `message_queue`: store `platform_id`; per-chat FIFO eligibility becomes scoped to `(platform_id, chat_id)`.
- `delivery_outbox` and `dead_letters`: retain platform identity through retries and recovery.
- `conversation_history` and `feedback_pending`: scope records by `(platform_id, chat_id)`.
- Polling offsets: store one cursor per platform, rather than one global `polling_offset`.
- Admin state and operational counts: make platform scope explicit where the action is platform-specific.

Migrations must preserve existing rows as `platform_id='soroush'`, preserve current Soroush offsets and delivery receipts, and be repeatable on both SQLite and PostgreSQL. Do not enable a second adapter until the migration and rollback behavior have PostgreSQL integration coverage.

## 5. Delivery and worker model

Keep the durable queue and Outbox in the shared core. Resolve the adapter from each queue row's `platform_id` at delivery time. Isolate pollers per platform. Worker capacity may be shared initially, but separate per-platform concurrency/rate-limit controls should be available so one provider's outage or throttling does not starve another provider.

Outbox identity must include the platform-scoped queue identity. Persist neutral message components and render them only at the adapter boundary. Preserve the current conservative rule: a send with an ambiguous outcome is marked uncertain and is not automatically repeated unless the provider later supplies a documented idempotency mechanism.

## 6. Safe migration sequence

1. Introduce canonical event and outbound-message types without changing the Soroush behavior.
2. Wrap the current Soroush polling, parsing, command registration, and sending paths in a `SoroushAdapter`.
3. Add adapter contract tests using fixtures from the current Soroush API.
4. Add platform-scoped database columns and composite uniqueness with a migration defaulting all existing data to `soroush`.
5. Move reply keyboards to neutral components and render them in the adapter.
6. Add a second adapter only after a target platform is selected and its official API, authentication model, rate limits, update cursor semantics, and delivery guarantees are verified.
7. Run SQLite and isolated PostgreSQL tests, then provider-specific contract tests. Keep all changes Draft until explicitly approved.

## 7. Current decision and non-goals

Only Soroush Plus is currently selected for NeptonBot. No additional target platform, priority order, or provider API has been approved. Therefore this phase records the architecture boundary and migration requirements; it deliberately does not invent a second platform or add credentials, dependencies, paid infrastructure, or live API calls.

This document does not claim that NeptonBot is already multi-platform. Production remains unchanged.
