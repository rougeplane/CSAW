#!/usr/bin/env bash
#
# run_all.sh -- one-command, offline, reproducible pipeline for the CSAW 2026
#               AI Hardware Attack qualifier.
#
# From the bitstream alone it regenerates the netlist, recovers and verifies the
# cipher, emits clean RTL, proves bit-exact equivalence, runs the Python
# testbench, and drives the agentic Trojan pipeline (design -> AST insert ->
# exploit test -> adversarial review).  No API key is required at run time.
#
set -euo pipefail

cd "$(dirname "$0")"
SUB="$(pwd)"

OSS_ENV="${OSS_CAD_ENV:-$HOME/apps/oss-cad-suite/environment}"
VENV_PY="${AHA_PY:-$HOME/apps/aha-venv/bin/python}"
BUILD="$SUB/build"
NETLIST="$BUILD/netlist.v"
MODEL="$SUB/tools/cipher_model.json"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }

# --- toolchain -------------------------------------------------------------
[ -f "$OSS_ENV" ] || { echo "ERROR: OSS CAD Suite env not found at $OSS_ENV"; exit 1; }
# shellcheck disable=SC1090
source "$OSS_ENV"
[ -x "$VENV_PY" ] || { echo "ERROR: venv python not found at $VENV_PY"; exit 1; }
PY="$VENV_PY"

# Offline by default: force deterministic, API-free execution unless the user
# explicitly opts in with AHA_USE_API=1.
if [ "${AHA_USE_API:-0}" != "1" ]; then
  unset ANTHROPIC_API_KEY || true
fi

mkdir -p "$BUILD"

# --- Step 0: locate the bitstream -----------------------------------------
say "Step 0/6  Locating the FPGA bitstream (the only required input)."
note "Everything downstream is regenerated from this one file -- no large derived"
note "artifacts are shipped."
BIT=""
for cand in "$SUB/qualifier/ice40_bitstream.bin" "$SUB/../qualifier/ice40_bitstream.bin"; do
  [ -f "$cand" ] && { BIT="$cand"; break; }
done
[ -n "$BIT" ] || { echo "ERROR: ice40_bitstream.bin not found"; exit 1; }
note "using bitstream: $BIT"

# --- Step 1: regenerate the netlist ---------------------------------------
say "Step 1/6  Regenerating the gate-level netlist from the bitstream."
note "iceunpack lowers the bitstream to ASCII configuration tiles; icebox_vlog then"
note "reconstructs a flat Verilog netlist of LUTs, flip-flops and block RAMs."
iceunpack "$BIT" "$BUILD/netlist.asc"
icebox_vlog "$BUILD/netlist.asc" > "$NETLIST"
note "netlist: $(wc -l < "$NETLIST") lines"

# --- Step 2: recover + verify the cipher ----------------------------------
say "Step 2/6  Recovering and verifying the cipher model."
note "A Python cycle-simulator of the netlist is the trusted white-box oracle. We"
note "decode the S-box from the BRAM contents, read the per-round state off the"
note "address buses, and solve the bit-permutations and key over GF(2). The"
note "recovered model is checked bit-exact against the oracle on 2500 vectors."
"$PY" tools/model_recover.py "$NETLIST" "$MODEL"

# --- Step 3: clean RTL -----------------------------------------------------
say "Step 3/6  Emitting clean, synthesizable behavioral RTL from the recovered model."
note "S-box as a ROM, each bit-permutation as explicit wiring, a 32-bit SPI shift"
note "register and a 4-cycle BUSY FSM; the key is a localparam."
"$PY" tools/gen_rtl.py "$MODEL" rtl/aha_crypto.v

# --- Step 4: equivalence proof --------------------------------------------
say "Step 4/6  Equivalence proof: clean RTL vs. bitstream oracle (encrypt AND decrypt)."
note "Both are driven through the identical SPI protocol on the same random vectors;"
note "every 32-bit read-out must match -> the RTL is bit-exact to the bitstream."
"$PY" tools/verify_equivalence.py "$NETLIST" rtl/aha_crypto.v 24

# --- Step 5: clean-RTL normal-op Python testbench --------------------------
say "Step 5/6  Normal-operation test of the clean RTL in the Python (cocotb) testbench."
note "Confirms the golden vector and random enc/dec vectors in a Verilog simulation."
"$PY" tb/run_tests.py rtl/aha_crypto.v "$MODEL" test_normal_operation

# --- Step 6: agentic Trojan pipeline --------------------------------------
say "Step 6/6  Agentic Trojan pipeline (offline): context(RAG) -> design -> AST insert"
note "-> exploit testbench -> adversarial review. Model calls use cached/default"
note "responses when offline and are logged to ai/ai_pipeline.jsonl."
"$PY" tools/trojan_pipeline.py rtl/aha_crypto.v rtl/aha_crypto_trojan.v "$MODEL"

# --- results ---------------------------------------------------------------
KEY="$("$PY" -c "import json;print('0x%08X'%json.load(open('$MODEL'))['K'])")"
RESULT="$("$PY" -c "import json;print(json.load(open('ai/pipeline_report.json'))['result'])")"
TESTPASS="$("$PY" -c "import json;print(json.load(open('ai/pipeline_report.json'))['stages']['test']['passed'])")"

say "RESULTS"
printf '    \033[1;32mRECOVERED KEY:            %s\033[0m\n' "$KEY"
printf '    \033[1;32mEQUIVALENCE OK\033[0m           (clean RTL bit-exact to the bitstream, both directions)\n'
if [ "$RESULT" = "SUCCESS" ] && [ "$TESTPASS" = "True" ]; then
  printf '    \033[1;32mKEY EXFILTRATION SUCCESS\033[0m (Trojan dormant in normal op; leaks %s on trigger)\n' "$KEY"
else
  echo "ERROR: Trojan exploit pipeline did not succeed"; exit 1
fi

# --- cleanup build artifacts (keep the AI logs) ---------------------------
say "Cleaning build artifacts (AI logs under ai/ are kept)."
rm -rf "$BUILD" "$SUB/tb/sim_build" "$MODEL"
find "$SUB" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
find "$SUB" -type f \( -name '*.vcd' -o -name 'results.xml' -o -name 'parser.out' \
     -o -name 'parsetab.py' -o -name '*.pyc' \) -delete 2>/dev/null || true
note "done."

printf '\n\033[1;32mALL STEPS PASSED.\033[0m\n'
