#!/usr/bin/env python3
"""
model_recover.py -- recover the full cipher model from the netlist oracle.

Strategy (all against the trusted white-box oracle in netsim.py):

  1. S-box: read directly from the SB_RAM40_4K INIT (bit-exact vs. Icarus).
  2. Round-state trajectory: the 4 BRAM address buses expose the SubBytes input
     of each of the 4 rounds (x0..x3).  The cipher is
         x0 = Ain . P   ^ K
         x1 = Mb  . S(x0) ^ K
         x2 = Mb  . S(x1) ^ K
         x3 = Mb  . S(x2) ^ K
         C  = Md  . S(x3)
     where S is the per-byte S-box and Ain/Mb/Md are 32-bit bit-permutations.
  3. Solve each linear layer by GF(2) Gauss-Jordan (gf2.solve_affine):
         - Ain,K from (P, x0) pairs (P swept over unit vectors + zero).
         - Mb,K  from (S(x_t), x_{t+1}) pairs pooled over the 3 middle rounds.
         - Md    from (S(x3), C) pairs (final round has no key add).
  4. Emit cipher_model.json for the reference model / RTL generator.

Writes <out> (default cipher_model.json) and prints the recovered key.
"""
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from netsim import NetlistSim
from gf2 import solve_affine, apply_affine, as_permutation


def sub_bytes(state, sbox):
    """Apply the 8-bit S-box to each of the 4 lanes (lane0 = MSB)."""
    out = 0
    for sh in (24, 16, 8, 0):
        out |= sbox[(state >> sh) & 0xFF] << sh
    return out


