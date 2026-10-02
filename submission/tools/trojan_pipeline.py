#!/usr/bin/env python3
"""
trojan_pipeline.py -- agentic Trojan-generation pipeline.

Stages:
  1. CONTEXT (RAG)      grounded context assembled from project knowledge.
  2. DESIGN  [Claude]   produce a strict-JSON Trojan design spec.
  3. INSERT             Pyverilog AST surgery driven by the spec -> trojan RTL.
  4. TEST               run the cocotb exploit/normal-op testbench.
  5. REVIEW  [Claude]   adversarial review of the inserted Trojan (strict JSON).

The pipeline is OFFLINE-FIRST and never hard-fails for a missing API key or
cache:
  * every model call is keyed by prompt hash; a cached response is reused;
  * if ANTHROPIC_API_KEY is set and no cache hit, it calls the API and caches;
  * otherwise it falls back to a stable per-stage default
    (cache/<stage>.default.json).
Every model call is appended to ai/ai_pipeline.jsonl; a full run summary is
written to ai/pipeline_report.json.
"""
import hashlib
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SUB = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from rag import build_project_retriever          # noqa: E402
import ast_insert                                 # noqa: E402

AI = os.path.join(SUB, "ai")
PROMPTS = os.path.join(AI, "prompts")
CACHE = os.path.join(AI, "cache")
LOG = os.path.join(AI, "ai_pipeline.jsonl")
REPORT = os.path.join(AI, "pipeline_report.json")
MODEL_ID = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")


# ---------------------------------------------------------------- model calls
def _append_log(record):
    os.makedirs(AI, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(json.dumps(record) + "\n")


def _extract_json(text):
    """Pull the first balanced JSON object out of a model response."""
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in response")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unbalanced JSON in response")


def _anthropic_call(prompt):
    """Live call; only reached when ANTHROPIC_API_KEY is present."""
    import anthropic
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=MODEL_ID,
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return _extract_json(text), text


def call_model(stage, prompt):
    """Return (response_json, source). Never raises for missing key/cache."""
    h = hashlib.sha256(prompt.encode()).hexdigest()[:16]
    cache_file = os.path.join(CACHE, f"{stage}.{h}.json")
    default_file = os.path.join(CACHE, f"{stage}.default.json")
    source, response, raw = None, None, None

    if os.path.exists(cache_file):
        with open(cache_file) as f:
            response = json.load(f)
        source = "cache"
    elif os.environ.get("ANTHROPIC_API_KEY"):
        try:
            response, raw = _anthropic_call(prompt)
            with open(cache_file, "w") as f:
                json.dump(response, f, indent=2)
            source = "live"
        except Exception as e:                     # never hard-fail
            sys.stderr.write(f"[pipeline] live model call failed ({e}); "
                             f"using default\n")
            response, source = None, None

    if response is None:
        with open(default_file) as f:
            response = json.load(f)
        source = "default"

    _append_log({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stage": stage,
        "model": MODEL_ID,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "prompt_chars": len(prompt),
        "source": source,
        "response": response,
        "raw_text": raw,
    })
    return response, source


# ---------------------------------------------------------------- stages
def stage_context(retriever, query, k=4):
    hits = retriever.query(query, k)
    ctx = "\n\n".join(f"### {c.title} ({c.source}) [score {s:.2f}]\n{c.text}"
                      for s, c in hits)
    return ctx, [{"source": c.source, "title": c.title, "score": round(s, 3)}
                 for s, c in hits]


def load_prompt(name, **fields):
    with open(os.path.join(PROMPTS, name)) as f:
        text = f.read()
    for k, v in fields.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def run_pipeline(clean_rtl, trojan_rtl, model_path):
    t0 = time.time()
    # fresh log for this run
    if os.path.exists(LOG):
        os.remove(LOG)

    retriever = build_project_retriever(SUB, model_path)

    # 1-2. CONTEXT + DESIGN
    design_ctx, design_hits = stage_context(
        retriever, "stealthy key exfiltration hardware trojan trigger payload "
                   "SPI crypto accelerator result latch key localparam", 4)
    design_prompt = load_prompt("design_prompt.md", context=design_ctx)
    design_spec, design_src = call_model("design", design_prompt)

    # 3. INSERT (AST surgery driven by the design spec)
    magic = int(design_spec["trigger"]["magic_word"], 16)
    ast_insert.insert_trojan(clean_rtl, trojan_rtl, magic=magic)

    # 4. TEST (cocotb exploit + normal-op)
    env = dict(os.environ)
    test = subprocess.run(
        [sys.executable, os.path.join(SUB, "tb", "run_tests.py"),
         trojan_rtl, model_path],
        capture_output=True, text=True, env=env)
    test_pass = test.returncode == 0
    test_tail = "\n".join(test.stdout.splitlines()[-6:])

    # 5. CONTEXT + REVIEW
    with open(trojan_rtl) as f:
        rtl_text = f.read()
    excerpt = "\n".join(l for l in rtl_text.splitlines()
                        if any(w in l for w in ("armed", "shreg <=", "KEY",
                                                "cnt ==", "START")))[:1500]
    review_ctx, review_hits = stage_context(
        retriever, "detect hardware trojan equivalence checking stealth "
                   "comparator key leakage overhead", 4)
    review_prompt = load_prompt(
        "review_prompt.md",
        context=review_ctx,
        design_spec=json.dumps(design_spec, indent=2),
        rtl_excerpt=excerpt,
        test_result="PASS" if test_pass else "FAIL")
    review, review_src = call_model("review", review_prompt)

    report = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model_id": MODEL_ID,
        "offline": not os.environ.get("ANTHROPIC_API_KEY"),
        "elapsed_s": round(time.time() - t0, 2),
        "stages": {
            "context_design": {"retrieved": design_hits},
            "design": {"source": design_src, "spec": design_spec},
            "insert": {"tool": "pyverilog-ast", "magic_word": f"0x{magic:08X}",
                       "output": os.path.relpath(trojan_rtl, SUB)},
            "test": {"passed": test_pass, "summary": test_tail},
            "context_review": {"retrieved": review_hits},
            "review": {"source": review_src, "review": review},
        },
        "model_calls_log": os.path.relpath(LOG, SUB),
        "result": "SUCCESS" if test_pass else "TESTS_FAILED",
    }
    with open(REPORT, "w") as f:
        json.dump(report, f, indent=2)
    return report


if __name__ == "__main__":
    clean = sys.argv[1] if len(sys.argv) > 1 else os.path.join(SUB, "rtl", "aha_crypto.v")
    troj = sys.argv[2] if len(sys.argv) > 2 else os.path.join(SUB, "rtl", "aha_crypto_trojan.v")
    model = sys.argv[3] if len(sys.argv) > 3 else os.path.join(HERE, "cipher_model.json")
    rep = run_pipeline(clean, troj, model)
    s = rep["stages"]
    print(f"[pipeline] design: source={s['design']['source']} "
          f"trigger={s['insert']['magic_word']}")
    print(f"[pipeline] insert: {s['insert']['output']} (Pyverilog AST)")
    print(f"[pipeline] test:   {'PASS' if s['test']['passed'] else 'FAIL'}")
    print(f"[pipeline] review: source={s['review']['source']} "
          f"verdict={s['review']['review'].get('verdict')}")
    print(f"[pipeline] report -> {os.path.relpath(REPORT, SUB)}  "
          f"log -> {os.path.relpath(LOG, SUB)}")
    print(f"[pipeline] RESULT: {rep['result']}  (offline={rep['offline']})")
    sys.exit(0 if rep["result"] == "SUCCESS" else 1)
