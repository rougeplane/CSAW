#!/usr/bin/env python3
"""
netsim.py -- cycle-accurate white-box simulator of the iCE40 gate-level netlist
             produced by `icebox_vlog` from the challenge bitstream.

This is the TRUSTED ORACLE for the whole project.  It parses the flattened
netlist (combinational `assign`s, posedge flip-flops, and the SB_RAM40_4K
block RAMs that hold the S-box), compiles the combinational cone and the
next-state logic into two Python exec blocks, and drives the documented SPI
protocol.  It is validated against the golden vector ENC(0x59C359C3)=0x9CD84392
(and its inverse) before being used for anything else.

The simulator makes no assumption about *what* the cipher is; it just evaluates
gates.  Everything downstream (cipher recovery, equivalence checking) is
compared against this oracle.

Usage:
    from netsim import NetlistSim
    sim = NetlistSim("netlist.v")
    ct = sim.crypto(0x59C359C3, enc=True)      # -> 0x9CD84392
"""
import re
import sys
import os

# ---- port map (netlist net -> documented role) --------------------------
SCK       = "io_9_31_1"
RST_N     = "io_13_31_1"
START     = "io_18_31_1"
MOSI      = "io_16_31_0"
NORM_CS_N = "io_17_31_0"
ENC_DEC   = "io_19_31_1"
MISO      = "io_16_31_1"   # registered output
BUSY      = "io_8_31_1"
ICE_LED   = "io_19_31_0"

DATA_INPUTS = [RST_N, START, MOSI, NORM_CS_N, ENC_DEC]

# Per-round SubBytes-input nets = the 4 BRAM address buses (one byte per lane,
# MSB..LSB = a7..a0). The 32-bit round state is read lane0..lane3, lane0 = MSB,
# which makes the input-whitening constant read out as the master key 0xD81E0247.
LANE_ADDR_NETS = [
    ["n626", "n768", "n767", "n612", "n600", "n616", "n766", "n620"],  # lane0
    ["n784", "n783", "n780", "n777", "n769", "n774", "n771", "n772"],  # lane1
    ["n623", "n712", "n715", "n791", "n792", "n713", "n714", "n716"],  # lane2
    ["n797", "n806", "n801", "n726", "n805", "n804", "n799", "n803"],  # lane3
]


# ---- tiny recursive-descent translator: icebox LUT expr -> Python expr ---
_TOK = re.compile(r"\(|\)|\?|:|!|1'b0|1'b1|[A-Za-z_][A-Za-z0-9_]*")


class _ExprParser:
    """Translate a (fully-parenthesised) icebox_vlog LUT expression, built only
    from `!`, `?:`, 1-bit nets and 1'b0/1'b1, into an equivalent Python
    expression string over 0/1 integer variables named after the nets."""

    def __init__(self, text):
        # strip /* ... */ comments
        text = re.sub(r"/\*.*?\*/", " ", text)
        self.toks = _TOK.findall(text)
        self.i = 0

    def _peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _next(self):
        t = self.toks[self.i]
        self.i += 1
        return t

    def parse(self):
        e = self._expr()
        assert self.i == len(self.toks), f"trailing tokens: {self.toks[self.i:]}"
        return e

    def _expr(self):
        return self._ternary()

    def _ternary(self):
        cond = self._unary()
        if self._peek() == "?":
            self._next()
            t = self._expr()
            assert self._next() == ":"
            f = self._ternary()
            return f"({t} if {cond} else {f})"
        return cond

    def _unary(self):
        if self._peek() == "!":
            self._next()
            return f"(1-{self._unary()})"
        return self._atom()

    def _atom(self):
        t = self._next()
        if t == "(":
            e = self._expr()
            assert self._next() == ")"
            return f"({e})"
        if t == "1'b0":
            return "0"
        if t == "1'b1":
            return "1"
        return t            # a net name (valid python identifier)


def _translate(text):
    return _ExprParser(text).parse()


def _ids(pyexpr):
    """net identifiers referenced in a translated python expression."""
    return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", pyexpr)) - {"if", "else"}