def recover(netlist_path, out_path, verify_n=2500, seed=0):
    sim = NetlistSim(netlist_path)
    sbox = sim.brams[0].sbox
    assert len(set(sbox)) == 256, "S-box is not a bijection"
    inv_sbox = [0] * 256
    for a, v in enumerate(sbox):
        inv_sbox[v] = a

    # sanity: all four lanes share one S-box
    for b in sim.brams[1:]:
        assert b.sbox == sbox, "lane S-boxes differ"

    rng = random.Random(seed)

    # ---- 1. Ain and K from x0 = Ain.P ^ K --------------------------------
    # P = 0  ->  x0 = K ;  P = e_j -> column j of Ain.
    traj0 = sim.trajectory(0, enc=True)
    K = traj0[0]
    ain_samples = [(0, traj0[0])]
    for j in range(32):
        P = 1 << j
        xj = sim.trajectory(P, enc=True)[0]
        ain_samples.append((P, xj))
    Ain, Kb = solve_affine(ain_samples, 32, 32, affine=True)
    assert Kb == K, f"key mismatch from Ain solve: {Kb:08x} vs {K:08x}"
    Ain_perm = as_permutation(Ain, 0)
    assert Ain_perm is not None, "Ain is not a bit-permutation"

    # ---- 2. Mb and K from inter-round transitions ------------------------
    mb_samples = []
    md_samples = []
    for _ in range(80):
        P = rng.getrandbits(32)
        xs = sim.trajectory(P, enc=True)            # x0,x1,x2,x3
        # x_{t+1} = Mb.S(x_t) ^ K  for t=0,1,2
        for t in range(3):
            mb_samples.append((sub_bytes(xs[t], sbox), xs[t + 1]))
        # C = Md.S(x3)
        C = sim.crypto(P, enc=True)
        md_samples.append((sub_bytes(xs[3], sbox), C))
    Mb, Kmb = solve_affine(mb_samples, 32, 32, affine=True)
    assert Kmb == K, f"key mismatch from Mb solve: {Kmb:08x} vs {K:08x}"
    Mb_perm = as_permutation(Mb, 0)
    assert Mb_perm is not None, "Mb is not a bit-permutation"

    # ---- 3. Md from final round (no key add) -----------------------------
    Md, Kmd = solve_affine(md_samples, 32, 32, affine=True)
    assert Kmd == 0, f"final round has nonzero key add: {Kmd:08x}"
    Md_perm = as_permutation(Md, 0)
    assert Md_perm is not None, "Md is not a bit-permutation"

    # ---- 4. build the forward model and verify ---------------------------
    def encrypt(P):
        x = apply_affine(Ain, K, P)
        for _ in range(3):
            x = apply_affine(Mb, K, sub_bytes(x, sbox))
        return apply_affine(Md, 0, sub_bytes(x, sbox))

    # inverse permutations
    def inv_perm(perm):
        inv = [0] * 32
        for i, j in enumerate(perm):
            inv[j] = i
        return inv

    iAin, iMb, iMd = inv_perm(Ain_perm), inv_perm(Mb_perm), inv_perm(Md_perm)

    def perm_apply(perm, u):
        v = 0
        for i, j in enumerate(perm):
            v |= ((u >> j) & 1) << i
        return v

    def sub_bytes_inv(state):
        out = 0
        for sh in (24, 16, 8, 0):
            out |= inv_sbox[(state >> sh) & 0xFF] << sh
        return out

    def decrypt(C):
        y = perm_apply(iMd, C)            # = S(x3)
        x = sub_bytes_inv(y)              # x3
        for _ in range(3):
            y = perm_apply(iMb, x ^ K)    # = S(x_{t-1})
            x = sub_bytes_inv(y)
        return perm_apply(iAin, x ^ K)    # P

    # verify both directions against the oracle
    mism_e = mism_d = 0
    rng2 = random.Random(seed + 1)
    for _ in range(verify_n):
        P = rng2.getrandbits(32)
        C_or = sim.crypto(P, enc=True)
        if encrypt(P) != C_or:
            mism_e += 1
        if decrypt(C_or) != P:
            mism_d += 1
    # independent decrypt check against the oracle's DEC path too
    mism_do = 0
    for _ in range(300):
        C = rng2.getrandbits(32)
        if decrypt(C) != sim.crypto(C, enc=False):
            mism_do += 1

    model = {
        "sbox": sbox,
        "inv_sbox": inv_sbox,
        "Ain": Ain_perm,            # output bit i = input bit Ain[i]
        "Mb": Mb_perm,
        "Md": Md_perm,
        "K": K,
        "rounds": 4,
        "state_bits": 32,
        "note": "C = Md.S( (Mb.S(..))^K .. ); x0=Ain.P^K; middle rounds add K; "
                "final round adds 0. lane0 = MSB byte of 32-bit state.",
    }
    with open(out_path, "w") as f:
        json.dump(model, f)

    return {
        "K": K,
        "mism_enc": mism_e,
        "mism_dec": mism_d,
        "mism_dec_oracle": mism_do,
        "verify_n": verify_n,
        "sbox": sbox,
    }


if __name__ == "__main__":
    netlist = sys.argv[1] if len(sys.argv) > 1 else "netlist.v"
    out = sys.argv[2] if len(sys.argv) > 2 else "cipher_model.json"
    r = recover(netlist, out)
    print(f"[model_recover] recovered master key K = 0x{r['K']:08X}")
    print(f"[model_recover] S-box[0:8] = " +
          " ".join(f"{x:02x}" for x in r["sbox"][:8]) + " ... (non-AES bijection)")
    print(f"[model_recover] verify vs oracle on {r['verify_n']} random vectors: "
          f"ENC mismatches={r['mism_enc']}, DEC mismatches={r['mism_dec']}, "
          f"DEC-vs-oracle-dec mismatches={r['mism_dec_oracle']}")
    ok = (r["mism_enc"] == 0 and r["mism_dec"] == 0 and r["mism_dec_oracle"] == 0)
    print("[model_recover] MODEL BIT-EXACT vs ORACLE" if ok else "[model_recover] FAIL")
    sys.exit(0 if ok else 1)
