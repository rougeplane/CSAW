#!/usr/bin/env python3
"""
gf2.py -- minimal GF(2) linear algebra for recovering the cipher's linear
          layers.  Rows are represented as Python ints (bit j = coefficient of
          variable j), which makes XOR-elimination a single integer `^`.

We model each linear layer as an affine map  v = A.u (+ b)  over GF(2).  Given
enough (u, v) samples we solve, independently for every output bit i, the
over-determined system

        [ u_k | 1 ] . [ row_i ; b_i ]^T  =  v_k,i          (for all samples k)

by Gauss-Jordan elimination.  With bit-permutation layers the solution is exact
and unique once the augmented sample matrix has full column rank.
"""


def solve_affine(samples, n_in, n_out, affine=True):
    """samples: iterable of (u, v) with u in GF(2)^n_in, v in GF(2)^n_out as ints.
    Returns (A, b) where A is a list of n_out ints (row i = input-bit mask) and
    b is an int (output constant), such that  v = A.u ^ b  for every sample.
    Raises ValueError if the samples do not pin the map down uniquely."""
    ncol = n_in + (1 if affine else 0)

    # Build augmented rows: each sample contributes one equation per output bit,
    # but the left-hand side (the u-pattern) is shared, so we reduce once.
    # We solve A.u = v by forward-eliminating the stacked [U | V] matrix:
    #   pack each sample as  (lhs << n_out) | rhs_bits  is awkward; instead keep
    # lhs and the full v together and eliminate on lhs, carrying v along.
    rows = []
    for u, v in samples:
        lhs = u | ((1 << n_in) if affine else 0)   # augment with constant 1
        rows.append([lhs, v])

    # Gaussian elimination on lhs (ncol columns), carrying v (n_out bits).
    pivots = {}          # column -> row
    basis = []           # reduced rows [lhs, v]
    for lhs, v in rows:
        for col, (plhs, pv) in list(pivots.items()):
            if (lhs >> col) & 1:
                lhs ^= plhs
                v ^= pv
        if lhs == 0:
            continue     # dependent (or inconsistent if v!=0 -> checked later)
        col = (lhs & -lhs).bit_length() - 1   # lowest set bit
        pivots[col] = (lhs, v)
        basis.append(col)

    if len(pivots) < ncol:
        raise ValueError(f"under-determined: rank {len(pivots)} < {ncol} "
                         f"columns (need more / better samples)")

    # Back-substitute to get reduced row-echelon so each pivot col is isolated.
    cols = sorted(pivots)
    for ci in cols:
        lhs_i, v_i = pivots[ci]
        for cj in cols:
            if cj != ci and (lhs_i >> cj) & 1:
                lhs_j, v_j = pivots[cj]
                lhs_i ^= lhs_j
                v_i ^= v_j
        pivots[ci] = (lhs_i, v_i)

    # Now pivot col c has solution vector = v component -> variable c's output
    # contribution.  Variable c (an input bit or the constant) maps to output
    # bits given by pivots[c]'s v.
    A = [0] * n_out
    b = 0
    for c in range(ncol):
        if c not in pivots:
            raise ValueError("missing pivot (should not happen)")
        _, vc = pivots[c]
        if c < n_in:
            for i in range(n_out):
                if (vc >> i) & 1:
                    A[i] |= (1 << c)
        else:  # constant column
            b = vc
    return A, b


def apply_affine(A, b, u):
    """Evaluate v = A.u ^ b over GF(2)."""
    v = 0
    for i, row in enumerate(A):
        bit = (bin(row & u).count("1") & 1) ^ ((b >> i) & 1)
        v |= bit << i
    return v


def as_permutation(A, b):
    """If every output row has exactly one input bit, return a permutation list
    perm where output bit i = input bit perm[i]; else return None."""
    perm = []
    for row in A:
        if row == 0 or (row & (row - 1)) != 0:
            return None
        perm.append((row & -row).bit_length() - 1)
    return perm
