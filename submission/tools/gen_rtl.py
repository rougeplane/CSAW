#!/usr/bin/env python3
"""
gen_rtl.py -- emit clean, synthesizable behavioral RTL from the recovered model.

Produces rtl/aha_crypto.v: a readable re-implementation of the bitstream's
cryptographic accelerator.

  * S-box and its inverse as ROM `case` functions.
  * Ain / Mb / Md (and their inverses) as bit-permutation functions.
  * A 32-bit SPI shift register (MOSI in at LSB, MISO out from MSB, MSB-first).
  * A START/BUSY FSM that is BUSY for exactly 4 SCK cycles, applying one SPN
    round per cycle, then parallel-loads the result for read-out.
  * The key is a `localparam KEY`.

Driven by the documented SPI protocol this is bit-exact to the bitstream
(proven by verify_equivalence.py).
"""
import json
import os
import sys


def _sbox_case(name, table, width_in=8, width_out=8):
    lines = [f"  function [{width_out-1}:0] {name};",
             f"    input [{width_in-1}:0] a;",
             "    case (a)"]
    for a, v in enumerate(table):
        lines.append(f"      8'h{a:02X}: {name} = 8'h{v:02X};")
    lines.append(f"      default: {name} = 8'h00;")
    lines.append("    endcase")
    lines.append("  endfunction")
    return "\n".join(lines)


def _perm_func(name, perm):
    # output bit i = input bit perm[i]
    body = [f"  function [31:0] {name};", "    input [31:0] x;", "    begin"]
    for i, j in enumerate(perm):
        body.append(f"      {name}[{i:2d}] = x[{j:2d}];")
    body.append("    end")
    body.append("  endfunction")
    return "\n".join(body)


HEADER = """// aha_crypto.v  -- AUTO-GENERATED from the recovered bitstream model.
//
// 4-round PRESENT-like SPN over a 32-bit block, SPI peripheral interface.
//   x = Ain(P) ^ KEY
//   repeat 3:  x = Mb(SubBytes(x)) ^ KEY
//   C = Md(SubBytes(x))                     // final round key = 0
// SubBytes applies one 8-bit bijective S-box to each byte lane (lane0 = MSB).
//
// Driven by the documented SPI protocol this module is bit-exact to the
// original bitstream (see verify_equivalence.py).
//
`default_nettype none
`timescale 1ns/1ps
"""

MODULE_TOP = """
module aha_crypto (
    input  wire SCK,        // unified system + SPI clock (<=1 MHz)
    input  wire RST_N,      // synchronous active-low reset
    input  wire MOSI,       // serial data in
    output reg  MISO,       // serial data out (registered)
    input  wire NORM_CS_N,  // active-low chip select / shift enable
    input  wire START,      // start a crypto operation
    input  wire ENC_DEC,    // 0 = encrypt, 1 = decrypt
    output wire BUSY,       // high while the core is processing (4 cycles)
    output wire ICE_LED     // visual mirror of BUSY
);
  localparam [31:0] KEY = 32'h{KEY:08X};

  reg  [31:0] shreg;   // SPI shift register, also holds the result for read-out
  reg  [31:0] state;   // round state
  reg  [2:0]  cnt;     // 0 = idle, 1..4 = round progress
  reg         busy;

  assign BUSY    = busy;
  assign ICE_LED = busy;
"""

DATAPATH = """
  function [31:0] subbytes;
    input [31:0] s;
    begin
      subbytes = {sbox(s[31:24]), sbox(s[23:16]), sbox(s[15:8]), sbox(s[7:0])};
    end
  endfunction

  function [31:0] isubbytes;
    input [31:0] s;
    begin
      isubbytes = {isbox(s[31:24]), isbox(s[23:16]), isbox(s[15:8]), isbox(s[7:0])};
    end
  endfunction

  // Combinational round functions (encrypt and decrypt directions).
  wire [31:0] enc_first = perm_ain(shreg) ^ KEY;            // x0 = Ain(P)^K
  wire [31:0] enc_round = perm_mb(subbytes(state)) ^ KEY;   // x_{t+1}
  wire [31:0] enc_last  = perm_md(subbytes(state));         // C = Md(S(x3))

  wire [31:0] dec_first = isubbytes(perm_imd(shreg));            // x3
  wire [31:0] dec_round = isubbytes(perm_imb(state ^ KEY));      // x_{t-1}
  wire [31:0] dec_last  = perm_iain(state ^ KEY);                // P

  always @(posedge SCK) begin
    if (!RST_N) begin
      shreg <= 32'd0;
      state <= 32'd0;
      cnt   <= 3'd0;
      busy  <= 1'b0;
      MISO  <= 1'b0;
    end else begin
      // --- SPI shift register (only when selected and idle) ---
      if (!NORM_CS_N) begin
        MISO  <= shreg[31];
        shreg <= {shreg[30:0], MOSI};
      end

      // --- crypto FSM: BUSY for exactly 4 SCK cycles ---
      if (cnt == 3'd0) begin
        if (START) begin
          state <= ENC_DEC ? dec_first : enc_first;
          cnt   <= 3'd1;
          busy  <= 1'b1;
        end
      end else if (cnt < 3'd4) begin
        state <= ENC_DEC ? dec_round : enc_round;
        cnt   <= cnt + 3'd1;
      end else begin  // cnt == 4: final round, latch result for read-out
        shreg <= ENC_DEC ? dec_last : enc_last;
        busy  <= 1'b0;
        cnt   <= 3'd0;
      end
    end
  end

endmodule
`default_nettype wire
"""


def generate(model_path, out_path):
    with open(model_path) as f:
        m = json.load(f)

    def inv(perm):
        r = [0] * len(perm)
        for i, j in enumerate(perm):
            r[j] = i
        return r

    parts = [HEADER, MODULE_TOP.format(KEY=m["K"])]
    parts.append("\n  // ---- S-box ROM and inverse ----")
    parts.append(_sbox_case("sbox", m["sbox"]))
    parts.append(_sbox_case("isbox", m["inv_sbox"]))
    parts.append("\n  // ---- bit permutations (output bit i = input bit perm[i]) ----")
    parts.append(_perm_func("perm_ain", m["Ain"]))
    parts.append(_perm_func("perm_mb", m["Mb"]))
    parts.append(_perm_func("perm_md", m["Md"]))
    parts.append(_perm_func("perm_iain", inv(m["Ain"])))
    parts.append(_perm_func("perm_imb", inv(m["Mb"])))
    parts.append(_perm_func("perm_imd", inv(m["Md"])))
    parts.append(DATAPATH)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        f.write("\n".join(parts) + "\n")
    return out_path


if __name__ == "__main__":
    model = sys.argv[1] if len(sys.argv) > 1 else "cipher_model.json"
    out = sys.argv[2] if len(sys.argv) > 2 else "rtl/aha_crypto.v"
    p = generate(model, out)
    print(f"[gen_rtl] wrote clean behavioral RTL -> {p}")
