// mlp.h - SDW1 权重的加载与前向(仅训练环境使用;部署用生成的内嵌代码)
// 契约见 rl/SPEC.md §6:fc1(412→256) fc2(256→256) pi(256→8) vf(256→1),ReLU。

#pragma once

#include <array>
#include <cstdint>
#include <cstdio>
#include <stdexcept>
#include <string>
#include <vector>

namespace rl {

inline constexpr int OBS_DIM = 8 * 49 + 20;  // 412
inline constexpr int ACT_DIM = 8;

struct Mlp {
    struct Layer {
        int in = 0, out = 0;
        std::vector<float> w;  // 行主序 out×in
        std::vector<float> b;  // out
    };
    std::vector<Layer> layers;  // [fc1, fc2, pi, vf]

    static Mlp load(const std::string& path) {
        FILE* f = std::fopen(path.c_str(), "rb");
        if (!f) throw std::runtime_error("mlp: 打不开权重文件 " + path);
        char magic[4];
        if (std::fread(magic, 1, 4, f) != 4 || std::string(magic, 4) != "SDW1")
            throw std::runtime_error("mlp: 非法 magic");
        int32_t n = 0;
        if (std::fread(&n, 4, 1, f) != 1 || n != 4)
            throw std::runtime_error("mlp: 层数不为 4");
        Mlp m;
        for (int i = 0; i < n; ++i) {
            int32_t in = 0, out = 0;
            if (std::fread(&in, 4, 1, f) != 1 || std::fread(&out, 4, 1, f) != 1)
                throw std::runtime_error("mlp: 层头损坏");
            Layer L;
            L.in = in; L.out = out;
            L.w.resize(static_cast<size_t>(in) * out);
            L.b.resize(out);
            if (std::fread(L.w.data(), 4, L.w.size(), f) != L.w.size() ||
                std::fread(L.b.data(), 4, L.b.size(), f) != L.b.size())
                throw std::runtime_error("mlp: 权重数据损坏");
            m.layers.push_back(std::move(L));
        }
        std::fclose(f);
        return m;
    }

    // 前向:x → policy logits(8) 与 value。
    void forward(const float* x, std::array<float, ACT_DIM>& logits, float& value) const {
        std::vector<float> h1(layers[0].out), h2(layers[1].out);
        matvec_relu(layers[0], x, h1.data());
        matvec_relu(layers[1], h1.data(), h2.data());
        std::array<float, ACT_DIM> lg;
        matvec(layers[2], h2.data(), lg.data());
        logits = lg;
        matvec(layers[3], h2.data(), &value);
    }

private:
    static void matvec(const Layer& L, const float* x, float* y) {
        for (int o = 0; o < L.out; ++o) {
            const float* w = L.w.data() + static_cast<size_t>(o) * L.in;
            float s = L.b[o];
            for (int i = 0; i < L.in; ++i) s += w[i] * x[i];
            y[o] = s;
        }
    }
    static void matvec_relu(const Layer& L, const float* x, float* y) {
        matvec(L, x, y);
        for (int o = 0; o < L.out; ++o)
            if (y[o] < 0.0f) y[o] = 0.0f;
    }
};

}  // namespace rl
