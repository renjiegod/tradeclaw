#!/usr/bin/env node
// 移动端适配静态门禁（AGENTS.md「前端移动端适配硬性规则」的机器执行面）。
//
// 拦截三类曾导致手机端整页塌掉的写法（对话页输入框不可见事故的根因）：
//   1. 无响应式前缀的固定像素 grid 列（grid-cols-[...NNNpx...]）——
//      手机上固定列吃掉全部宽度，minmax(0,1fr) 主列塌成 0；
//   2. 无响应式前缀的固定宽度 ≥240px（w-/min-w-/basis-[NNNpx]）；
//   3. 裸 100vh —— 手机浏览器动态工具栏下真实可视高度小于 100vh，
//      底部内容（输入框）被裁掉，必须用 100dvh。
//
// 带 sm:/md:/lg:/xl:/2xl:/max-*: 前缀（即显式声明了桌面才生效）的写法放行。
import { readdirSync, readFileSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join, relative } from "node:path";

const SRC = fileURLToPath(new URL("../src", import.meta.url));
const VARIANT = /^(?:sm|md|lg|xl|2xl|max-sm|max-md|max-lg|max-xl|max-2xl|dark|hover|focus|group-\w+|peer-\w+)$/;

const violations = [];

function* walk(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) {
      yield* walk(path);
    } else if (/\.(tsx|ts|css)$/.test(name) && !/\.test\.(tsx|ts)$/.test(name)) {
      yield path;
    }
  }
}

function hasResponsiveVariant(token) {
  const parts = token.split(":");
  return parts.length > 1 && parts.slice(0, -1).every((p) => VARIANT.test(p));
}

for (const file of walk(SRC)) {
  const rel = relative(join(SRC, ".."), file);
  const lines = readFileSync(file, "utf8").split("\n");
  lines.forEach((line, i) => {
    const loc = `${rel}:${i + 1}`;

    // 规则 3：裸 100vh（100dvh / 100svh 放行；CSS 与 TSX 内联样式都查）
    if (/100vh/.test(line) && !/100[ds]vh/.test(line.replace(/100vh/g, ""))) {
      violations.push(`${loc}  裸 100vh（手机动态工具栏会裁掉底部内容），改用 100dvh：${line.trim().slice(0, 120)}`);
    }

    if (!file.endsWith(".css")) {
      for (const token of line.split(/[\s"'`{}]+/)) {
        if (!token || hasResponsiveVariant(token)) continue;
        const bare = token.split(":").pop();

        // 规则 1：固定像素 grid 列
        if (/^grid-cols-\[.*\d{3,}px/.test(bare)) {
          violations.push(`${loc}  无响应式前缀的固定像素 grid 列（手机上主列会塌成 0）：${token}`);
        }

        // 规则 2：固定宽度 ≥240px（max-w-* 只会收缩，放行）
        const m = bare.match(/^(?:w|min-w|basis)-\[(\d+)px\]$/);
        if (m && Number(m[1]) >= 240) {
          violations.push(`${loc}  无响应式前缀的固定宽度 ${m[1]}px（≥240px 需 lg: 等前缀或改 max-w）：${token}`);
        }
      }
    }
  });
}

if (violations.length > 0) {
  console.error(`check:responsive 未通过（${violations.length} 处）——规则见 AGENTS.md「前端移动端适配硬性规则」\n`);
  for (const v of violations) console.error("  " + v);
  process.exit(1);
}
console.log("check:responsive 通过");