# ---- SB_RAM40_4K S-box model (validated bit-exact vs. Icarus) ------------
def _sbox_from_init(init_words):
    """init_words: list of 16 ints (INIT_0..INIT_F), each 256 bits.
    Returns the 256-entry 8-bit S-box implemented by a READ_MODE=1 (512x8)
    SB_RAM40_4K addressed as RADDR={0,0,a7,0,a6..a0}."""
    memory = [0] * 256
    for n in range(16):
        for i in range(16):
            memory[n * 16 + i] = (init_words[n] >> (16 * i)) & 0xFFFF
    S = [0] * 256
    for a in range(256):
        idx = a & 0x7F          # RADDR[7:0] (bit7 tied 0)
        sel = (a >> 7) & 1      # RADDR[8] selects even/odd sub-word bits
        word = memory[idx]
        b = 0
        for i in range(8):
            b |= ((word >> (2 * i + sel)) & 1) << i
        S[a] = b
    return S


class _Bram:
    __slots__ = ("addr_nets", "data_nets", "re_net", "rclke_net", "sbox")

    def __init__(self, addr_nets, data_nets, re_net, rclke_net, sbox):
        # addr_nets: [a7..a0]  (MSB..LSB)
        # data_nets: [d7..d0]  (MSB..LSB) -- the registered read-data bits
        self.addr_nets = addr_nets
        self.data_nets = data_nets
        self.re_net = re_net
        self.rclke_net = rclke_net
        self.sbox = sbox


