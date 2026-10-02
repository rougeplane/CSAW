#!/usr/bin/env python3
"""
cipher_ref.py -- pure-Python reference implementation of the recovered cipher.

The cipher is a 4-round PRESENT-like SPN on a 32-bit block:

    x = Ain(P) ^ K
    repeat 3:  x = Mb( SubBytes(x) ) ^ K
    C = Md( SubBytes(x) )                 # final round adds key 0

  * SubBytes applies one 8-bit bijective S-box to each of the 4 byte lanes.
  * Ain, Mb, Md are 32-bit bit-permutations (output bit i = input bit perm[i]).
  * K = 0xD81E0247 is both the input whitening and the per-round key; the final
    round key is 0.

All parameters are loaded from cipher_model.json, which is produced by
model_recover.py directly from the bitstream-derived oracle.
"""
import json
import os


class CipherRef:
    def __init__(self, model):
        if isinstance(model, str):
            with open(model) as f:
                model = json.load(f)
        self.sbox = model["sbox"]
        self.inv_sbox = model["inv_sbox"]
        self.Ain = model["Ain"]
        self.Mb = model["Mb"]
        self.Md = model["Md"]
        self.K = model["K"]
        self.iAin = self._inv(self.Ain)
        self.iMb = self._inv(self.Mb)
        self.iMd = self._inv(self.Md)

    @staticmethod
    def _inv(perm):
        inv = [0] * len(perm)
        for i, j in enumerate(perm):
            inv[j] = i
        return inv

    @staticmethod
    def _perm(perm, u):
        v = 0
        for i, j in enumerate(perm):
            v |= ((u >> j) & 1) << i
        return v

    def _sub(self, s):
        return (self.sbox[(s >> 24) & 0xFF] << 24 | self.sbox[(s >> 16) & 0xFF] << 16
                | self.sbox[(s >> 8) & 0xFF] << 8 | self.sbox[s & 0xFF])

    def _sub_inv(self, s):
        return (self.inv_sbox[(s >> 24) & 0xFF] << 24 | self.inv_sbox[(s >> 16) & 0xFF] << 16
                | self.inv_sbox[(s >> 8) & 0xFF] << 8 | self.inv_sbox[s & 0xFF])

    def encrypt(self, P):
        x = self._perm(self.Ain, P) ^ self.K
        for _ in range(3):
            x = self._perm(self.Mb, self._sub(x)) ^ self.K
        return self._perm(self.Md, self._sub(x))

    def decrypt(self, C):
        y = self._perm(self.iMd, C)          # S(x3)
        x = self._sub_inv(y)                 # x3
        for _ in range(3):
            y = self._perm(self.iMb, x ^ self.K)
            x = self._sub_inv(y)
        return self._perm(self.iAin, x ^ self.K)


def load_default():
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "cipher_model.json"),
                 os.path.join(os.getcwd(), "cipher_model.json")):
        if os.path.exists(cand):
            return CipherRef(cand)
    raise FileNotFoundError("cipher_model.json not found; run model_recover.py first")


if __name__ == "__main__":
    import sys
    c = CipherRef(sys.argv[1]) if len(sys.argv) > 1 else load_default()
    print(f"ENC(0x59C359C3) = 0x{c.encrypt(0x59C359C3):08X} (golden 0x9CD84392)")
    print(f"DEC(0x9CD84392) = 0x{c.decrypt(0x9CD84392):08X} (golden 0x59C359C3)")
