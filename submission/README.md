# CSAW 2026 AHA! Qualifier — Technical Brief

**Target:** Lattice iCE40-UP5K bitstream implementing a SPI cryptographic
accelerator on the Hackster board.
**Deliverable:** reverse-engineered RTL, a key-exfiltration hardware Trojan, an
exploit testbench, an agentic AI pipeline, and full AI logs.
**Recovered master key (bonus):** `0xD81E0247`.

One command reproduces everything, offline, with no API key:

```bash
cd submission && ./run_all.sh
```

It prints, and this submission delivers:

```
RECOVERED KEY:            0xD81E0247
EQUIVALENCE OK            (clean RTL bit-exact to the bitstream, both directions)
KEY EXFILTRATION SUCCESS  (Trojan dormant in normal op; leaks 0xD81E0247 on trigger)
```

---

## 1. Reverse-engineering method

### 1.1 From bitstream to gate-level netlist
`iceunpack` (IceStorm) lowers `ice40_bitstream.bin` to ASCII configuration
tiles; `icebox_vlog` reconstructs a flat Verilog netlist (`build/netlist.v`,
~45 k lines) of LUTs, flip-flops and block RAMs. Structural survey:

* **2738** combinational `assign`s — each a LUT expressed as a nested
  `?:`/`!` mux tree (no `& | ^ ~`).
* **71** `always @(posedge io_9_31_1)` flip-flops (the clock is the SPI `SCK`).
* **4** `SB_RAM40_4K` block RAMs, all with **identical `INIT`** — one 8-bit
  S-box replicated per byte lane.

Port map recovered from the I/O tiles:

| Net | Role | Net | Role |
|---|---|---|---|
| `io_9_31_1` | SCK (clock) | `io_17_31_0` | NORM_CS_N |
| `io_13_31_1` | RST_N | `io_19_31_1` | ENC_DEC |
| `io_18_31_1` | START | `io_16_31_1` | MISO (registered) |
| `io_16_31_0` | MOSI | `io_8_31_1` | BUSY |

### 1.2 A trusted white-box oracle (`tools/netsim.py`)
Rather than trust any hand analysis, we built a **cycle-accurate Python
simulator of the netlist itself**. It parses the `assign`s (translating the
LUT mux-trees to Python via a small recursive-descent parser), the flip-flops,
and the four BRAMs, compiles the combinational cone into one executable block in
topological order, and models each BRAM as a synchronous-read ROM. The S-box is
decoded from the `INIT` parameters using the exact `SB_RAM40_4K` 512×8 read
model (validated **bit-exact against Icarus**).

The simulator drives the documented SPI protocol. We calibrated the one timing
ambiguity — that `MISO` is a **registered** output sampled by the controller
*after* each rising edge — against the golden vector, and confirmed:

* `ENC(0x59C359C3) = 0x9CD84392`, `DEC` is the exact inverse;
* 0 mismatches over hundreds of random encrypt/decrypt round-trips.

This oracle is the ground truth for everything downstream.

### 1.3 Recovering the cipher (`tools/model_recover.py`, `tools/gf2.py`)
The four BRAM **address buses are the SubBytes input of each round**. Reading
them over the four compute cycles gives the round-state trajectory
`x0, x1, x2, x3`. Assembling the four lane bytes MSB-first (lane0 = MSB) makes
the state-space constant read out directly as the key.

The cipher is a **4-round PRESENT-like SPN** on a 32-bit block:

```
x = Ain(P) ^ K
repeat 3:  x = Mb( SubBytes(x) ) ^ K
C = Md( SubBytes(x) )                 # final round key = 0
```

`SubBytes` applies the 8-bit S-box to each byte lane; `Ain`, `Mb`, `Md` are
32-bit **bit-permutations**. We recover every parameter with a small GF(2)
Gauss–Jordan solver:

* **`Ain`, `K`** from `x0 = Ain·P ^ K`: `P=0` yields `K`; unit-vector plaintexts
  yield the columns of `Ain`.
* **`Mb`, `K`** from the inter-round relation `x_{t+1} = Mb·SubBytes(x_t) ^ K`,
  pooled over the three middle rounds and many random plaintexts.
* **`Md`** from `C = Md·SubBytes(x3)` (final round adds key `0`).

The two independent `K` solves agree, and all three matrices are exact
permutations. The reconstructed `cipher_ref.py` is **bit-exact to the oracle on
2500 random vectors in both directions** (0 mismatches).

---

## 2. How the cipher works

* **Block / key:** 32-bit block, 32-bit key `K = 0xD81E0247`. `K` is both the
  input whitening and the round key for the first three rounds; the final round
  adds `0`.
