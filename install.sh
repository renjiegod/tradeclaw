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
#   2. 默认从 GitHub / Gitee Release 安装预构建 wheel（已内嵌网页控制台，无需 Node）
#   3. uv tool install 把 doyoutrade 装成常驻命令
#   4. 打印下一步：在你自己的终端运行 `doyoutrade`，首启在网页向导配置模型
#
# macOS / Linux 只运行 DoYouTrade 本体（`doyoutrade` 默认 --mode doyoutrade）。QMT 实时
# 行情 / 实盘依赖 Windows-only 的 xtquant，需在一台已登录 miniQMT 的 Windows 机器上运行
# qmt-proxy（在那台 Windows 直接 `doyoutrade`，已内置 qmt-proxy）。本机在首启向导里填入
# 那台 Windows 的地址即可，或稍后 `doyoutrade-cli account create --base-url ...` 登记。
#
# 安装源选择（优先级从高到低）：
#   DOYOUTRADE_INSTALL_SOURCE=...   # 显式源（本地目录 / fork / git+），始终优先
#   DOYOUTRADE_INSTALL_VERSION=...  # 指定 Release 版本（如 0.1.10）；默认 latest
#   DOYOUTRADE_MIRROR=gitee|cn|china|github|gh   # 强制镜像
#   否则探测 GitHub 连通性（短超时）；不通则自动改用 Gitee
# ---------------------------------------------------------------------------
set -eu

GITHUB_GIT_SOURCE="git+https://github.com/renjiegod/doyoutrade.git"
GITEE_GIT_SOURCE="git+https://gitee.com/renjie-god/doyoutrade.git"
GITHUB_OWNER_REPO="renjiegod/doyoutrade"
GITEE_OWNER="renjie-god"
GITEE_REPO="doyoutrade"

info()  { printf '\033[1;36m==>\033[0m %s\n' "$1"; }
warn()  { printf '\033[1;33m[!]\033[0m %s\n' "$1"; }
ok()    { printf '\033[1;32m[✓]\033[0m %s\n' "$1"; }
die()   { printf '\033[1;31m[✗]\033[0m %s\n' "$1" >&2; exit 1; }

github_reachable() {
  # C: short probe — China networks often time out on github.com.
  command -v curl >/dev/null 2>&1 || return 1
  curl -fsSL --connect-timeout 3 --max-time 5 -o /dev/null https://github.com/ 2>/dev/null
}

preferred_mirror() {
  mirror="$(printf '%s' "${DOYOUTRADE_MIRROR:-}" | tr '[:upper:]' '[:lower:]')"
  case "$mirror" in
    gitee|cn|china) printf '%s\n' "gitee"; return ;;
    github|gh) printf '%s\n' "github"; return ;;
  esac
  if github_reachable; then
    printf '%s\n' "github"
  else
    warn "GitHub 不可达（或超时），改用 Gitee 镜像。"
    printf '%s\n' "gitee"
  fi
}

normalize_release_tag() {
  # prints: tag_with_v<TAB>bare_version
  t="$(printf '%s' "$1" | tr -d '[:space:]')"
  [ -n "$t" ] || die "release tag is empty"
  case "$t" in
    v*|V*)
      bare="${t#?}"
      printf 'v%s\t%s\n' "$bare" "$bare"
      ;;
    *)
      printf 'v%s\t%s\n' "$t" "$t"
      ;;
  esac
}

wheel_filename() {
  bare="$(normalize_release_tag "$1" | cut -f2)"
  printf 'doyoutrade-%s-py3-none-any.whl\n' "$bare"
}

resolve_release_wheel_url() {
  # $1=tag $2=mirror
  tag_line="$(normalize_release_tag "$1")"
  tagged="$(printf '%s' "$tag_line" | cut -f1)"
  bare="$(printf '%s' "$tag_line" | cut -f2)"
  file="$(wheel_filename "$bare")"
  mirror="$2"
  case "$mirror" in
    gitee)
      printf 'https://gitee.com/%s/%s/releases/download/%s/%s\n' \
        "$GITEE_OWNER" "$GITEE_REPO" "$tagged" "$file"
      ;;
    *)
      printf 'https://github.com/%s/releases/download/%s/%s\n' \
        "$GITHUB_OWNER_REPO" "$tagged" "$file"
      ;;
  esac
}

latest_release_tag() {
  mirror="$1"
  if [ "$mirror" = "gitee" ]; then
    api="https://gitee.com/api/v5/repos/${GITEE_OWNER}/${GITEE_REPO}/releases/latest"
  else
    api="https://api.github.com/repos/${GITHUB_OWNER_REPO}/releases/latest"
  fi
  command -v curl >/dev/null 2>&1 || die "需要 curl 才能查询 Release / 下载 wheel。"
  payload="$(curl -fsSL --connect-timeout 10 --max-time 30 "$api")" \
    || die "无法读取 $mirror latest Release"
  # Prefer python (usually present); fall back to sed for tag_name.
  if command -v python3 >/dev/null 2>&1; then
    printf '%s' "$payload" | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])'
  elif command -v python >/dev/null 2>&1; then
    printf '%s' "$payload" | python -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])'
  else
    printf '%s' "$payload" | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1
  fi
}

