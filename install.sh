#!/usr/bin/env sh
# DoYouTrade 一键安装脚本 (macOS / Linux)
# ---------------------------------------------------------------------------
# 用法一（最省事）：
#   curl -fsSL https://raw.githubusercontent.com/renjiegod/doyoutrade/main/install.sh | sh
#
# 中国网络 / Gitee 镜像：
#   curl -fsSL https://gitee.com/renjie-god/doyoutrade/raw/main/install.sh | sh
#   # 或强制走 Gitee 安装源：
#   DOYOUTRADE_MIRROR=gitee sh install.sh
#
# 用法二（先审阅再执行，推荐谨慎用户）：
#   curl -fsSL https://raw.githubusercontent.com/renjiegod/doyoutrade/main/install.sh -o install.sh
#   less install.sh          # 看清楚它做了什么
#   sh install.sh
#
# 这个脚本只负责「安装」，不会替你启动服务，也不碰任何交易账户：
#   1. 检测 / 安装 uv（Astral 的 Python 包管理器，自带 Python 3.12）
#   2. 提示是否有 Node.js（有则安装时自动打包网页控制台，没有则退化为 API + CLI）
#   3. uv tool install 把 doyoutrade 装成常驻命令（可反复 `doyoutrade` 启动、`uv tool upgrade` 升级）
#   4. 打印下一步：在你自己的终端运行 `doyoutrade`，首启会进入安装向导配置模型
#
# macOS / Linux 只运行 DoYouTrade 本体（`doyoutrade` 默认 --mode doyoutrade）。QMT 实时
# 行情 / 实盘依赖 Windows-only 的 xtquant，需在一台已登录 miniQMT 的 Windows 机器上运行
# qmt-proxy（在那台 Windows 直接 `doyoutrade`，已内置 qmt-proxy）。本机在首启向导里填入
# 那台 Windows 的地址即可，或稍后 `doyoutrade-cli account create --base-url ...` 登记。
#
# 安装源选择（优先级从高到低）：
#   DOYOUTRADE_INSTALL_SOURCE=...   # 显式源（本地目录 / fork），始终优先
#   DOYOUTRADE_MIRROR=gitee|cn|china|github|gh   # 强制镜像
#   否则探测 GitHub 连通性（短超时）；不通则自动改用 Gitee
# ---------------------------------------------------------------------------
set -eu

GITHUB_GIT_SOURCE="git+https://github.com/renjiegod/doyoutrade.git"
GITEE_GIT_SOURCE="git+https://gitee.com/renjie-god/doyoutrade.git"

info()  { printf '\033[1;36m==>\033[0m %s\n' "$1"; }
warn()  { printf '\033[1;33m[!]\033[0m %s\n' "$1"; }
ok()    { printf '\033[1;32m[✓]\033[0m %s\n' "$1"; }
die()   { printf '\033[1;31m[✗]\033[0m %s\n' "$1" >&2; exit 1; }

github_reachable() {
  # C: short probe — China networks often time out on github.com.
  command -v curl >/dev/null 2>&1 || return 1
  curl -fsSL --connect-timeout 3 --max-time 5 -o /dev/null https://github.com/ 2>/dev/null
}

resolve_default_source() {
  # D: DOYOUTRADE_MIRROR forces a side; otherwise fall back via network probe.
  mirror="$(printf '%s' "${DOYOUTRADE_MIRROR:-}" | tr '[:upper:]' '[:lower:]')"
  case "$mirror" in
    gitee|cn|china)
      printf '%s\n' "$GITEE_GIT_SOURCE"
      return
      ;;
    github|gh)
      printf '%s\n' "$GITHUB_GIT_SOURCE"
      return
      ;;
  esac
  if github_reachable; then
    printf '%s\n' "$GITHUB_GIT_SOURCE"
  else
    warn "GitHub 不可达（或超时），改用 Gitee 镜像安装源。"
    printf '%s\n' "$GITEE_GIT_SOURCE"
  fi
}

if [ -n "${DOYOUTRADE_INSTALL_SOURCE:-}" ]; then
  SOURCE="$DOYOUTRADE_INSTALL_SOURCE"
else
  SOURCE="$(resolve_default_source)"
fi

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    ok "已检测到 uv ($(uv --version 2>/dev/null || echo unknown))"
    return
  fi
  info "未检测到 uv，正在从 astral.sh 安装（会装到 ~/.local/bin）…"
  command -v curl >/dev/null 2>&1 || die "需要 curl 才能安装 uv，请先安装 curl 后重试。"
  curl -LsSf https://astral.sh/uv/install.sh | sh || die "uv 安装失败，请参考 https://docs.astral.sh/uv/ 手动安装后重试。"
  # 让 uv 在当前 shell 会话内立即可用（安装器已写入 shell 配置，供以后的终端使用）。
  export PATH="$HOME/.local/bin:$PATH"
  [ -f "$HOME/.local/bin/env" ] && . "$HOME/.local/bin/env" || true
  command -v uv >/dev/null 2>&1 || die "uv 安装后仍不可用，请重开一个终端再运行本脚本。"
  ok "uv 安装完成 ($(uv --version 2>/dev/null || echo unknown))"
}

check_node() {
  if command -v npm >/dev/null 2>&1; then
    ok "检测到 Node.js / npm — 安装时会自动打包网页控制台。"
  else
    warn "未检测到 Node.js — 将安装为「API + CLI」模式（没有网页界面）。"
    warn "  想要网页控制台：装好 Node.js LTS 后重跑本脚本即可。"
  fi
}

install_doyoutrade() {
  info "正在安装 doyoutrade（源：${SOURCE}）…"
  info "首次安装会拉取依赖并构建，可能需要几分钟，请耐心等待。"
  # --force：幂等，重复运行会覆盖重装到最新版本。
  uv tool install --force --from "$SOURCE" doyoutrade || die "安装失败，请检查网络 / 安装源后重试。"
  # 确保 uv 的工具目录（~/.local/bin）在 PATH 中（对新开的终端生效）。
  uv tool update-shell >/dev/null 2>&1 || true
  ok "doyoutrade 安装完成。"
}

main() {
  printf '\n============================================================\n'
  printf 'DoYouTrade 安装脚本\n'
  printf '============================================================\n\n'
  ensure_uv
  check_node
  install_doyoutrade

  printf '\n============================================================\n'
  ok "安装完成！下一步："
  printf '\n'
  printf '  1. 在你自己的终端运行：  \033[1mdoyoutrade\033[0m\n'
  printf '     （若提示找不到命令，重开一个终端，或运行  uv tool update-shell  后重试）\n'
  printf '  2. 首次启动会进入\033[1m安装向导\033[0m，按提示选择一个大模型供应商并填入 API Key；\n'
  printf '     如已有 Windows 上的 qmt-proxy，可在向导里填入其地址（形如 http://<win-ip>:8001）。\n'
  printf '  3. 浏览器打开  \033[1mhttp://localhost:8000\033[0m  即是完整控制台。\n'
  printf '\n'
  printf '本机为 DoYouTrade-only；QMT 实时行情 / 实盘需一台 Windows 跑 qmt-proxy（在那台机器\n'
  printf '直接运行 doyoutrade 即内置启动）。默认使用本地 SQLite，零外部数据库依赖；详见 README。\n'
  printf '升级：  uv tool upgrade doyoutrade      卸载：  uv tool uninstall doyoutrade\n'
  printf '============================================================\n\n'
}

main "$@"