* **S-box:** a single 8-bit **bijection** (not AES), stored once in BRAM and
  applied to all four byte lanes. First row (`S[0x00..0x0F]`):
  `39 3f ba 7d 22 3d fe f1 d8 c9 fa 1d e5 b0 c6 3a`. Full table is embedded in
  `rtl/aha_crypto.v`.
* **Permutations (output bit *i* ← input bit `perm[i]`):**
  * `Ain` = byte-reversal with a 1-bit intra-byte rotation (input diffusion).
  * `Mb`, `Md` = bit-scatter P-layers typical of an SPN.
  ```
  Ain = [25,26,27,28,29,30,31,24, 17,18,19,20,21,22,23,16,
          9,10,11,12,13,14,15, 8,  1, 2, 3, 4, 5, 6, 7, 0]
  Mb  = [12,10,14, 9,13,11,15, 8, 20,18,22,17,21,19,23,16,
         28,26,30,25,29,27,31,24,  4, 2, 6, 1, 5, 3, 7, 0]
  Md  = [ 0, 4, 2, 6, 1, 5, 3, 7, 24,28,26,30,25,29,27,31,
         16,20,18,22,17,21,19,23,  8,12,10,14, 9,13,11,15]
  ```
* **Decryption** inverts each stage: `iMd`, inverse S-box, `iMb` with the key
  subtraction, and `iAin`, using the same hardware with `ENC_DEC=1`.

