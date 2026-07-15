"""Zero-dependency interactive terminal menu (arrow keys + numbered fallback).

Used by the first-run setup wizard so operators can pick a provider the way
``npx`` / OpenCode do — ↑↓ to move, Enter to confirm — without pulling in
``questionary`` / ``@clack/prompts``.

Design rules:

- Never block non-interactive startups: callers already gate on TTY; this module
  still falls back to numbered ``input()`` when raw mode is unavailable.
- Ctrl-C / Esc / EOF → treat as skip (``None``) when ``allow_skip`` is True,
  otherwise re-raise ``KeyboardInterrupt`` for Esc-less cancel paths.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence


def select_index(
    message: str,
    options: Sequence[str],
    *,
    allow_skip: bool = True,
    skip_label: str = "跳过（稍后在网页配置）",
    default: int = 0,
) -> int | None:
    """Prompt for one of ``options``.

    Returns a 0-based index into ``options``, or ``None`` if the user skipped /
    cancelled (only when ``allow_skip`` is True).
    """

    if not options:
        raise ValueError("select_index requires at least one option")
    if not (0 <= default < len(options)):
        raise ValueError(f"default index {default} out of range for {len(options)} options")

    if _can_use_arrow_menu():
        try:
            return _arrow_select(
                message,
                options,
                allow_skip=allow_skip,
                skip_label=skip_label,
                default=default,
            )
        except (KeyboardInterrupt, EOFError):
            if allow_skip:
                print("\n", flush=True)
                return None
            raise
        except Exception as exc:  # noqa: BLE001 — fall back rather than kill the wizard
            print(
                f"\n（方向键菜单不可用：{type(exc).__name__}: {exc}；改用编号选择）",
                flush=True,
            )

    return _numbered_select(
        message,
        options,
        allow_skip=allow_skip,
        skip_label=skip_label,
        default=default,
    )


def _can_use_arrow_menu() -> bool:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    if sys.platform == "win32":
        try:
            import msvcrt  # noqa: F401
        except ImportError:
            return False
        return True
    try:
        import termios  # noqa: F401
        import tty  # noqa: F401
    except ImportError:
        return False
    return True


def _numbered_select(
    message: str,
    options: Sequence[str],
    *,
    allow_skip: bool,
    skip_label: str,
    default: int,
) -> int | None:
    print(f"\n{message}", flush=True)
    for idx, label in enumerate(options, start=1):
        print(f"  {idx}. {label}", flush=True)
    if allow_skip:
        print(f"  0. {skip_label}", flush=True)

    hint = f"请输入编号 [{default + 1}]"
    raw = input(f"{hint}: ").strip()
    if not raw:
        return default
    if allow_skip and raw == "0":
        return None
    try:
        choice = int(raw)
    except ValueError:
        print("无效编号，已跳过。", flush=True)
        return None
    if 1 <= choice <= len(options):
        return choice - 1
    print("无效编号，已跳过。", flush=True)
    return None


def _arrow_select(
    message: str,
    options: Sequence[str],
    *,
    allow_skip: bool,
    skip_label: str,
    default: int,
) -> int | None:
    rows = list(options)
    if allow_skip:
        rows = [*rows, skip_label]
    index = min(default, len(options) - 1)
    line_count = 1 + len(rows)  # message + options

    print(flush=True)
    _draw_menu(message, rows, index)
    try:
        while True:
            key = _read_key()
            if key == "up":
                index = (index - 1) % len(rows)
                _clear_menu_lines(line_count)
                _draw_menu(message, rows, index)
            elif key == "down":
                index = (index + 1) % len(rows)
                _clear_menu_lines(line_count)
                _draw_menu(message, rows, index)
            elif key == "enter":
                _clear_menu_lines(line_count)
                if allow_skip and index == len(options):
                    print(f"{message}\n  → {skip_label}\n", flush=True)
                    return None
                print(f"{message}\n  → {options[index]}\n", flush=True)
                return index
            elif key in ("esc", "ctrl-c"):
                _clear_menu_lines(line_count)
                if allow_skip:
                    print(f"{message}\n  → {skip_label}\n", flush=True)
                    return None
                raise KeyboardInterrupt
    finally:
        # Ensure cursor is visible even if we bail mid-loop.
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()


def _draw_menu(message: str, rows: Sequence[str], index: int) -> None:
    sys.stdout.write("\033[?25l")  # hide cursor
    sys.stdout.write(f"{message}\n")
    for i, label in enumerate(rows):
        marker = "❯" if i == index else " "
        # Highlight selected row.
        if i == index:
            sys.stdout.write(f"  \033[36m{marker} {label}\033[0m\n")
        else:
            sys.stdout.write(f"  {marker} {label}\n")
    sys.stdout.flush()


def _clear_menu_lines(count: int) -> None:
    for _ in range(count):
        sys.stdout.write("\033[1A\033[2K")
    sys.stdout.flush()


def _read_key() -> str:
    """Return a normalized key name: up/down/enter/esc/ctrl-c."""

    if sys.platform == "win32":
        return _read_key_windows()
    return _read_key_posix()


def _read_key_windows() -> str:
    import msvcrt

    ch = msvcrt.getwch()
    if ch in ("\r", "\n"):
        return "enter"
    if ch == "\x03":
        return "ctrl-c"
    if ch == "\x1b":
        return "esc"
    if ch in ("\x00", "\xe0"):
        ch2 = msvcrt.getwch()
        if ch2 == "H":
            return "up"
        if ch2 == "P":
            return "down"
    return "other"


def _read_key_posix() -> str:
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\x03":
            return "ctrl-c"
        if ch == "\x1b":
            # CSI sequences: ESC [ A/B ; bare ESC cancels.
            rest = sys.stdin.read(1)
            if rest != "[":
                return "esc"
            arrow = sys.stdin.read(1)
            if arrow == "A":
                return "up"
            if arrow == "B":
                return "down"
            return "other"
        return "other"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
