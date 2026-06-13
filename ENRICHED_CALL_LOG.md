# Enriched call_log Format

The call_log entries are now self-describing. Every event that matters has a typed `metadata` object on it, so you can stop parsing content strings and matching arrays by timestamp.

This doc covers what changed, what the new fields are, and how to migrate.

---

## What changed

Three things:

1. **System-log entries** now have an `action` field plus a structured `metadata` object with typed fields for every event (step changes, function calls, gather flow, session lifecycle, etc.)

2. **Tool entries** now have `function_name` in their metadata — you don't need to match against `swaig_log[]` by timestamp to get the function name.

3. **A new `call_timeline` array** is included in the post_data payload. It's a flat event stream extracted from the call_log with metadata flattened to top level. Optional convenience — all the data is already on the call_log entries.

### What didn't change

- `times[]` is untouched — still the same parallel array for per-response metrics.
- `swaig_log[]` is untouched — still carries full function args/results.
- Existing fields on user/assistant/tool entries are unchanged.
- System-log entries still have human-readable `content` strings.

---

## System-log entries

System-log entries pass through to the blessed `call_log` **as-is** (no flattening). The `metadata` is a nested object.

### Entry shape

```json
{
  "role": "system-log",
  "action": "step_change",
  "lang": "en",
  "timestamp": 1705300001123000,
  "tokens": 0,
  "content": "greet[0] -> collect[1] (ai_function)",
  "content_type": "Detected Speech",
  "metadata": {
    "context": "default",
    "step": "greet",
    "step_index": 0,
    "from_step": "greet",
    "from_index": 0,
    "to_step": "collect",
    "to_index": 1,
    "trigger": "ai_function"
  }
}
```

### action types and their metadata fields

Every system-log entry with metadata has `context`, `step`, and `step_index` stamped automatically (when steps are active).

#### Navigation

**`step_change`** — Step transition completed.

| Field | Type | Description |
|-------|------|-------------|
| `from_step` | string | Name of the step we left |
| `from_index` | number | Index of the step we left |
| `to_step` | string | Name of the step we entered |
| `to_index` | number | Index of the step we entered |
| `trigger` | string | What caused the transition (see trigger values below) |

**`context_enter`** — Context switch.

| Field | Type | Description |
|-------|------|-------------|
| `to_context` | string | Context we're entering |
| `from_context` | string | Context we came from |
| `trigger` | string | What caused the switch |
| `isolated` | boolean | Whether this context runs in isolation |

**`reset`** — Conversation reset.

| Field | Type | Description |
|-------|------|-------------|
| `consolidate` | boolean | Whether conversation was consolidated |
| `full_reset` | boolean | Whether this was a full reset |

#### Trigger values

| Value | Meaning |
|-------|---------|
| `"ai_function"` | Model called `next_step` or `change_context` |
| `"webhook_action"` | Webhook post_response action triggered it |
| `"gather_complete"` | All gather questions answered, `completion_action: "next_step"` |
| `"auto_advance"` | Step's `skip_to_next_step` condition was met |

#### Gather flow

**`gather_start`** — Gather sequence activated on a step.

| Field | Type | Description |
|-------|------|-------------|
| `output_key` | string or null | Key in `global_data` where answers are stored |
| `total_questions` | number | Total questions in this gather |

**`gather_question`** — A question is being presented.

| Field | Type | Description |
|-------|------|-------------|
| `key` | string | Answer key name |
| `question_index` | number | 0-based index of this question |
| `question_type` | string | Expected answer type ("string", "integer", etc.) |
| `requires_confirm` | boolean | Whether the model must confirm with the user |

**`gather_answer`** — Answer accepted and stored.

| Field | Type | Description |
|-------|------|-------------|
| `key` | string | Answer key name |
| `question_index` | number | Which question this answers |
| `attempt` | number | Attempt number (starts at 0) |
| `confirmed` | boolean | Whether user explicitly confirmed |

**`gather_reject`** — Answer rejected.

| Field | Type | Description |
|-------|------|-------------|
| `key` | string | Answer key name |
| `question_index` | number | Which question |
| `attempt` | number | Attempt number |
| `reason` | string | `"confirmation_required"` or `"missing_answer"` |

**`gather_complete`** — All questions answered.

