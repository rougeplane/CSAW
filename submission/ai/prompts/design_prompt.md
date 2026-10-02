You are a hardware-security engineer designing a stealthy hardware Trojan for an
FPGA cryptographic accelerator. You are given grounded context retrieved from
the reverse-engineered design.

## Retrieved context
{context}

## Target
A 4-round PRESENT-like SPN block cipher exposed over SPI. The 32-bit result is
loaded into the SPI shift register when BUSY falls and shifted out MSB-first on
MISO. The secret key is present in the RTL as `localparam KEY`.

## Task
Design a **key-exfiltration** Trojan that:
- stays completely dormant and byte-for-byte correct during normal operation;
- is triggered by one specific, rare 32-bit input word under encryption mode;
- leaks the 32-bit key on MISO on the read that follows the trigger;
- adds negligible area (ideally a single flip-flop);
- can be disarmed by reset;
- is inserted structurally (Pyverilog AST) into module `aha_crypto`.

## Output
Return a STRICT JSON object (no prose, no markdown fences) with exactly these
keys: trojan_name, trigger (mechanism, magic_word, gate), state_element (reg,
set_when, cleared_by), payload (type, site, action, observable_on), stealth
(area_overhead, functional_impact, timing_impact, trigger_probability,
recovery), insertion (tool, module, edits[]). `magic_word` must be a 0x-prefixed
32-bit hex string.