remote_url_exists() {
  url="$1"
  command -v curl >/dev/null 2>&1 || return 1
  code="$(curl -sS -o /dev/null -w '%{http_code}' -I --connect-timeout 10 --max-time 20 "$url" || true)"
  case "$code" in
    2??|3??) return 0 ;;
  esac
  code="$(curl -sS -o /dev/null -w '%{http_code}' -r 0-0 --connect-timeout 10 --max-time 30 "$url" || true)"
  case "$code" in
    2??|3??) return 0 ;;
    *) return 1 ;;
  esac
}

is_dev_or_ci_version() {
  v="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  case "$v" in
    *dev*|*ci*|0.0.0*) return 0 ;;
    *) return 1 ;;
  esac
}

resolve_default_source() {
  mirror="$(preferred_mirror)"
  tag_input="${DOYOUTRADE_INSTALL_VERSION:-}"
  if [ -z "$tag_input" ]; then
    info "未指定版本——查询 $mirror latest Release…"
    tag_input="$(latest_release_tag "$mirror")"
    [ -n "$tag_input" ] || die "$mirror latest Release 缺少 tag_name"
  fi
  tag_line="$(normalize_release_tag "$tag_input")"
  tagged="$(printf '%s' "$tag_line" | cut -f1)"
  bare="$(printf '%s' "$tag_line" | cut -f2)"
  wheel_url="$(resolve_release_wheel_url "$tagged" "$mirror")"
  info "安装源：$mirror Release $tagged → $wheel_url"
  if remote_url_exists "$wheel_url"; then
    printf '%s\n' "$wheel_url"
    return
  fi
  if is_dev_or_ci_version "$bare"; then
    if [ "$mirror" = "gitee" ]; then
      git_source="$GITEE_GIT_SOURCE"
    else
      git_source="$GITHUB_GIT_SOURCE"
    fi
    warn "未找到预构建 wheel（开发/CI 版本 $tagged）——回退到源码安装：$git_source"
    warn "正式用户请安装带版本号的 Release 包；源码安装需要本机 Node.js 才能打包网页控制台。"
    printf '%s\n' "$git_source"
    return
  fi
  die "Release wheel 不存在或无法下载：$wheel_url（请确认 $mirror 上已发布 $tagged 的 .whl）"
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

warn_if_source_install_without_node() {
  case "$SOURCE" in
    *.whl) return ;;
    git+*|*archive*|*.zip) ;;
    *) return ;;
  esac
  if command -v npm >/dev/null 2>&1; then
    ok "检测到 Node.js / npm — 源码安装时会打包网页控制台。"
  else
    warn "正在从源码安装且未检测到 Node.js — 打出的包可能没有网页控制台。"
    warn "  正式用户请改用 Release 预构建 wheel（不要设置 DOYOUTRADE_INSTALL_SOURCE）。"
  fi
}

install_doyoutrade() {
  info "正在安装 doyoutrade（源：${SOURCE}）…"
  info "首次安装会拉取依赖，可能需要几分钟，请耐心等待。"
  warn_if_source_install_without_node
  # PEP 508 direct reference — same shape as Windows / in-app updater.
  uv tool install --force "doyoutrade @ ${SOURCE}" || die "安装失败，请检查网络 / 安装源后重试。"
  # 确保 uv 的工具目录（~/.local/bin）在 PATH 中（对新开的终端生效）。
  uv tool update-shell >/dev/null 2>&1 || true
  ok "doyoutrade 安装完成。"
}

main() {
  printf '\n============================================================\n'
  printf 'DoYouTrade 安装脚本\n'
  printf '============================================================\n\n'
  ensure_uv
  install_doyoutrade

  printf '\n============================================================\n'
  ok "安装完成！下一步："
  printf '\n'
  printf '  1. 在你自己的终端运行：  \033[1mdoyoutrade\033[0m\n'
  printf '     （若提示找不到命令，重开一个终端，或运行  uv tool update-shell  后重试）\n'
  printf '  2. 首次启动会自动打开网页控制台，在向导里选择大模型供应商并填入 API Key；\n'
  printf '     如已有 Windows 上的 qmt-proxy，可在设置里填入其地址（形如 http://<win-ip>:8001）。\n'
  printf '  3. 浏览器打开  \033[1mhttp://localhost:8000\033[0m  即是完整控制台。\n'
  printf '\n'
  printf '本机为 DoYouTrade-only；QMT 实时行情 / 实盘需一台 Windows 跑 qmt-proxy（在那台机器\n'
  printf '直接运行 doyoutrade 即内置启动）。默认使用本地 SQLite，零外部数据库依赖；详见 README。\n'
  printf '升级：应用内自动更新，或重跑本脚本。卸载：  uv tool uninstall doyoutrade\n'
  printf '============================================================\n\n'
}

main "$@"