| Field | Type | Description |
|-------|------|-------------|
| `output_key` | string or null | Where answers were stored in global_data |
| `answered` | number | Total questions answered |
| `completion_action` | string or null | e.g. `"next_step"` |

#### Functions

**`function_call`** — A SWAIG function was executed.

| Field | Type | Description |
|-------|------|-------------|
| `function` | string | Function name |
| `native` | boolean | true if internal (next_step, change_context, gather_submit) |
| `duration_ms` | number | Execution time in ms (0 if not measured) |
| `error` | string or null | Error message if the call failed |

**`function_error`** — Function call failed.

| Field | Type | Description |
|-------|------|-------------|
| `function` | string | Function name |
| `error_type` | string | Error category (e.g. "timeout") |
| `http_code` | number | HTTP status code |

#### Special Functions

These cover hooks and special SWAIG functions that are not AI-routed function calls.

**`startup_hook`** — Startup hook webhook completed.

| Field | Type | Description |
|-------|------|-------------|
| `function` | string | Always `"startup_hook"` |
| `duration_ms` | number | Webhook execution time in ms |
| `has_response` | boolean | Whether the webhook returned a response |
| `error` | string or null | Error message if execution failed |

**`hangup_hook`** — Hangup hook webhook completed.

| Field | Type | Description |
|-------|------|-------------|
| `function` | string | Always `"hangup_hook"` |
| `duration_ms` | number | Webhook execution time in ms |
| `has_response` | boolean | Whether the webhook returned a response |
| `error` | string or null | Error message if execution failed |

The hangup hook POST data includes two extra fields when the session ended due to a fatal error:

| POST field | Type | Description |
|------------|------|-------------|
| `fatal_error` | boolean | `true` when the session ended due to a fatal error |
| `error_reason` | string | One of `"token_exhaustion"`, `"llm_fatal"`, `"llm_max_retries"` |

If the webhook response contains an action with `SWML` and `transfer: true`, the call is transferred to that SWML instead of hanging up. Example response:

```json
{
  "response": "Transferring to error handler",
  "action": [{"SWML": "{\"version\":\"1.0.0\",\"sections\":{\"main\":[...]}}", "transfer": true}]
}
```

**`summarize_start`** — Summarize conversation mode activated.

| Field | Type | Description |
|-------|------|-------------|
| `function` | string | Always `"summarize_conversation"` |
| `model` | string or null | Model used for summarization |

**`check_for_input`** — Input poll webhook returned a response. Only logged when the webhook actually returns data (not on every poll).

| Field | Type | Description |
|-------|------|-------------|
| `function` | string | Always `"check_for_input"` |
| `duration_ms` | number | Time since last poll in ms |

#### Manual Say

**`manual_say`** — System-initiated speech to the user (error recovery, commands, etc.)

| Field | Type | Description |
|-------|------|-------------|
| `text` | string | What was spoken to the user |
| `is_error` | boolean | Whether this is an error recovery message |
| `error_reason` | string or null | Error type identifier (see legend below) |

**Error reason legend:**

| Reason | Spoken to caller | Meaning | Fatal? |
|--------|-----------------|---------|--------|
| `token_exhaustion` | *(nothing)* | Token limit exhausted after consolidation already attempted | Yes |
| `llm_fatal` | "I'm so sorry, I'm going to have to let you go. I apologize for the inconvenience." | Fatal LLM error (context overflow, invalid request) | Yes |
| `llm_max_retries` | "I'm sorry, I'm not able to continue right now. I apologize for the trouble." | LLM retries exhausted (only spoken if streaming) | Yes |
| `llm_retry` | "Pardon me, I lost my train of thought." | First LLM error, retrying | No |
| `invalid_function` | "Bear with me, I need to rethink that." | Model called a non-existent function | No |
| `invalid_params` | "One moment, let me try a different approach." | Model called a function with parameters that don't match schema | No |
| `webhook_http_failure` | "Just a second, I'm having a little trouble on my end." | Webhook HTTP/CURL failure after retries | No |
| `voice_config_error` | "Excuse me, I need to adjust something." | TTS voice selection/init failed, using gcloud fallback | No |
| `voice_runtime_error` | "Sorry about that, let me get myself sorted out." | TTS engine error during speech output, using gcloud fallback | No |
| `fatal_error_transfer` | *(nothing — NULL text)* | Timeline marker: fatal error recovery transferred the call via hangup hook SWML | No |

