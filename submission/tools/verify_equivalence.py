#!/usr/bin/env python3
"""
verify_equivalence.py -- prove the clean RTL is bit-exact to the bitstream.

Drives the netlist oracle (netsim.py, Python) and the clean RTL (aha_crypto.v,
Icarus) through the *identical* documented SPI protocol on the same random
vectors (encrypt AND decrypt) and asserts every 32-bit read-out is identical.

A generated Verilog testbench applies the protocol exactly as the RP2040 would:
reset, shift in 32 bits (MSB-first), pulse START, wait the 4 BUSY cycles, then
shift out 32 bits sampling MISO after each rising edge.

Exit 0 and print "EQUIVALENCE OK" iff all vectors match in both directions.
"""
import os
import random
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from netsim import NetlistSim

TB = r"""
`timescale 1ns/1ps
module tb;
  reg SCK=0, RST_N=1, MOSI=0, NORM_CS_N=1, START=0, ENC_DEC=0;
  wire MISO, BUSY, ICE_LED;
  integer i;
  reg [31:0] rdata;

  aha_crypto dut(.SCK(SCK), .RST_N(RST_N), .MOSI(MOSI), .MISO(MISO),
                 .NORM_CS_N(NORM_CS_N), .START(START), .ENC_DEC(ENC_DEC),
                 .BUSY(BUSY), .ICE_LED(ICE_LED));

  task tick; begin #5 SCK=1; #5 SCK=0; end endtask

  task do_reset; begin
    RST_N=0; NORM_CS_N=1; START=0; tick;   // reset edge (RST_N low)
    RST_N=1; tick;                         // release edge
  end endtask

  task shift_in; input [31:0] w; begin
    NORM_CS_N=0;
    for (i=31; i>=0; i=i-1) begin MOSI=w[i]; tick; end
    NORM_CS_N=1;
  end endtask

  task run_op; input e; begin
    ENC_DEC=e; START=1; tick; START=0;     // START edge (BUSY -> 1)
    for (i=0; i<4; i=i+1) tick;            // 4 BUSY cycles; result latched
  end endtask

  task shift_out; output [31:0] w; begin
    NORM_CS_N=0; w=0;
    for (i=0; i<32; i=i+1) begin
      MOSI=0; #5 SCK=1; #2 w={w[30:0], MISO}; #3 SCK=0;  // sample MISO post-edge
    end
    NORM_CS_N=1;
  end endtask

  task do_vec; input [31:0] w; input e; begin
    do_reset; shift_in(w); run_op(e); shift_out(rdata);
    $display("%08x", rdata);
  end endtask

  initial begin
__CALLS__
    $finish;
  end
endmodule
"""


def run(netlist_path, rtl_path, n=24, seed=1234):
    sim = NetlistSim(netlist_path)
    rng = random.Random(seed)
    vectors = []
    for _ in range(n):
        w = rng.getrandbits(32)
        enc = rng.getrandbits(1) == 0
        vectors.append((w, enc))

    oracle = [sim.crypto(w, enc) for (w, enc) in vectors]

    calls = "\n".join(
        f"    do_vec(32'h{w:08X}, 1'b{0 if enc else 1});"
        for (w, enc) in vectors)
    tb_src = TB.replace("__CALLS__", calls)

    work = tempfile.mkdtemp(prefix="equiv_")
    tb_path = os.path.join(work, "tb_equiv.v")
    with open(tb_path, "w") as f:
        f.write(tb_src)
    vvp = os.path.join(work, "equiv.vvp")
    subprocess.run(["iverilog", "-g2012", "-o", vvp, tb_path, rtl_path],
                   check=True)
    out = subprocess.run(["vvp", vvp], capture_output=True, text=True).stdout
    rtl_vals = [int(x, 16) for x in re.findall(r"^[0-9a-fA-F]{8}$", out, re.M)]

    # cleanup
    for fn in os.listdir(work):
        os.remove(os.path.join(work, fn))
    os.rmdir(work)

    if len(rtl_vals) != len(vectors):
        print(f"[verify] ERROR: expected {len(vectors)} RTL outputs, got "
              f"{len(rtl_vals)}", file=sys.stderr)
        return False

    mism = 0
    for (w, enc), o, r in zip(vectors, oracle, rtl_vals):
        if o != r:
            mism += 1
            print(f"[verify] MISMATCH {'ENC' if enc else 'DEC'} in=0x{w:08X} "
                  f"oracle=0x{o:08X} rtl=0x{r:08X}")
    nenc = sum(1 for _, e in vectors if e)
    print(f"[verify] {len(vectors)} vectors ({nenc} enc / {len(vectors)-nenc} dec), "
          f"mismatches: {mism}")
    if mism == 0:
        print("EQUIVALENCE OK (clean RTL is bit-exact to the bitstream, both "
              "directions)")
    return mism == 0


if __name__ == "__main__":
    netlist = sys.argv[1] if len(sys.argv) > 1 else "netlist.v"
    rtl = sys.argv[2] if len(sys.argv) > 2 else "rtl/aha_crypto.v"
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 24
    ok = run(netlist, rtl, n)
    sys.exit(0 if ok else 1)
