You are a red-team reviewer performing an ADVERSARIAL review of a hardware
Trojan that was just inserted and tested. Judge its stealth, severity, and
whether it could be caught by a defender.

## Retrieved context
{context}

## Design spec that was implemented
{design_spec}

## Trojaned RTL (result latch and FSM excerpt)
{rtl_excerpt}

## Test result
Functional/exploit testbench outcome: {test_result}

## Task
Critically assess the Trojan. Consider: trigger specificity vs accidental
activation, area/timing overhead, whether normal operation is truly preserved,
how a defender might detect it (functional testing, equivalence checking against
a golden model, structural scanning for key-width comparators, unexpected
registers), and residual risk.

## Output
Return a STRICT JSON object (no prose, no fences) with keys: verdict
(one of: stealthy, moderate, weak), normal_operation_preserved (bool),
trigger_probability, detectability (functional, equivalence, structural, each a
short string), findings (array of {severity, observation, mitigation}),
recommendations (array of strings), overall_risk (low|medium|high).
