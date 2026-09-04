# export_cpp.py - 把 .sdw 权重导出为 ai/rl_weights.h(C 数组)
#
# 用法: python3 export_cpp.py --weights ../weights/final.sdw --out ../../ai/rl_weights.h

import argparse
import os
import struct

import numpy as np


def load_sdw(path):
    with open(path, "rb") as f:
        data = f.read()
    assert data[:4] == b"SDW1", "bad magic"
    n = struct.unpack_from("<i", data, 4)[0]
    off = 8
    layers = []
    for _ in range(n):
        in_f, out_f = struct.unpack_from("<ii", data, off)
        off += 8
        w = np.frombuffer(data, np.float32, out_f * in_f, off).reshape(out_f, in_f)
        off += 4 * out_f * in_f
        b = np.frombuffer(data, np.float32, out_f, off)
        off += 4 * out_f
        layers.append((in_f, out_f, w, b))
    return layers


def fmt_array(f, name, arr, per_line=8):
    flat = arr.flatten()
    f.write(f"static const float {name}[{len(flat)}] = {{\n")
    for i in range(0, len(flat), per_line):
        f.write("    " + ",".join(f"{v:.9g}" for v in flat[i:i + per_line]) + ",\n")
    f.write("};\n\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    layers = load_sdw(args.weights)
    assert len(layers) == 4, f"expect 4 layers, got {len(layers)}"
    assert (layers[0][0], layers[0][1]) == (412, 256)
    assert (layers[1][0], layers[1][1]) == (256, 256)
    assert (layers[2][0], layers[2][1]) == (256, 8)
    assert (layers[3][0], layers[3][1]) == (256, 1)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        f.write("// rl_weights.h - 由 export_cpp.py 自动生成,请勿手改\n")
        f.write(f"// 来源: {os.path.abspath(args.weights)}\n#pragma once\n\n")
        f.write("static const int kLayerIn[4] = {412, 256, 256, 256};\n")
        f.write("static const int kLayerOut[4] = {256, 256, 8, 1};\n\n")
        for i, (_in, _out, w, b) in enumerate(layers):
            fmt_array(f, f"kW{i}", w)
            fmt_array(f, f"kB{i}", b)
    total = sum(w.size + b.size for _, _, w, b in layers)
    print(f"exported {len(layers)} layers, {total} params -> {args.out}")


if __name__ == "__main__":
    main()
