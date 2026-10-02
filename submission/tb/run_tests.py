#!/usr/bin/env python3
"""
run_tests.py -- build and run the cocotb testbench against the Trojaned RTL.

Uses the cocotb runner (Icarus backend, timescale 1ns/1ps).  Exits non-zero if
any test fails, so it can gate the pipeline / run_all.sh.

Usage: run_tests.py [trojan_rtl] [cipher_model.json] [testcase]
       (testcase optional, e.g. test_normal_operation, to run a single test)
"""
import os
import sys
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)
from cocotb_tools.runner import get_runner, get_results

HERE = os.path.dirname(os.path.abspath(__file__))
SUB = os.path.dirname(HERE)
TOOLS = os.path.join(SUB, "tools")


def main():
    rtl = sys.argv[1] if len(sys.argv) > 1 else os.path.join(SUB, "rtl", "aha_crypto_trojan.v")
    model = sys.argv[2] if len(sys.argv) > 2 else os.path.join(TOOLS, "cipher_model.json")
    testcase = sys.argv[3] if len(sys.argv) > 3 else None
    rtl = os.path.abspath(rtl)
    model = os.path.abspath(model)
    if not os.path.exists(rtl):
        sys.exit(f"[run_tests] RTL not found: {rtl}")
    if not os.path.exists(model):
        sys.exit(f"[run_tests] cipher_model.json not found: {model} "
                 f"(run model_recover.py first)")

    build_dir = os.path.join(HERE, "sim_build")
    runner = get_runner("icarus")
    runner.build(
        verilog_sources=[rtl],
        hdl_toplevel="aha_crypto",
        timescale=("1ns", "1ps"),
        build_dir=build_dir,
        always=True,
    )
    results_xml = runner.test(
        test_module="tb_crypto",
        hdl_toplevel="aha_crypto",
        testcase=testcase,
        timescale=("1ns", "1ps"),
        build_dir=build_dir,
        test_dir=HERE,
        extra_env={
            "AHA_TOOLS": TOOLS,
            "AHA_MODEL": model,
            "PYTHONPATH": HERE + os.pathsep + TOOLS + os.pathsep
            + os.environ.get("PYTHONPATH", ""),
        },
    )
    num_tests, num_failed = get_results(results_xml)
    print(f"[run_tests] {num_tests} tests, {num_failed} failed")
    if num_failed:
        sys.exit(1)
    print("[run_tests] ALL COCOTB TESTS PASSED")


if __name__ == "__main__":
    main()