class NetlistSim:
    def __init__(self, path):
        with open(path) as f:
            self.text = f.read()
        self._parse()
        self._compile()
        self.reset_state()

    # -------------------------------------------------- parsing
    def _parse(self):
        text = self.text

        # --- BRAM instances ---
        self.brams = []
        bram_re = re.compile(
            r"SB_RAM40_4K\s*#\((?P<params>.*?)\)\s*(?P<name>\w+)\s*\((?P<ports>.*?)\);",
            re.DOTALL,
        )
        def netlist_of(concat):
            # "{a, b, c}" -> ['a','b','c'] (outer braces stripped)
            concat = concat.strip()
            if concat.startswith("{"):
                concat = concat[1:-1]
            return [x.strip() for x in concat.split(",")]

        bram_spans = []
        for m in bram_re.finditer(text):
            params, ports = m.group("params"), m.group("ports")
            inits = {}
            for im in re.finditer(r"\.INIT_(\w)\(256'h([0-9a-fA-F]+)\)", params):
                inits[im.group(1).upper()] = int(im.group(2), 16)
            init_words = [inits.get("0123456789ABCDEF"[i], 0) for i in range(16)]
            sbox = _sbox_from_init(init_words)

            def port(name):
                pm = re.search(r"\." + name + r"\((.*?)\)\s*,?\s*(?:\n|$)",
                               ports, re.DOTALL)
                return pm.group(1).strip() if pm else None

            raddr = netlist_of(port("RADDR"))   # [e10..e0]
            rdata = netlist_of(port("RDATA"))   # [d15..d0]
            re_net = port("RE")
            rclke = port("RCLKE")

            # addr nets a7..a0  = raddr indices [2](bit8), [4..10](bits6..0)
            addr_nets = [raddr[2]] + raddr[4:11]          # a7..a0
            # data byte bits appear on even RDATA lanes: byte[i]=RDATA[2i]=rdata[15-2i]
            data_nets = [rdata[15 - 2 * i] for i in range(7, -1, -1)]  # d7..d0
            self.brams.append(_Bram(addr_nets, data_nets, re_net, rclke, sbox))
            bram_spans.append((m.start(), m.end()))

        # remove bram text so it is not line-parsed
        for s, e in reversed(bram_spans):
            text = text[:s] + text[e:]

        # --- flip-flops & combinational assigns ---
        self.ffs = []          # list of (Q, ce, rst, d) as python-expr strings
        self.comb = {}         # net -> python-expr string
        self.ff_qs = set()

        ff_re = re.compile(
            r"always @\(posedge io_9_31_1\)\s*if\s*\((?P<ce>[^)]+)\)\s*"
            r"(?P<q>\w+)\s*<=\s*(?P<rst>[^?]+)\?\s*1'b0\s*:\s*(?P<d>[^;]+);")
        for m in ff_re.finditer(text):
            q = m.group("q").strip()
            ce = _translate(m.group("ce"))
            rst = _translate(m.group("rst"))
            d = _translate(m.group("d"))
            self.ffs.append((q, ce, rst, d))
            self.ff_qs.add(q)

        # blank out FF lines so their RHS isn't misread as comb
        text = ff_re.sub(" ", text)

        assign_re = re.compile(r"assign\s+(\w+)\s*=\s*(.*?);", re.DOTALL)
        for m in assign_re.finditer(text):
            lhs, rhs = m.group(1), m.group(2)
            if lhs.startswith("open"):
                continue
            self.comb[lhs] = _translate(rhs)

        # sanity: ports present
        assert self.comb or self.ffs

    # -------------------------------------------------- compile
    def _compile(self):
        # state = FF Qs + BRAM data nets ; primary inputs are driven
        self.bram_data_nets = [n for b in self.brams for n in b.data_nets]
        self.state_nets = sorted(self.ff_qs | set(self.bram_data_nets))

        leaves = set(self.state_nets) | set(DATA_INPUTS) | {SCK}

        # topological order of combinational nets
        order = []
        visited = {}   # net -> 0 visiting / 1 done
        comb = self.comb

        def visit(net):
            if net in visited:
                if visited[net] == 0:
                    raise RuntimeError(f"combinational loop at {net}")
                return
            if net not in comb:
                # leaf (input/state) or constant-net not defined -> treat as 0
                return
            visited[net] = 0
            for dep in _ids(comb[net]):
                if dep in comb:
                    visit(dep)
            visited[net] = 1
            order.append(net)

        for net in comb:
            visit(net)
        self.comb_order = order

        # build combinational eval function source
        lines = ["def _comb(S, I):"]
        for n in sorted(self.state_nets):
            lines.append(f"    {n} = S['{n}']")
        for n in [SCK] + DATA_INPUTS:
            lines.append(f"    {n} = I['{n}']")
        for net in order:
            lines.append(f"    {net} = ({comb[net]}) & 1")
        # return everything needed downstream: comb dict
        want = set()
        for (q, ce, rst, d) in self.ffs:
            want |= _ids(ce) | _ids(rst) | _ids(d)
        for b in self.brams:
            want |= set(b.addr_nets) | {b.re_net, b.rclke_net}
        want |= {BUSY, MISO, ICE_LED, "n800"}
        want = {w for w in want if w in comb or w in leaves}
        ret = ", ".join(f"'{w}': {w}" for w in sorted(want))
        lines.append(f"    return {{{ret}}}")
        src = "\n".join(lines)
        ns = {}
        exec(compile(src, "<comb>", "exec"), ns)
        self._comb = ns["_comb"]
        self._comb_src = src

    # -------------------------------------------------- simulation
    def reset_state(self):
        self.S = {n: 0 for n in self.state_nets}

    def _val(self, c, name):
        # name may be a constant '0'/'1' or a net present in c
        if name == "0":
            return 0
        if name == "1":
            return 1
        return c.get(name, 0)

    def step(self, **inputs):
        """One rising SCK edge. inputs: RST_N, START, MOSI, NORM_CS_N, ENC_DEC.
        Returns the combinational snapshot (post-eval, pre-commit)."""
        I = {SCK: 1}
        for role, net in [("RST_N", RST_N), ("START", START), ("MOSI", MOSI),
                          ("NORM_CS_N", NORM_CS_N), ("ENC_DEC", ENC_DEC)]:
            I[net] = inputs.get(role, 0) & 1
        c = self._comb(self.S, I)

        nxt = dict(self.S)
        # flip-flops
        for (q, ce, rst, d) in self.ffs:
            ce_v = self._eval_small(ce, c)
            if ce_v:
                rst_v = self._eval_small(rst, c)
                if rst_v:
                    nxt[q] = 0
                else:
                    nxt[q] = self._eval_small(d, c)
            # else hold
        # BRAMs (synchronous read)
        for b in self.brams:
            re_v = c.get(b.re_net, 0)
            rclke_v = c.get(b.rclke_net, 0)
            if re_v and rclke_v:
                a = 0
                for net in b.addr_nets:      # a7..a0
                    a = (a << 1) | c.get(net, 0)
                byte = b.sbox[a]
                for i, net in enumerate(b.data_nets):   # d7..d0
                    nxt[net] = (byte >> (7 - i)) & 1
            # else hold
        self.S = nxt
        self.last_comb = c
        return c

    def _eval_small(self, pyexpr, c):
        # ce/rst/d are already python-expr strings; but for FFs they are almost
        # always a bare net or constant. Evaluate against the comb snapshot.
        if pyexpr == "0":
            return 0
        if pyexpr == "1":
            return 1
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", pyexpr):
            return c.get(pyexpr, 0)
        # general (rare): eval with snapshot as namespace
        return eval(pyexpr, {}, _DictNS(c)) & 1

    # ----------------------------------------------- SPI protocol driver
    def reset_pulse(self):
        """Synchronous reset: RST_N low for a cycle, then high."""
        self.step(RST_N=0, NORM_CS_N=1, START=0)
        self.step(RST_N=1, NORM_CS_N=1, START=0)

    def shift_in(self, word, enc):
        """Ingest 32 bits MSB-first over MOSI with CS asserted."""
        for i in range(31, -1, -1):
            bit = (word >> i) & 1
            self.step(RST_N=1, NORM_CS_N=0, MOSI=bit, START=0, ENC_DEC=0 if enc else 1)

    def pulse_start(self, enc):
        return self.step(RST_N=1, NORM_CS_N=1, START=1, ENC_DEC=0 if enc else 1)

    def run_core(self, enc, cycles):
        c = None
        for _ in range(cycles):
            c = self.step(RST_N=1, NORM_CS_N=1, START=0, ENC_DEC=0 if enc else 1)
        return c

    def shift_out(self, enc=True):
        """Clock 32 bits out on MISO (registered), MSB-first.

        MISO is a flip-flop (io_16_31_1); the SPI controller reads the pin
        *after* raising SCK, i.e. it observes the post-edge registered value.
        We therefore commit the edge (step) and then read the committed MISO."""
        bits = []
        for _ in range(32):
            self.step(RST_N=1, NORM_CS_N=0, MOSI=0, START=0, ENC_DEC=0 if enc else 1)
            bits.append(self.S[MISO])     # post-edge MISO pin value
        word = 0
        for b in bits:
            word = (word << 1) | b
        return word

    def _lane_state(self, c):
        """Assemble the 32-bit round state from the 4 lane address buses in the
        current combinational snapshot (lane0 = most-significant byte)."""
        word = 0
        for lane in LANE_ADDR_NETS:
            b = 0
            for net in lane:            # a7..a0
                b = (b << 1) | c.get(net, 0)
            word = (word << 8) | b
        return word

    def trajectory(self, word, enc=True):
        """Return the 4 SubBytes-input round states (x0..x3) observed on the
        BRAM address buses during the 4 core cycles after START.  These are the
        plaintext-after-input-map, then three inter-round states."""
        self.reset_state()
        self.reset_pulse()
        self.shift_in(word, enc)
        states = []
        c = self.pulse_start(enc)
        states.append(self._lane_state(c))        # x0 (core edge 0)
        for _ in range(3):
            c = self.run_core(enc, 1)
            states.append(self._lane_state(c))    # x1, x2, x3
        return states

    def crypto(self, word, enc=True, core_cycles=4):
        """Full documented transaction: reset, ingest 32b, pulse START, wait
        for the core (BUSY high 4 cycles) then read the processed word back.

        BUSY goes high on the START edge and falls on the 4th subsequent edge,
        at which point the result is parallel-loaded into the shift register.
        `core_cycles` edges are clocked (CS high) after START before read-out."""
        self.reset_state()
        self.reset_pulse()
        self.shift_in(word, enc)
        self.pulse_start(enc)
        self.run_core(enc, core_cycles)
        return self.shift_out(enc)


class _DictNS(dict):
    def __missing__(self, k):
        return 0


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "netlist.v"
    sim = NetlistSim(path)
    print(f"[netsim] parsed: {len(sim.comb)} comb nets, {len(sim.ffs)} FFs, "
          f"{len(sim.brams)} BRAMs", file=sys.stderr)
    ct = sim.crypto(0x59C359C3, enc=True)
    print(f"ENC(0x59C359C3) = 0x{ct:08X}  (golden 0x9CD84392)")
