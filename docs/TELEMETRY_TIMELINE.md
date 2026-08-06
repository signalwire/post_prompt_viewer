# Unified turn-timeline telemetry — coordination spec (mod_deepgram ↔ mod_openai)

> **STATUS 2026-08-02 — implemented; kept for the reasoning, not as a to-do.**
>
> Written 2026-06-14 and lived in `/tmp` for seven weeks, which is why it is
> checked in now. §5's action items are **done on both sides**; read them as
> history. What stays valuable is §1 (the measurement proving the "poll" was a
> stamping artifact, not latency the caller heard) and §2's argument that
> redefining `acoustic_latency` alone cannot work, because
> `first_audio − last_word_end ≡ eos_to_push + audio + poll` is an identity.
> That is the part someone will otherwise re-derive from scratch.
>
> **Where the implementation went beyond this spec:**
> - `first_audio` is stamped at the true first PCM write (`oart.c`), which is
>   what actually collapsed the poll. The spec called for it; worth knowing it
>   is the load-bearing piece.
> - The anchor briefly moved to `suspected_end`, then back to `last_word_end`.
>   `suspected_end` was **never emitted by mod_deepgram** and has since been
>   deleted from mod_openai entirely — do not reintroduce it.
> - mod_deepgram emits **four** caller stamps: `speech_start`, `last_word_end`,
>   `turn_decided`, `status_pushed`.
> - Injected silence corrupted the stream→wall mapping behind `last_word_end`,
>   which roughly doubled reported `acoustic_latency` until it was fixed (#84).
>   Reported turn latency fell ~1000 → 471 ms on a live call — a measurement
>   correction, not a speedup.
> - mod_openai briefly reconstructed the anchor from
>   `status_pushed − timing.commit_latency_ms`. That measures the wrong quantity
>   (−741 ms mean, −1563 ms worst, always under) and was removed. **`timing.*`
>   is hold telemetry and must not be used to reconstruct timeline anchors.**
> - The acoustic-offset stamp discussed as future work was declined by both
>   sides: the residual is ~107 ms with mixed sign, i.e. word-boundary noise
>   rather than the systematic bias such a stamp would correct.

Status: **verified against call 03827fdf**. Proposes one schema both modules emit
so a generic renderer needs zero anchor math, and `acoustic_latency` stops lying.

## 1. The problem (measured, not asserted)

`acoustic_latency` overstates the recorded AI onset by a variable **"poll"** lag.
On 03827fdf, for every turn:

```
acoustic_latency  ==  eos_to_push_latency  +  audio_latency  +  poll
```

| turn | acoustic | eos_to_push | audio | eos+audio | **poll** |
|---|---|---|---|---|---|
| 2  | 2066 | 591 | 504 | 1095 | **971** |
| 4  | 1494 | 778 | 158 | 936  | **558** |
| 6  | 2390 | 1233| 260 | 1493 | **897** |
| 9  | 2588 | 1534| 1027| 2561 | **27**  |
| 13 | 1775 | 1091| 673 | 1764 | **11**  |

Poll across the call: **min 11, max 971, mean 560 ms** (27 turns). The consumer's
wav analysis shows the real recorded onset tracks `eos_to_push + audio_latency`
(within ~100 ms) — i.e. **the poll is an artifact of late stamping, not real
silence the caller hears**, yet it lands entirely in `acoustic_latency`.

### Root cause: two clocks, one hidden anchor
- **Caller half** (mod_deepgram) → absolute wall-µs stamps on `switch_time_now`.
- **Model half** (mod_openai) → *relative ms* (`latency`, `utterance_latency`,
  `audio_latency`) anchored at `request_detect_time`, **which is never emitted**.

`acoustic_latency` and `audio_latency` share the end anchor `audio_response_time`,
but `audio_latency` starts at `request_detect_time` (both shifted by the poll →
cancels) while `acoustic_latency` starts at the *real* `last_word_end` (only the
end shifted → poll leaks in). Hence `acoustic ≈ recorded + poll`.

## 1b. The anchor is PER USER TURN, not per response — read this before averaging

`last_word_end` marks one thing: the end of a caller's turn. mod_deepgram emits
it once, on that turn's final.

A consumer may then see it **repeated across several `ai_response` events** —
fillers, gather sub-turns, chained responses after a tool call — because every
one of those responses is genuinely anchored to that same caller turn. That is
correct and consistent with this spec. It is also a trap, reported from
production (call 76b49482):

```
lwe -> first_audio =  1542 ms   <- filler; what the caller actually experienced
lwe -> first_audio =  3142 ms   <- follow-up, same lwe
lwe -> first_audio = 40062 ms   <- real response after a 40 s tool wait, same lwe
```

Averaging `acoustic_latency` across those gives **14.9 s "mouth-to-ear"** for a
call where the caller heard something at **1.5 s**. The 40 s is real elapsed
time; it is not caller-perceived latency, because the caller was not sitting in
silence waiting for it — they had already been answered twice.

**So: `acoustic_latency` is only a caller-experience number on the FIRST
response of a turn.** On later responses it measures "time since the caller
stopped talking", which is a different and usually uninteresting quantity.

Any consumer computing mouth-to-ear must reduce to one entry per user turn
first. Grouping by `stamps_us.last_word_end` works — it is unique per turn at
microsecond resolution — and mod_openai now ships that dedupe
(post-prompt-viewer `ebe949f`). A `first_response_for_turn` flag on the response
event would make it explicit rather than inferred, and belongs on the mod_openai
side since `ai_response` is theirs.

**Interaction worth knowing:** when mod_deepgram cannot produce a trustworthy
anchor it emits **no** `last_word_end` rather than a plausible wrong one (see
`ANCHOR_ABSENT` in the log, and §4's rule that a missing number beats a wrong
one). Grouping-by-anchor therefore cannot group those turns at all. They have no
`acoustic_latency` to average either, so they drop out of the metric rather than
corrupting it — but a consumer that assumes every response carries an anchor
will not find one.

## 1c. When we CANNOT produce an anchor we emit nothing — and what that means for you

`last_word_end` is an audio position mapped through the send anchor and
corrected for injected silence. When that mapping cannot be trusted,
mod_deepgram emits **no** `last_word_end` rather than a plausible wrong one.

That is deliberate, and it is the same rule as §4's: **a plausible wrong number
is worse than a missing one, because it gets trusted.** A consumer that receives
a fabricated anchor will compute a fabricated mouth-to-ear latency and have no
way to know. A consumer that receives nothing at least knows it has nothing.

Three paths omit it, and all three now log (added 2026-08-02):

```
ANCHOR_ABSENT reason=no_word_end      the committed turn carried no words array
ANCHOR_ABSENT reason=no_audio_anchor  no stream->wall anchor captured yet
"... precedes its turn's onset ... omitting (bad anchor)"   WARNING; the guard rejected it
```

### What a consumer should do

**Those turns cannot be attributed to a user turn at all.** Not "attributed with
latency 0" — not attributable. Drop them from caller-experience metrics rather
than letting an absent anchor read as zero.

This is orthogonal to the per-turn reduction in §1b: an anchor-absent response
can never anchor a first-response flag either, so a `first_response_for_turn`
style marker does not rescue it. They are two separate reasons a response may
not carry usable caller-latency, and a consumer needs to handle both.

### How often, and why

Measured by the consumer over an 18-call corpus (150 non-silent responses),
2026-08-06:

```
  17 anchor-absent (11.3%)
  16 opening greeting — the agent speaks first, there IS no user turn      expected
   1 unexplained — a closing turn, caller's final short utterance
```

**Strip greetings and unexplained anchor loss is ~0.67%.** The dominant cause is
structural and correct: an agent-initiated greeting has no preceding caller turn
to anchor to, so no anchor can exist. That is not a defect and no logging will
ever appear for it — the paths above are about turns we *had* and could not map,
not about responses that never had one.

The one residual case (call `642c5afa`, turn 24, "You're welcome Brian! Have a
great day", `audio_latency` 1367 ms with no anchor) looks like a closing turn
where a very short caller utterance — "thanks", "bye" — did not yield a
trustworthy anchor. Short finals are the plausible suspect for
`reason=no_word_end`: a very short utterance can produce an is_final whose
alternative carries no `words` array, and the anchor is derived from
`words[last].end`. Unconfirmed — isolating it needs one INFO-level log
correlated against that call's payload.

## 2. The fix: one clock, every event a stamp

Emit every pipeline event as a wall-µs stamp on the single `switch_time_now` clock
(the same one `record_call_start` uses). Every latency becomes `stamp_b − stamp_a`;
recording alignment is `stamp − record_call_start`; the renderer just sorts.

```jsonc
"stamps_us": {
  "speech_start":    1781389930803000,  // caller began                  ┐ mod_deepgram
  "last_word_end":   1781389931223000,  // caller's last word (= wav human-stop) │ EMITTED
  "turn_decided":    1781389931663000,  // EOT committed                  │ TODAY,
  "status_pushed":   1781389931814000,  // result deliverable             ┘ verified
  "request_detect":  1781389931834000,  // final read / model send   ◀ mod_openai — NEW (today: hidden)
  "first_token":     1781389932526000,  // model first token         ◀ mod_openai — NEW (today: `latency` ms)
  "first_utterance": 1781389932573000,  // first speakable           ◀ mod_openai — NEW (today: `utterance_latency`)
  "first_audio":     1781389932723000   // first PCM frame written   ◀ mod_openai — NEW (today: `audio_latency`)
}
```

## 3. Who emits what

| stamp | owner | status |
|---|---|---|
| `speech_start`, `last_word_end`, `turn_decided`, `status_pushed` | **mod_deepgram** | **DONE** — now emitted as a `stamps_us` object (short keys, the caller half) **and** the legacy flat `*_wall_us` fields (deprecated). One clock, monotonic & poll-free on every turn of 03827fdf (`speech_start ≤ last_word_end ≤ turn_decided ≤ status_pushed` held everywhere) |
| `request_detect`, `first_token`, `first_utterance`, `first_audio` | **mod_openai** | **TODO** — the moments already exist (`First Token, elapsed 0.122` is logged at a wall instant in `webhook.c:1482`, on the *same* clock as the caller stamps); only the *raw µs* needs emitting instead of the `request_detect`-relative ms |

## 4. Every latency = a subtraction (keep the field names, redefine from stamps)

| field | = |
|---|---|
| `latency` (TTFT) | `first_token − request_detect` |
| `utterance_latency` | `first_utterance − request_detect` |
| `audio_latency` | `first_audio − request_detect` |
| `eos_to_push_latency` | `status_pushed − last_word_end` |
| `dg_decision_latency` | `status_pushed − turn_decided` |
| **`acoustic_latency`** | **`first_audio − last_word_end`** — = the recorded silence **only when `first_audio` is stamped at the true PCM-write** (see note) |
| **`poll`** (NEW) | **`request_detect − status_pushed`** — the read lag as its own field; collapses to ~0 once the stamps are true |

> **Resolving the apparent paradox (the consumer's catch — correct).**
> Algebraically `first_audio − last_word_end ≡ eos_to_push + audio_latency + poll`
> *always*; the redefinition does **not** by itself remove the poll. The poll is
> removed by stamping `first_audio` (and `request_detect`) at their **true**
> moments instead of on a poll/batch tick. Today they're recorded ~`poll` late, so
> the `first_audio` stamp sits ~0.5–1 s after the real first PCM. The wav proves
> the true onset is at `eos_to_push + audio_latency` (no poll). Therefore:
> - **Stamp `first_audio` truly → `poll → ~0`**, and `acoustic = first_audio −
>   last_word_end` *is* the recorded silence with nothing to subtract. **This is
>   the fix.** It also makes `stamps_us.first_audio` land on the waveform — a late
>   stamp plots the AI onset ~0.5–1 s off, which defeats the overlay that is the
>   entire point of the spec.
> - Do **not** keep the late `first_audio` and either (a) leave `acoustic` inflated
>   with a "subtract poll" footnote or (b) redefine `acoustic = eos_to_push +
>   audio_latency`. (a) still lies; both leave `stamps_us.first_audio` wrong on the
>   timeline. (b) is acceptable only as a last resort if stamping truly is
>   genuinely impossible — and even then the timeline's `first_audio` stays off.

## 5. mod_openai action items

1. **This is the actual fix.** Stamp `request_detect` / `first_token` /
   `first_utterance` / `first_audio` at their **true moments**, inline on
   `switch_time_now()` — e.g. `first_audio` *where you write the first PCM frame*,
   exactly like the First-Token stamp you already take — **not** on a poll/batch
   tick. Late stamping is what created the poll leak *and* would plant
   `stamps_us.first_audio` ~0.5–1 s off the waveform. With true stamps,
   `acoustic_latency = first_audio − last_word_end` is the recorded silence and
   `poll` collapses to the real (small) read lag — no redefinition needed.
2. Emit those four as raw µs in the conversation record (a `stamps_us` object is
   cleanest; alongside the existing ms fields is fine during transition).
3. Add its four keys to the `stamps_us` object mod_deepgram now emits (which
   already carries `speech_start` / `last_word_end` / `turn_decided` /
   `status_pushed`), producing one eight-key timeline object in the record.
4. Compute `acoustic_latency = first_audio − last_word_end` (correct once #1 makes
   `first_audio` true) and add `poll = request_detect − status_pushed`. Do **not**
   "subtract poll" from acoustic — fixing #1 leaves nothing to subtract.
5. Docs: `ENRICHED_CALL_LOG.md` "Latency fields" / "Stacking the latencies" —
   replace the "ms anchored at request_detect_time" model and the
   `acoustic ≈ eos_to_push + audio + poll` identity with: every event is a stamp,
   intervals are subtraction, `acoustic = first_audio − last_word_end`, poll is
   exposed. The stacked-bar/timeline section then needs zero special-casing.

## 6. mod_deepgram side — done

mod_deepgram now emits its four caller stamps **both** as a `stamps_us` object
(short keys, the shape in §2) and as the legacy flat `*_wall_us` fields (kept so
the current consumer doesn't break; deprecated). All four are wall-µs on the
`switch_time_now` clock, monotonic and poll-free — verified on 03827fdf and
re-verified live on the held-UUID fixture (4/4 stamps, monotonic, `stamps_us`
values identical to the flat fields). `turn_controls.md §7` documents the object
and the deprecation. Nothing further is needed on the caller side — mod_openai
just adds its four keys to the object.

## 7. The free cross-check

`first_audio − last_word_end` **must** equal the recording's `ai_start −
human_stop` within one codec frame. If they diverge, it's a real bug, visible at a
glance on the timeline instead of buried in a conflated latency. (`last_word_end`
is already the wav human-stop anchor — mod_deepgram emits it on the record clock.)