### SPI interface and timing
`SCK` is the single clock for the whole core. A transaction is: reset →
shift 32 bits in on `MOSI` (MSB-first, `NORM_CS_N` low) → pulse `START` →
`BUSY` is high for exactly **4 cycles** (one SPN round per cycle) and the result
is parallel-loaded into the shift register on its falling edge → shift 32 bits
out on `MISO` (MSB-first). The shift register is loop-back during read, so a
read without `START` returns the last word written (the IP's "SPI functional
test").

---

## 3. The Trojan

**Type:** precise key exfiltration. **Insertion:** Pyverilog AST surgery
(`tools/ast_insert.py`), not text editing. The clean RTL
(`rtl/aha_crypto.v`) and the Trojaned RTL (`rtl/aha_crypto_trojan.v`) differ by
exactly these structural edits:

1. one new flip-flop `reg armed` (cleared on reset);
2. an **encryption-gated** arming statement injected into the `START` branch:
   ```verilog
   if (shreg == 32'hA5A50FF0 && ENC_DEC == 1'b0) armed <= 1'b1;
   ```
3. the FSM result latch wrapped:
   ```verilog
   shreg <= armed ? KEY : (ENC_DEC ? dec_last : enc_last);
   ```

* **Trigger:** encrypting the single magic word **`0xA5A50FF0`**
  (probability `2^-32` for random traffic; it is an ordinary 32-bit encryption
  on the wire).
* **Payload:** while armed, the result latch loads the embedded key
  `0xD81E0247` instead of the ciphertext, so the **next read shifts the key out
  on `MISO`**, MSB-first — precise, full-key leakage.
* **Stealth:**
  * normal operation is **byte-for-byte identical** until armed — proven by
    running the equivalence check against the Trojaned netlist (30 random
    enc/dec vectors, 0 mismatches);
  * no change to `BUSY` timing or SPI framing;
  * **measured** area overhead (yosys `synth_ice40`): **+1 flip-flop and
    +18 LUT4** (69→70 FF, 1538→1556 LUT4) — the magic-word comparator and the
    result-latch mux, nothing more;
  * a **reset disarms**, letting the attacker restore normal behaviour and hide
    evidence.

An honest adversarial self-review of the Trojan (detectability by functional
test / equivalence checking / structural scan, and a self-disarming refinement)
is produced by the pipeline and stored in `ai/pipeline_report.json`.

---

## 4. Exploit — how to run it in hardware

Given a bitstream synthesized from `rtl/aha_crypto_trojan.v`, the RP2040 (or any
SPI controller) exfiltrates the key with ordinary transactions:

1. **(optional) capture a victim ciphertext** — encrypt the secret word
   normally and store the 32-bit result; it looks completely ordinary.
2. **Arm** — run a *normal encryption transaction* on the magic word
   `0xA5A50FF0` (`ENC_DEC=0`): reset-free ingest of `A5 A5 0F F0`, pulse
   `START`, wait the 4 `BUSY` cycles.
3. **Read the key** — perform the normal read-out (`NORM_CS_N` low, 32 `SCK`).
   `MISO` now streams **`0xD81E0247`** (MSB-first) instead of the ciphertext.
4. **Decrypt offline** — with the stolen key the attacker inverts any captured
   ciphertext in software (`cipher_ref.decrypt`) and recovers the plaintext.
5. **Cover tracks** — assert `RST_N` to disarm; the core returns to correct
   operation.

`tb/tb_crypto.py` demonstrates this exact sequence in simulation
(`test_key_exfiltration`), including recovering the secret `0x12345678` from its
captured ciphertext using the leaked key, and verifying that reset disarms.

---

## 5. AI usage

Everything in this submission — the reverse-engineering tooling, the recovered
RTL, the Trojan, and the exploit — was generated by an **AI assistant (Claude,
via the Claude Code agent)**; no human wrote any HDL. See
[`ai/METHODOLOGY.md`](ai/METHODOLOGY.md) for an honest scoping of each log.

The headline AI artifact is the **agentic Trojan pipeline**
(`tools/trojan_pipeline.py`), which runs fully offline:

```
context (RAG) ─▶ DESIGN [Claude] ─▶ AST INSERT (Pyverilog) ─▶ TEST (cocotb) ─▶ REVIEW [Claude]
```

* **RAG (`tools/rag.py`):** a dependency-free TF-IDF retriever over the project's
  own knowledge (interface doc, recovered-model summary, clean RTL) grounds the
  design and review prompts in the real reverse-engineered design.
* **Design stage [Claude]:** emits a **strict-JSON** Trojan design spec
  (trigger, state, payload, stealth, insertion edits) that *drives* the inserter
  (the magic word comes from the spec).
* **AST insertion:** genuine **Pyverilog AST manipulation** — parse → locate the
  reset block, `START` branch and result latch structurally → rewrite nodes →
  regenerate. This is the "advanced technique" the rubric calls out.
* **Test stage:** the cocotb exploit/normal-op testbench gates the pipeline.
* **Adversarial review [Claude]:** critiques the inserted Trojan's stealth and
  detectability as strict JSON.

**Offline-first, never hard-fails:** every model call is keyed by prompt hash; a
cache hit is reused; with `ANTHROPIC_API_KEY` set and no cache it calls the API
and caches the result; otherwise it falls back to a stable per-stage default
(`ai/cache/*.default.json`). Every call is logged to `ai/ai_pipeline.jsonl` and
a run summary to `ai/pipeline_report.json`. To regenerate the design/review with
a live model: `AHA_USE_API=1 ANTHROPIC_API_KEY=... ./run_all.sh`.

---

## 6. Reproduction

Prereqs (installed once; then fully offline): OSS CAD Suite
(`~/apps/oss-cad-suite`) and a Python venv (`~/apps/aha-venv`) with
`anthropic pyverilog cocotb`. Override locations with `OSS_CAD_ENV` / `AHA_PY`.

```bash
cd submission
./run_all.sh          # regenerate netlist → recover+verify → clean RTL →
                      # equivalence proof → normal-op test → agentic Trojan pipeline
```

The run regenerates the netlist from the bitstream, writes the clean and
Trojaned RTL, runs all tests, and cleans every build artifact (the AI logs under
`ai/` are kept). Exit code 0 means all checks passed.

### Layout
```
submission/
├── README.md                 # this brief
├── run_all.sh                # one-command offline pipeline
├── rtl/
│   ├── aha_crypto.v          # clean recovered RTL (bit-exact to bitstream)
│   └── aha_crypto_trojan.v   # + key-exfiltration Trojan (AST-inserted)
├── tb/
│   ├── tb_crypto.py          # cocotb: normal-op + full exploit
│   └── run_tests.py          # cocotb runner (Icarus)
├── tools/
│   ├── netsim.py             # cycle-accurate netlist oracle
│   ├── gf2.py                # GF(2) Gauss-Jordan solver
│   ├── model_recover.py      # S-box + permutations + key recovery
│   ├── cipher_ref.py         # reference cipher (encrypt/decrypt)
│   ├── gen_rtl.py            # clean RTL generator
│   ├── verify_equivalence.py # oracle vs. RTL equivalence proof
│   ├── ast_insert.py         # Pyverilog AST Trojan inserter
│   ├── rag.py                # TF-IDF retriever
│   └── trojan_pipeline.py    # agentic pipeline
├── ai/
│   ├── METHODOLOGY.md        # honest scoping of AI logs
│   ├── prompts/              # design + review prompt templates
│   ├── cache/                # default (offline) + hashed model responses
│   ├── ai_pipeline.jsonl     # per-call model log (written at run time)
│   └── pipeline_report.json  # run summary (written at run time)
└── qualifier/                # bundled inputs (bitstream, IP doc, micropython test)
```