Fatal errors (`token_exhaustion`, `llm_fatal`, `llm_max_retries`) set `fatal_error_reason` on the session. When a `hangup_hook` is configured, the hook's POST data includes `"fatal_error": true` and `"error_reason": "<reason>"`. If the hook responds with `{"action": [{"SWML": "...", "transfer": true}]}`, the call is transferred to that SWML instead of hanging up.

#### Text Rewriting

These rewrite **user input** and are emitted as system-log entries (only when the text actually changed).

**`hearing_hint`** — Hearing hints rewrote user input.

| Field | Type | Description |
|-------|------|-------------|
| `original` | string | Input text before rewriting |
| `result` | string | Input text after rewriting |

**`auto_correct`** — LLM-based auto-correction rewrote user input.

| Field | Type | Description |
|-------|------|-------------|
| `original` | string | Raw ASR text before correction |
| `corrected` | string | Text after LLM correction (redacted version if `redact_prompt` is on) |
| `redacted` | boolean | `true` if the corrected text was redacted for PII (only present when redaction occurred) |

> **Pronounce rules and text normalization (ITN/TN) are NOT system-log entries.** The rewrite is embedded on the affected entry instead — ITN on the user entry's `original` field, pronounce/TN on the assistant entry's `pronounced` field — and surfaced as synthetic `text_normalize` / `pronounce` events in `call_timeline` (see [Synthetic events](#synthetic-events)).

#### Session

**`session_start`** — AI session began.

| Field | Type | Description |
|-------|------|-------------|
| `model` | string or null | Model name (e.g. "gpt-4o") |

**`session_end`** — AI session ended.

| Field | Type | Description |
|-------|------|-------------|
| `reason` | string | `"normal"`, `"hard_timeout"`, etc. |
| `ended_by` | string | `"caller"`, `"system"`, etc. |

#### Misc

**`attention_timeout`** — User didn't respond within timeout.

| Field | Type | Description |
|-------|------|-------------|
| `timeout_ms` | number | Timeout value in ms |

**`filler`** — Filler audio played.

| Field | Type | Description |
|-------|------|-------------|
| `text` | string | Filler text spoken |
| `language` | string | Language code |
| `filler_type` | string | `"thinking"`, etc. |

---

## Tool entries: function_name

Tool entries (`role: "tool"`) now have `function_name` in their metadata. In the blessed call_log, metadata is flattened to top level:

```json
{
  "role": "tool",
  "tool_call_id": "call_abc123",
  "content": "Order found",
  "timestamp": 1705300015890000,
  "function_name": "check_order",
  "latency": 200,
  "function_latency": 100,
  "execution_latency": 100,
  "start_timestamp": 1705300015690000,
  "end_timestamp": 1705300015890000
}
```

This means you can get the function name directly from the tool entry instead of matching against `swaig_log[]` by timestamp:

```javascript
// Before (fragile 5-second timestamp match)
const match = swaigLog.find(s =>
  Math.abs(s.epoch_time - toolEntry.timestamp / 1e6) < 5
);
const funcName = match?.command_name;

// After
const funcName = toolEntry.function_name;
```

Note: `function_name` is on the **tool** entry (the result), not the **assistant** entry (which has `tool_calls` with the call ID). The assistant entry with `tool_calls` still has its existing shape.

---

## User and assistant entries

These are unchanged structurally. Metadata is flattened to top level in blessed output (same as before). User entries have `confidence`, `content_type`, `start_timestamp`, `end_timestamp`, the `speaking_to_*` / `turn_detection_*` timing fields, and — when mod_deepgram supplies them on the final — the `entity` / `eot` / `timing` ASR-telemetry blocks (see below). Assistant entries have `latency`, `utterance_latency`, `audio_latency`, and `acoustic_latency`.

### User entry ASR telemetry

When mod_deepgram delivers turn telemetry on a speech final, three nested blocks ride along on the user entry's metadata (and flatten to top level in blessed output / `call_timeline` like every other metadata field). They are **absent** on DTMF, on injected/system-sourced user turns, and on finals the engine didn't enrich — so test for presence before reading.

**`entity`** — the validated structured shape the turn resolved to. Present **only when a complete entity is recognized** (so `entity` present ⇒ you have validated structured data; absent ⇒ text only).

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `phone` · `email` · `ssn` · `card` · `uuid` · `url` · `money` · `time` · `date` · `ordinal` |
| `value` | string | The **canonical** form — libphonenumber E.164 for a phone, the assembled `local@domain.tld` for an email, the formatted value otherwise. Read this back, not the raw ASR text. |
| `valid` | boolean | `true` once it passed validation (phone via libphonenumber, ssn ranges, card Luhn). A phone-shaped run that fails validation is `false`. |

**`eot`** — how the turn ended (makes "latency = f(turn-end confidence)" observable).

| Field | Type | Description |
|-------|------|-------------|
| `basis` | string | `entity_snap` (released on the short settle — high confidence) · `growth_stop` (waited out the growth-stall) · `ceiling` (force-released at the hold cap — **low confidence, consider re-prompting**) · `natural` (a normal un-held turn) |
| `confidence` | number | Acoustic Smart-Turn end-of-turn probability at commit (0–1) |

**`timing`** — observability for tuning the hold/commit.

| Field | Type | Description |
|-------|------|-------------|
| `hold_ms` | number | How long the dictation hold kept the turn open (0 for an un-held turn) |
| `commit_latency_ms` | number | Last-token → commit (the stall window that elapsed) |
| `segments` | number | How many engine finals fused into the turn (1 = clean single turn; >1 = a held multi-segment dictation) |

**Example user entry (blessed, metadata flattened):**

```json
{
  "role": "user",
  "content": "415-555-0192",
  "confidence": 0.98,
  "content_type": "Detected Speech",
  "start_timestamp": 1705300002000000,
  "end_timestamp": 1705300003789000,
  "entity": { "type": "phone", "value": "+14155550192", "valid": true },
  "eot":    { "basis": "entity_snap", "confidence": 0.967 },
  "timing": { "hold_ms": 308, "commit_latency_ms": 308, "segments": 2 }
}
```


### Latency fields on assistant entries

The pre-computed latency fields are in **milliseconds**. They measure different intervals; which one to trust depends on what you're comparing against. The raw wall-clock μs timestamps below the table are exposed so consumers can compute custom derivations.

| Field | Start point | End point | When to use |
|-------|-------------|-----------|-------------|
| `latency` | Text: start of user turn at LLM-send time (`request_detect_time` = ASR final) / OART: `response.created` | Text: first response token / OART: `response.output_item.added` | Measures model time-to-first-token; infrastructure-level LLM benchmarking |
| `utterance_latency` | Same as `latency` | Text: first completed utterance segment (phrase stop) / OART: first `response.audio.delta` received | TTS-ready latency; how long until something is ready to speak |
| `audio_latency` | Same as `latency` | Text: first non-silence PCM frame written by output thread (`audio_response_time`) / OART: first `response.audio.delta` received | Server-side "first audio available" — still before codec/RTP pipeline |
| `acoustic_latency` | Text: end of user's last spoken word (from Deepgram word-level timestamps, anchored via `audio_anchor_wall_us` / `audio_anchor_stream_us`); falls back to last `detected-partial-speech` event if mod_deepgram didn't supply word-level fields / OART: `input_audio_buffer.speech_stopped` (server's end-of-turn decision) | Text: first non-silence PCM frame written (`audio_response_time`) / OART: first non-silence PCM frame written via `switch_core_session_write_frame` (`first_audio_write_time`) | Comparison against wav-based analyzers measuring acoustic-silence → first-audio-in-recording |
| `eos_to_push_latency` | End of user's last spoken word (from word-level timestamps) | mod_deepgram's `status_pushed_wall_us` — when the final result was deliverable | Isolates ASR / turn-detection / fusion overhead from the model+TTS pipeline that `audio_latency` rolls in. Text mode only; absent when word-level timing fields aren't provided |
| `dg_decision_latency` | mod_deepgram's `turn_decided_wall_us` — when the turn-end decision fired | mod_deepgram's `status_pushed_wall_us` | Pure mod_deepgram internal queue-push overhead. Useful for spotting implementation-side delays distinct from acoustic or fusion latency |

**Start-point differences explained:**

- Text mode `audio_latency` uses `request_detect_time`, which is set to the moment the Deepgram **final** transcript event fires. Deepgram emits this after its VAD's silence-hang window closes (typically 200–500 ms after the user actually stopped making sound), so the start is later than acoustic silence.
- Text mode `acoustic_latency` uses the **end of the user's last spoken word** as its anchor, computed from `audio_anchor_wall_us + (last_word_end_stream_us - audio_anchor_stream_us)` against mod_deepgram's word-level timestamps. This is grounded directly in the audio waveform rather than the partial-delivery cadence, so it's more precise than `last_partial_time`. When those word-level fields aren't supplied (older mod_deepgram, or engines that don't emit them), it falls back to `last_partial_time` — the timestamp of the most recent **partial** transcript event, which fires close to (but not exactly at) acoustic silence.
- OART mode `audio_latency` measures only the model generation window (`response.created` → first audio delta). `response.created` fires after the server has decided the user's turn is over — with `server_vad` this tracks Deepgram-style behavior, with `semantic_vad` it can fire predictively while the user is still trailing off.
- OART mode `acoustic_latency` starts at `user_speech_end_time` (the server's `input_audio_buffer.speech_stopped` timestamp) and ends at `first_audio_write_time` (when the first real PCM frame is actually written to FreeSWITCH's write pipeline). This absorbs the entire model round-trip and also the buffering between "delta received" and "frame written."

**End-point differences explained:**

- Text and OART `audio_latency` mark the server-side "first audio byte available" event — for OART this is the first `response.audio.delta` received from the WebSocket; for text it's the first non-silence frame handed to the output thread. In both cases, there is still frame-buffer, codec, and RTP work to happen before the caller actually hears audio.
- `acoustic_latency`'s end point is the first real PCM frame the write loop hands off via `switch_core_session_write_frame`. This is what an external wav analyzer sees as "bot audio starts here" (modulo one codec frame / ~20 ms of RTP scheduling).

**Expected numeric relationship** between the metrics for the same turn: `latency ≤ utterance_latency ≤ audio_latency ≤ acoustic_latency`. `acoustic_latency` is the only one anchored at the user's actual end of speech (last word's audio energy ending, or last partial as fallback), while the other three anchor at `request_detect_time` (Deepgram final delivery), which fires *after* the silence-hang has closed. Earlier start + same end ⇒ a larger interval, so `acoustic_latency` is typically the largest of the four — matching what a wav-based analyzer would measure end-to-end (e.g. the spec-verified case where `audio_latency=561 ms`, `acoustic_latency=1088 ms`, wav-measured=1050 ms). For OART mode the same ordering holds: `acoustic_latency` starts at `input_audio_buffer.speech_stopped` (similar to text's anchor) but ends at write-frame rather than delta-received, so it stays the largest.

If you are comparing against a wav-based acoustic analyzer (measuring from the last sample of user audio in the recording to the first sample of assistant audio in the recording), use `acoustic_latency`. If you are measuring server-side model latency alone, use `audio_latency`.

**`null` vs positive value:** `acoustic_latency`, `eos_to_push_latency`, and `dg_decision_latency` are always present in the schema on assistant entries. A positive number is a measured value; JSON `null` means the metric is not derivable for this turn. The null cases are:

- **Greeting / timeout / end-of-call summary:** no preceding user turn → no acoustic anchor.
- **`assistant-manual` entries** (queued `ais_say` fillers like "Querying the knowledge base"): these don't go through the LLM round-trip and have no latency of their own. All three derived metrics are always `null` for `assistant-manual` to prevent inheriting the surrounding response's values.

Consumers should treat `null` as "not measured," distinct from `0` (which would only ever appear if the bot somehow produced audio at the same μs as the user's last word — never in practice).

The three raw `*_wall_us` fields follow the same anchor convention: present (a wall-clock μs value) when this turn responds to a user utterance, omitted on turns with no anchor.

### Raw wall-clock timestamps for custom derivations

Wall-clock μs timestamps exposed on the assistant entry alongside the pre-computed latencies. Use these when you need a measurement the pre-computed fields don't cover (e.g. subtracting from any other wall-clock event in your stack). All three come from mod_deepgram's final result JSON; absent in OART mode, on turns with no preceding user-speech anchor, or when mod_deepgram didn't supply them.

The values are snapshotted at the end of the most recent accepted user final and persist through the entire response cycle (LLM call, tool calls, reasoning, manual_says) — so they reflect the user turn the response is answering, not whatever live ASR state was when the audio thread happened to write a frame. Any subsequent user partials/finals only replace the snapshot after the *next* accepted user final.

| Field | Meaning |
|-------|---------|
| `last_word_end_wall_us` | Wall-clock μs when the user's last spoken word's audio energy ended. Computed locally from `audio_anchor_wall_us + (engine_data[0].channel.alternatives[0].words[LAST].end × 1e6 - audio_anchor_stream_us)`. The most precise "user stopped talking" anchor available. |
| `turn_decided_wall_us` | Wall-clock μs when mod_deepgram decided the turn ended — fusion fire OR Deepgram-natural `speech_final` OR libfvad `STOP_TALKING`. |
| `status_pushed_wall_us` | Wall-clock μs when `SignalWireRecognitionComplete` was pushed to the status queue (i.e. the moment the next `asr_check_results` poll would return SUCCESS). |

**Custom derivations:**

```
decision_to_push_us = status_pushed_wall_us - turn_decided_wall_us   (mod_deepgram internal overhead)
eos_to_push_us      = status_pushed_wall_us - last_word_end_wall_us  (turn-detection + ASR overhead)
```

These two are also exposed as the pre-computed `dg_decision_latency` and `eos_to_push_latency` fields in milliseconds.

### Stacking the latencies (full-pipeline view)

The pre-existing `latency` / `utterance_latency` / `audio_latency` fields are anchored at `request_detect_time` — the moment FreeSWITCH receives the Deepgram final event. That's *after* the user actually stopped talking, so old consumers stacking those three see only the **model + TTS + write** portion of the pipeline.

The new `acoustic_latency` (now sharpened) and `eos_to_push_latency` are anchored at `last_word_end_wall_us` — the moment user audio energy ended. They surface the **turn-detection** portion that was previously invisible.

#### Timeline

```
                                  ASR poll
                                  delay (~20ms,
                                  not exposed)
                                       │
[user stopped]    [final pushed]   [FS reads it]              [bot audio plays]
     │                  │                │                            │
     ▼                  ▼                ▼                            ▼
     ├ eos_to_push  ───┤
                       │
                                        │
                                        ├ latency ──────────────┤
                                        ├ utterance_latency ─────────┤
                                        ├ audio_latency ───────────────┤
     │
     ├ acoustic_latency ──────────────────────────────────────────────┤
```

#### Identity

```
acoustic_latency ≈ eos_to_push_latency + audio_latency + (asr_check_results poll delay)
```

The poll delay is the only gap not directly exposed. Back it out as `acoustic_latency - eos_to_push_latency - audio_latency` if you need it.

#### Bar-segment breakdown for new consumers

A consumer building a stacked-bar visualization anchored at "user-stopped-talking" can decompose the full timeline like this:

| Bar segment | Field |
|---|---|
| Turn detection (user-stopped → final deliverable) | `eos_to_push_latency` |
| └─ of which mod_deepgram internal (decision → publish) | `dg_decision_latency` |
| ASR check poll delay | (derived: `acoustic_latency - eos_to_push_latency - audio_latency`) |
| Model time-to-first-token | `latency` |
| Model → utterance ready | `utterance_latency - latency` |
| Utterance → audio frame | `audio_latency - utterance_latency` |
| **Total user-perceived** | `acoustic_latency` |

#### What old consumers should do

Nothing required. `latency` / `utterance_latency` / `audio_latency` keep the same start point (`request_detect_time`) and same meanings. Bar-graph dashboards that stacked those three continue to work.

To gain the new "turn detection" segment, add `eos_to_push_latency` as the bottom-most bar and shift everything else right by that amount (plus the small poll delay). Or switch to `acoustic_latency` as the full-extent reference and break it down per the table above.

### Assistant barge-in metadata

When an assistant response is interrupted by the user (normal barge, not transparent barge), the assistant entry in `raw_array` gets additional metadata fields:

| Field | Type | Description |
|-------|------|-------------|
| `barged` | boolean | `true` — response was interrupted by user |
| `barge_elapsed_ms` | number | Milliseconds of audio that played before barge |
| `text_heard_approx` | string | Approximate text heard before barge (substring to word boundary) |
| `text_spoken_total` | string | Full SWAIG-stripped text that would have been spoken |

`text_heard_approx` is estimated from `barge_elapsed_ms` and a self-calibrating TTS speaking rate (seeded at ~15 chars/sec, updated from each completed non-barged response in the session). Consumers who want different estimation can use `barge_elapsed_ms` and `text_spoken_total` directly.

These fields auto-flatten into `call_timeline` `ai_response` events via `build_call_timeline`.

**Example:**

```json
{
  "role": "assistant",
  "content": "Thank you for calling. I can help you with your order. Let me look that up for you.",
  "metadata": {
    "latency": 500,
    "utterance_latency": 450,
    "audio_latency": 480,
    "acoustic_latency": 720,
    "eos_to_push_latency": 95,
    "dg_decision_latency": 12,
    "last_word_end_wall_us": 1705300004380000,
    "turn_decided_wall_us": 1705300004463000,
    "status_pushed_wall_us": 1705300004475000,
    "barged": true,
    "barge_elapsed_ms": 3200,
    "text_heard_approx": "Thank you for calling. I can help you with your",
    "text_spoken_total": "Thank you for calling. I can help you with your order. Let me look that up for you."
  }
}
```

---

## call_timeline (optional)

A new `call_timeline` array is included at the top level of the post_data payload, alongside `call_log`, `swaig_log`, `times[]`, etc. It's only present when there are entries with metadata.

It's a flat event stream built from the call_log. Each event has:

- `ts` — timestamp (microseconds, same as call_log timestamps)
- `type` — event type string
- All metadata fields flattened to top level

### Type mapping

| call_log role | type value |
|---------------|------------|
| system-log | Value of `action` field (e.g. `"step_change"`, `"function_call"`) |
| user | `"user_input"` |
| assistant / assistant-manual | `"ai_response"` |
| tool | `"tool_result"` |

Entries without metadata are skipped. System prompts, pvt entries, etc. don't appear.

### Synthetic events

Beyond the role-mapped events above, `build_call_timeline` emits two **synthetic** events derived from entry fields (no system-log entry of their own exists for these):

| type | Emitted from | Fields |
|------|--------------|--------|
| `text_normalize` | A user entry whose `original` differs from `content` (ITN ran on ASR input) | `direction: "itn"`, `original`, `normalized` |
| `pronounce` | An assistant entry whose `pronounced` differs from `content` (pronounce rules / TN ran on TTS output) | `original`, `result` |

### Example

```json
{
  "call_timeline": [
    {"ts": 1705300001123000, "type": "session_start", "context": "default", "step": "greeting", "model": "gpt-4o"},
    {"ts": 1705300001456000, "type": "ai_response", "latency": 450, "utterance_latency": 200, "audio_latency": 250},
    {"ts": 1705300003789000, "type": "user_input", "confidence": 0.955, "start_timestamp": 1705300002000000, "end_timestamp": 1705300003789000, "entity": {"type": "phone", "value": "+14155550192", "valid": true}, "eot": {"basis": "entity_snap", "confidence": 0.967}},
    {"ts": 1705300005012000, "type": "step_change", "context": "default", "from_step": "greeting", "to_step": "collect_info", "trigger": "ai_function"},
    {"ts": 1705300010000000, "type": "gather_start", "output_key": "profile", "total_questions": 3},
    {"ts": 1705300010100000, "type": "gather_question", "key": "first_name", "question_index": 0, "question_type": "string", "requires_confirm": false},
    {"ts": 1705300012000000, "type": "gather_answer", "key": "first_name", "question_index": 0, "attempt": 0, "confirmed": false},
    {"ts": 1705300015890000, "type": "function_call", "context": "default", "step": "lookup", "function": "check_order", "native": false, "duration_ms": 234},
    {"ts": 1705300016000000, "type": "tool_result", "function_name": "check_order", "latency": 234, "execution_latency": 200},
    {"ts": 1705300025456000, "type": "session_end", "reason": "hangup", "ended_by": "caller"}
  ]
}
```

You can use `call_timeline` instead of walking call_log yourself if you just need a flat event stream. All the same data is on the call_log entries.

---

## Migration guide

### Step changes

```javascript
// Before: content string parsing + action field
if (entry.action === 'change_step' && entry.name) { ... }
// or
const match = entry.content.match(/Calling function: next_step\(([^)]+)\)/);

// After: structured metadata
if (entry.action === 'step_change') {
  const { from_step, to_step, to_index, trigger } = entry.metadata;
}
```

Note the action name changed from `change_step` to `step_change`. The old `name` and `index` top-level fields are replaced by `to_step` and `to_index` inside `metadata`. The `from_step`/`from_index` fields are new — you couldn't get "where we came from" before.

### Context changes

```javascript
// Before: content string parsing
const match = entry.content.match(/Calling function: change_context\(([^)]+)\)/);

// After
if (entry.action === 'context_enter') {
  const { to_context, from_context, trigger, isolated } = entry.metadata;
}
```

### Function identity on tool entries

```javascript
// Before: 5-second timestamp match against swaig_log
const match = swaigLog.find(s =>
  Math.abs(s.epoch_time - entry.timestamp / 1e6) < 5
);
const funcName = match?.command_name;

// After: direct field on tool entry
const funcName = entry.function_name;
```

### Function call events from system-log

```javascript
// Before: parsed from content "Calling function: <name>"
// After: structured metadata
if (entry.action === 'function_call') {
  const { function: funcName, native, duration_ms, error } = entry.metadata;
}
```

### Gather flow (new — no equivalent before)

```javascript
// No previous way to track gather flow in call_log.
// Now you get the full sequence:
switch (entry.action) {
  case 'gather_start':    // gather activated
  case 'gather_question': // question presented
  case 'gather_answer':   // answer accepted
  case 'gather_reject':   // answer rejected (confirmation_required, missing_answer)
  case 'gather_complete': // all questions done
}
```

### Using call_timeline instead of walking call_log

```javascript
// If call_timeline exists, you get a flat event stream
if (data.call_timeline) {
  for (const event of data.call_timeline) {
    switch (event.type) {
      case 'session_start':
      case 'step_change':
      case 'function_call':
      case 'user_input':
      case 'ai_response':
      case 'tool_result':
      case 'session_end':
        // All metadata fields are top-level on the event
        break;
    }
  }
}
```

---

## What you can stop doing

| Old pattern | Why it's unnecessary |
|-------------|---------------------|
| Parse system-log `content` strings to classify events | Use `action` field + `metadata` |
| Match `swaig_log[]` to `call_log[]` by 5-second timestamp window | Use `function_name` on tool entries for identity; use `function_call` system-log events for execution tracking |
| 50ms time window to determine if step change was tool-forced | Check `trigger` field on `step_change` metadata |
| Content regex for `next_step(...)` / `change_context(...)` | Use `step_change` / `context_enter` action types |

### What you still need swaig_log[] for

- Full function arguments (`command_arg`) — system-log `function_call` events don't carry the full args
- Full function results — system-log events carry a summary, swaig_log has the complete response
- `post_response` actions — what the webhook told the system to do after the function call

### What you still need times[] for

- `answer_time`, `tps`, `token_time`, `response_word_count` per assistant response
- Full response text (times entries carry the complete text; call_log assistant entries do too, but times[] is the established source)

**Important — `times[]` is per-generation, NOT per-spoken-turn.** Every LLM round-trip writes an entry, including abandoned/regenerated responses (a barge-interrupted "Got it…" and the retry that replaced it both appear) and empty tool-call rounds (the LLM round that just emitted a tool call carries no spoken text but still records timing). The blessed `call_log` collapses these into per-spoken-turn entries; `times[]` does not. **Match `times[]` entries to spoken turns by response text, not by index** — array index N in `times[]` is generally NOT the Nth spoken assistant entry.

---

## Backwards compatibility

All new fields are **additive**. Nothing was removed from the existing format:

- System-log entries still have `content` strings (human-readable summary)
- `swaig_log[]` still exists with full function details
- `times[]` still exists with per-response performance metrics
- User/assistant/tool entry shapes are unchanged (metadata flattening was already in place)

The only breaking change: system-log `action` values are different for step/context changes:
- `change_step` → `step_change`
- `change_context` → `context_enter`

If you're currently keying on `action === 'change_step'`, update to `action === 'step_change'`.

The old `name` and `index` top-level fields on step change system-log entries are gone. They're now `to_step` and `to_index` inside `metadata`. If you read `entry.name` for step changes, change to `entry.metadata.to_step`.
