// 构建期类型门禁：只拦「未定义标识符 / 声明前使用」这类会导致运行时
// ReferenceError（整页白屏）的错误，排除测试文件与第三方库泛型噪音。
//
// 历史事故：App() 的路由表引用了不在其作用域的 deploymentMode（该 state 属于
// ConsoleShell），esbuild 把它当全局自由变量打包，运行时抛
// ReferenceError: deploymentMode is not defined，React 渲染即崩、console 白屏。
// 因为 build 脚本只有 `check:responsive && vite build`、从不跑 tsc，这个未定义
// 变量一路溜到了生产。本门禁就是为堵住这一类问题。
//
// 为什么不整包 tsc：代码库积累了数百个历史类型错误（多为测试文件缺 jest-dom
// 类型、第三方图表库复杂泛型不兼容），运行时基本无害，一次性修完不现实且有回归
// 风险。本门禁只对「名字不存在 / 用在声明前」这类真正会崩的错误 fail，既能落地
// 又精准防住白屏复发。完整类型检查见 `npm run typecheck`。
import { spawnSync } from "node:child_process";

// 会导致运行时崩溃的错误码：名字未定义、拼写未命中、块级变量声明前使用、
// 变量赋值前使用。
const FATAL = new Set(["TS2304", "TS2552", "TS2448", "TS2454", "TS2662", "TS2663"]);
// 测试文件不进生产 bundle，其类型噪音不阻塞构建。
const IGNORE = /(\.test\.[tj]sx?|__tests__|vitest\.setup)/;

const res = spawnSync(
  "npx",
  ["tsc", "-p", "tsconfig.app.json", "--noEmit"],
  { encoding: "utf8", shell: process.platform === "win32" },
);

const out = `${res.stdout || ""}${res.stderr || ""}`;
const fatal = out.split("\n").filter((line) => {
  const m = line.match(/error (TS\d+):/);
  return m && FATAL.has(m[1]) && !IGNORE.test(line);
});

if (fatal.length > 0) {
  console.error(
    "✗ 类型门禁失败：应用源码存在未定义标识符（会导致运行时 ReferenceError / 白屏）\n",
  );
  console.error(fatal.join("\n"));
  console.error(`\n共 ${fatal.length} 处。补齐 import / 变量声明后再构建。`);
  process.exit(1);
}

console.log("✓ 类型门禁通过：应用源码无未定义标识符");
