#!/usr/bin/env python3
"""Concatenate challenge + algorithm .cu files and compile to PTX via nvcc.

Usage:
    python3 scripts/build_ptx.py <challenge> [--outdir <dir>]

Outputs <outdir>/<challenge>.ptx (default outdir: target/ptx/).

Designed to run inside the Docker GPU image where nvcc is available.
"""

import argparse
import os
import subprocess
import sys
import tempfile
from glob import glob


def main():
    parser = argparse.ArgumentParser(description="Compile .cu files to PTX")
    parser.add_argument("challenge", help="Challenge name (e.g. hypergraph, neuralnet_optimizer)")
    parser.add_argument("--outdir", default="target/ptx", help="Output directory for .ptx file")
    args = parser.parse_args()

    challenge = args.challenge
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    challenge_cus = sorted(glob(os.path.join(base, f"src/{challenge}/**/*.cu"), recursive=True))
    algorithm_cus = sorted(glob(os.path.join(base, f"src/{challenge}/algorithm/*.cu")))
    non_algorithm_cus = [f for f in challenge_cus if f not in algorithm_cus]

    if not non_algorithm_cus:
        print(f"Error: no challenge .cu files found for {challenge}", file=sys.stderr)
        sys.exit(1)

    combined = ""
    combined += '#include <stdio.h>\n#include <stdint.h>\n#include <cuda_runtime.h>\n'
    combined += '#include <math.h>\n#include <float.h>\n\n'

    for cu_path in non_algorithm_cus:
        with open(cu_path, "r") as f:
            combined += f"// --- {os.path.relpath(cu_path, base)} ---\n"
            combined += f.read() + "\n\n"

    for cu_path in algorithm_cus:
        with open(cu_path, "r") as f:
            combined += f"// --- {os.path.relpath(cu_path, base)} ---\n"
            combined += f.read() + "\n\n"

    os.makedirs(args.outdir, exist_ok=True)
    out_ptx = os.path.join(args.outdir, f"{challenge}.ptx")

    with tempfile.NamedTemporaryFile(suffix=".cu", mode="w", delete=False) as tmp:
        tmp.write(combined)
        tmp_path = tmp.name

    try:
        cmd = [
            "nvcc", "-ptx", tmp_path, "-o", out_ptx,
            "-arch", "compute_70",
            "--use_fast_math",
        ]
        print(f"Running: {' '.join(cmd)}", file=sys.stderr)
        subprocess.run(cmd, check=True)
        print(out_ptx)
    finally:
        os.unlink(tmp_path)


if __name__ == "__main__":
    main()
