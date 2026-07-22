import { Button } from "antd";
import type { ButtonProps } from "antd";
import type { ReactNode } from "react";

type ToolbarButtonProps = Omit<ButtonProps, "children"> & {
  /** 按钮文案。桌面（lg+）显示「图标 + 文字」；窄屏（<lg）折叠为纯图标。 */
  label: ReactNode;
  /** label 的纯文本形式，用于窄屏 hover title 与 aria-label；label 为字符串时可省略。 */
  labelText?: string;
  icon?: ReactNode;
};

/**
 * 控制台工具栏按钮：桌面显示「图标 + 文字」，窄屏（<lg）自动折叠为纯图标。
 *
 * 统一各页头部 / 工具栏在手机上的紧凑度，避免每个页面各写一份
 * `<span className="hidden lg:inline">`。文案在窄屏下转为原生 title 与
 * aria-label，保证纯图标态仍可被识别 / 无障碍读出。规则见 AGENTS.md
 * 「前端移动端适配硬性规则」。
 *
 * 约定：折叠为纯图标依赖 `icon` 存在——无 icon 时文案始终显示（否则窄屏会得到空按钮）。
 */
export function ToolbarButton({ label, labelText, icon, title, ...rest }: ToolbarButtonProps) {
  const text = labelText ?? (typeof label === "string" ? label : undefined);
  return (
    <Button icon={icon} title={title ?? text} aria-label={text} {...rest}>
      {label != null ? <span className={icon ? "hidden lg:inline" : undefined}>{label}</span> : null}
    </Button>
  );
}
