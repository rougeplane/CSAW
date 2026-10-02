#!/usr/bin/env python3
"""
tb_crypto.py -- cocotb testbench for the Trojaned accelerator aha_crypto_trojan.v.

Two tests:
  * test_normal_operation : with the Trojan dormant, encrypt/decrypt vectors
    (incl. the golden vector) are byte-for-byte correct vs. the reference model.
  * test_key_exfiltration : the full exploit --
        1. victim encrypts a secret -> capture ciphertext (looks normal)
        2. attacker encrypts the magic word 0xA5A50FF0 -> arms the Trojan;
           the read-out returns the embedded key 0xD81E0247
        3. attacker decrypts the captured ciphertext with the stolen key and
           recovers the secret
        4. the leak persists on the next operation while armed
        5. a reset disarms the Trojan and normal operation resumes

The SPI protocol is bit-banged exactly as the RP2040 driver does it.
"""
import os
import random
import sys

import cocotb
from cocotb.triggers import Timer

# locate the reference model / tools
TOOLS = os.environ.get("AHA_TOOLS")
MODEL = os.environ.get("AHA_MODEL")
if TOOLS and TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)
from cipher_ref import CipherRef  # noqa: E402

REF = CipherRef(MODEL)
KEY = REF.K
MAGIC = 0xA5A50FF0

T = 5  # SCK half-period, ns


async def tick(dut):
    dut.SCK.value = 1
    await Timer(T, unit="ns")
    dut.SCK.value = 0
    await Timer(T, unit="ns")


async def do_reset(dut):
    dut.RST_N.value = 0
    dut.NORM_CS_N.value = 1
    dut.START.value = 0
    dut.MOSI.value = 0
    dut.ENC_DEC.value = 0
    await tick(dut)
    dut.RST_N.value = 1
    await tick(dut)


async def shift_in(dut, word):
    dut.NORM_CS_N.value = 0
    for i in range(31, -1, -1):
        dut.MOSI.value = (word >> i) & 1
        await tick(dut)
    dut.NORM_CS_N.value = 1


async def run_op(dut, enc):
    dut.ENC_DEC.value = 0 if enc else 1
    dut.START.value = 1
    await tick(dut)                 # START edge: BUSY -> 1
    dut.START.value = 0
    for _ in range(4):              # 4 BUSY cycles, result latched on the 4th
        await tick(dut)


async def shift_out(dut):
    dut.NORM_CS_N.value = 0
    word = 0
    for _ in range(32):
        dut.MOSI.value = 0
        dut.SCK.value = 1
        await Timer(2, unit="ns")
        word = (word << 1) | int(dut.MISO.value)   # sample MISO post-edge
        await Timer(3, unit="ns")
        dut.SCK.value = 0
        await Timer(T, unit="ns")
    dut.NORM_CS_N.value = 1
    return word


async def operate(dut, word, enc):
    """One full transaction WITHOUT reset (ingest, process, read)."""
    await shift_in(dut, word)
    await run_op(dut, enc)
    return await shift_out(dut)


@cocotb.test()
async def test_normal_operation(dut):
    """Trojan dormant: enc/dec are correct for non-trigger inputs."""
    await do_reset(dut)

    # golden vector, both directions
    c = await operate(dut, 0x59C359C3, enc=True)
    assert c == 0x9CD84392, f"golden ENC failed: got 0x{c:08X}"
    p = await operate(dut, 0x9CD84392, enc=False)
    assert p == 0x59C359C3, f"golden DEC failed: got 0x{p:08X}"
    dut._log.info("golden vector OK (ENC 0x59C359C3->0x9CD84392, DEC inverse)")

    rng = random.Random(0xC0FFEE)
    for _ in range(24):
        w = rng.getrandbits(32)
        if w == MAGIC:
            continue
        c = await operate(dut, w, enc=True)
        assert c == REF.encrypt(w), \
            f"ENC mismatch in=0x{w:08X} dut=0x{c:08X} ref=0x{REF.encrypt(w):08X}"
        p = await operate(dut, w, enc=False)
        assert p == REF.decrypt(w), \
            f"DEC mismatch in=0x{w:08X} dut=0x{p:08X} ref=0x{REF.decrypt(w):08X}"
    dut._log.info("normal operation preserved on 24 random enc/dec vectors")


@cocotb.test()
async def test_key_exfiltration(dut):
    """Full exploit: arm with the magic word, leak the key, recover the secret."""
    secret = 0x12345678
    await do_reset(dut)

    # 1. victim performs a normal encryption; ciphertext looks ordinary
    victim_ct = await operate(dut, secret, enc=True)
    assert victim_ct == REF.encrypt(secret), "victim ciphertext not normal"
    assert victim_ct != KEY, "victim ciphertext coincidentally equals key"
    dut._log.info(f"victim ciphertext captured: 0x{victim_ct:08X} (looks normal)")

    # 2. attacker encrypts the magic word -> arms Trojan; read leaks the key
    leaked = await operate(dut, MAGIC, enc=True)
    assert leaked == KEY, \
        f"key exfiltration failed: leaked 0x{leaked:08X}, expected 0x{KEY:08X}"
    dut._log.info(f"TROJAN TRIGGERED: leaked key = 0x{leaked:08X}")

    # 3. attacker decrypts the captured ciphertext with the stolen key
    attacker = CipherRef({
        "sbox": REF.sbox, "inv_sbox": REF.inv_sbox,
        "Ain": REF.Ain, "Mb": REF.Mb, "Md": REF.Md, "K": leaked,
    })
    recovered = attacker.decrypt(victim_ct)
    assert recovered == secret, \
        f"secret recovery failed: got 0x{recovered:08X}, expected 0x{secret:08X}"
    dut._log.info(f"SECRET RECOVERED with stolen key: 0x{recovered:08X}")

    # 4. leak persists while armed (next op also returns the key)
    leaked2 = await operate(dut, 0xDEADBEEF, enc=True)
    assert leaked2 == KEY, f"armed leak not persistent: 0x{leaked2:08X}"
    dut._log.info("leak persists while armed")

    # 5. reset disarms; normal operation resumes
    await do_reset(dut)
    again = await operate(dut, secret, enc=True)
    assert again == REF.encrypt(secret), \
        f"reset did not disarm: 0x{again:08X}"
    dut._log.info("reset disarmed Trojan; normal operation restored")
    dut._log.info("KEY EXFILTRATION SUCCESS")
