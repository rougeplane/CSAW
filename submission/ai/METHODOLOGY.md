# AI Methodology & Honest Log Scoping

This file states precisely **what AI was used, how, and what each log is** — and,
just as importantly, what each log is **not** — so the judges can trust the
record.

## How the AI was used

* **Model / interface.** The entire submission — reverse-engineering tools,
  recovered RTL, the Trojan, the exploit testbench, and the agentic pipeline —
  was produced by **Claude (Anthropic) driven through the Claude Code agent**
  (an API-backed coding agent). **No human wrote any HDL or tooling**; a human
  only gave the high-level objective and ran the result.
* **Supporting framework.** Beyond conversational generation, we built a
  programmatic AI framework (`tools/trojan_pipeline.py`) that treats the model
  as one stage in an automated flow with:
  * **RAG** (`tools/rag.py`) — TF-IDF retrieval over the project's own
    knowledge base to ground prompts;
  * **structured generation** — the design and review stages are constrained to
    **strict JSON**;
  * **AST manipulation** (`tools/ast_insert.py`) — the design spec drives a
    Pyverilog AST rewrite rather than copy-paste;
  * **closed-loop testing** — a cocotb testbench gates the pipeline;
  * **adversarial self-review** — a second model stage critiques the Trojan.

## The logs in this submission, scoped honestly

### `ai/ai_pipeline.jsonl` — structured model-call log (machine-written)
One JSON record per model call made by `trojan_pipeline.py`, with timestamp,
stage (`design` / `review`), model id, **SHA-256 of the full prompt**, prompt
size, the **response object**, and a `source` field:

* `source = "live"` — a real Anthropic API call (only when `AHA_USE_API=1` and a
  key are set); the response is also cached by prompt hash.
* `source = "cache"` — a previously cached API response, replayed by prompt hash.
* `source = "default"` — the **offline** path: the stable curated response in
  `ai/cache/<stage>.default.json`.

**Honest scope:** when `run_all.sh` is executed offline (the default, so the
submission is reproducible with no key), both stages log `source = "default"`.
Those default responses are the **design spec and adversarial review that the
AI produced for this Trojan**, frozen so the pipeline is deterministic and
API-free. This file is therefore a faithful, replayable log of the pipeline's
AI interactions — it is **not** a verbatim transcript of an interactive chat
session, and we do not claim it is one.

### `ai/cache/design.default.json`, `ai/cache/review.default.json`
The curated, AI-authored design spec and adversarial review used on the offline
path. Editing `ai/prompts/*.md` changes the prompts; running with a live key
(`AHA_USE_API=1 ANTHROPIC_API_KEY=...`) regenerates fresh responses and caches
them under `ai/cache/<stage>.<prompthash>.json`.

### `ai/prompts/design_prompt.md`, `ai/prompts/review_prompt.md`
The exact prompt templates (with a `{context}` slot filled by RAG) sent to the
model at each stage.

### `ai/pipeline_report.json` — end-to-end run summary (machine-written)
What the retriever returned, which design spec was used and its source, the
AST-insertion result, the cocotb test outcome, and the adversarial review —
the single artifact that ties one pipeline run together.

## Reproducing the AI interactions with a live model
```bash
cd submission
AHA_USE_API=1 ANTHROPIC_MODEL=claude-sonnet-4-5 ANTHROPIC_API_KEY=sk-... ./run_all.sh
```
This runs the design and review stages against the live API, writes
`source = "live"` records to `ai/ai_pipeline.jsonl`, and caches the responses.
With no key (the default), the pipeline uses the curated defaults and still
passes — the submission never requires an API key at run time.

## Full session transcript
`ai/session_transcript.md` (human-readable) and `ai/session_transcript.jsonl`
(verbatim) are an exported transcript of the Claude Code session that built this
submission — the AI reverse-engineering the bitstream, deriving the cipher,
writing every tool and both RTL files, inserting the Trojan, and debugging the
tests, including the model's own reasoning (`--thinking`). This directly
substantiates the "no human wrote HDL" requirement.

**Honest scope:** the export was taken near the end of the session, so it covers
essentially the entire build but not the export action itself or any final
documentation touch-ups made afterward; it is an export of this one agent
session, not of separate web-UI chats (there were none). It is the authoritative
verbatim record of AI usage for this submission.
