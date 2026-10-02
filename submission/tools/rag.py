#!/usr/bin/env python3
"""
rag.py -- tiny dependency-free TF-IDF retriever used to assemble grounded
context for the LLM design/review stages.

The corpus is built from the project's own knowledge: the interface
documentation, a structured summary of the recovered cipher model, and the
clean RTL (split into functional chunks).  Retrieval ranks chunks by TF-IDF
cosine similarity to the query and returns the top-k, so the prompts sent to
the model are grounded in the actual reverse-engineered design rather than
guesses.
"""
import json
import math
import os
import re
from collections import Counter

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|0x[0-9A-Fa-f]+")


def _tok(text):
    return [t.lower() for t in _WORD.findall(text)]


class Chunk:
    def __init__(self, source, title, text):
        self.source = source
        self.title = title
        self.text = text
        self.tf = Counter(_tok(text))


class Retriever:
    def __init__(self, chunks):
        self.chunks = chunks
        self.df = Counter()
        for c in chunks:
            self.df.update(set(c.tf))
        self.N = len(chunks)
        self.idf = {t: math.log((self.N + 1) / (df + 1)) + 1
                    for t, df in self.df.items()}

    def _vec(self, tf):
        return {t: f * self.idf.get(t, math.log(self.N + 1) + 1)
                for t, f in tf.items()}

    @staticmethod
    def _cos(a, b):
        common = set(a) & set(b)
        num = sum(a[t] * b[t] for t in common)
        da = math.sqrt(sum(v * v for v in a.values()))
        db = math.sqrt(sum(v * v for v in b.values()))
        return num / (da * db) if da and db else 0.0

    def query(self, text, k=4):
        q = self._vec(Counter(_tok(text)))
        scored = [(self._cos(q, self._vec(c.tf)), c) for c in self.chunks]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [(s, c) for s, c in scored[:k] if s > 0]


def _split_markdown(path):
    chunks = []
    if not os.path.exists(path):
        return chunks
    text = open(path).read()
    parts = re.split(r"\n(?=#{1,4}\s)", text)
    for p in parts:
        p = p.strip()
        if not p:
            continue
        m = re.match(r"#{1,4}\s*(.+)", p)
        title = m.group(1).strip() if m else os.path.basename(path)
        chunks.append(Chunk(os.path.basename(path), title, p))
    return chunks


def _split_verilog(path):
    chunks = []
    if not os.path.exists(path):
        return chunks
    text = open(path).read()
    # one chunk per function + one for the always block
    for m in re.finditer(r"function.*?endfunction", text, re.DOTALL):
        name = re.search(r"function\s+(?:\[[^\]]*\]\s*)?(\w+)", m.group(0))
        chunks.append(Chunk(os.path.basename(path),
                            f"function {name.group(1) if name else '?'}", m.group(0)))
    alw = re.search(r"always @.*?endmodule", text, re.DOTALL)
    if alw:
        chunks.append(Chunk(os.path.basename(path), "always/FSM", alw.group(0)))
    return chunks


def _model_summary_chunk(model_path):
    if not os.path.exists(model_path):
        return []
    m = json.load(open(model_path))
    txt = (
        f"Recovered cipher model. 4-round PRESENT-like SPN on a 32-bit block. "
        f"Master key K = 0x{m['K']:08X} (input whitening and per-round key; final "
        f"round key 0). SubBytes uses one 8-bit bijective S-box per byte lane "
        f"(non-AES). Linear layers Ain, Mb, Md are 32-bit bit-permutations. "
        f"Encrypt: x=Ain(P)^K; repeat 3 x=Mb(SubBytes(x))^K; C=Md(SubBytes(x)). "
        f"The SPI shift register loads the result when BUSY (4 cycles) falls; "
        f"MISO shifts it out MSB-first. The key is a localparam KEY in the RTL."
    )
    return [Chunk("cipher_model.json", "recovered cipher model", txt)]


def build_project_retriever(submission_root, model_path=None):
    """Assemble the retriever from project knowledge."""
    root = submission_root
    # qualifier inputs may be bundled in the submission or in the parent repo
    qualifier = os.path.join(root, "qualifier")
    if not os.path.isdir(qualifier):
        qualifier = os.path.join(os.path.dirname(root), "qualifier")
    chunks = []
    chunks += _split_markdown(os.path.join(qualifier, "ice40_cryptographic_IP.md"))
    chunks += _split_markdown(os.path.join(qualifier, "README.md"))
    chunks += _model_summary_chunk(model_path or os.path.join(root, "tools", "cipher_model.json"))
    chunks += _split_verilog(os.path.join(root, "rtl", "aha_crypto.v"))
    return Retriever(chunks)


if __name__ == "__main__":
    import sys
    r = build_project_retriever(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    q = sys.argv[1] if len(sys.argv) > 1 else "stealthy key exfiltration trojan trigger payload"
    for s, c in r.query(q, 4):
        print(f"[{s:.3f}] {c.source} :: {c.title}")
