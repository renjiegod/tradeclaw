import React from "react";
import { Button, Select } from "antd";

type Option = { label: string; value: string };

type SelectPassthroughProps = Omit<
  React.ComponentProps<typeof Select>,
  "mode" | "options" | "popupRender" | "notFoundContent" | "optionFilterProp"
>;

type Props = SelectPassthroughProps & {
  options: Option[];
  placeholder?: string;
  loading?: boolean;
  /** Optional extra node appended after the bulk-action row (e.g. per-item config). */
  children?: React.ReactNode;
};

/**
 * Multi-select with a sticky "全选 / 清除" footer inside the dropdown.
 *
 * Replaces the copy-pasted `popupRender` block that lived in AgentFormModal for
 * both the Tools and Skills pickers. The bulk actions operate on the full
 * option set, not just the filtered view, so selecting all is predictable.
 *
 * All unknown props (id / value / onChange / disabled …) are forwarded to the
 * underlying Select so an wrapping antd Form.Item keeps its label ↔ control
 * association (the previous inline Select got an injected id; a wrapper that
 * swallowed props broke `getByLabelText` and screen readers).
 */
export function MultiSelectWithBulkActions({
  options,
  placeholder,
  loading,
  children,
  ...selectProps
}: Props) {
  const allValues = options.map((option) => option.value);
  const handleChange = selectProps.onChange;

  return (
    <Select
      mode="multiple"
      allowClear
      showSearch
      optionFilterProp="label"
      placeholder={placeholder}
      options={options}
      loading={loading}
      notFoundContent={loading ? "加载中…" : "暂无选项"}
      {...selectProps}
      popupRender={(originNode: React.ReactNode) => (
        <div>
          {originNode}
          <div
            style={{
              borderTop: "1px solid #f0f0f0",
              padding: "8px",
              display: "flex",
              gap: 8,
            }}
          >
            <Button
              type="text"
              size="small"
              style={{ padding: "0 4px", height: 22 }}
              onClick={() => handleChange?.(allValues)}
            >
              全选
            </Button>
            <Button
              type="text"
              size="small"
              style={{ padding: "0 4px", height: 22 }}
              onClick={() => handleChange?.([])}
            >
              清除
            </Button>
          </div>
          {children}
        </div>
      )}
    />
  );
}
