import { Select, Spin, Typography } from "antd";
import type { ReactNode } from "react";
import { useCallback, useEffect, useRef, useState } from "react";

import { listInstrumentCatalog } from "../api";

type Props = {
  value?: string[];
  onChange?: (value: string[]) => void;
  disabled?: boolean;
};

const SEARCH_DEBOUNCE_MS = 320;
const PAGE_LIMIT = 80;

/** Multi-select backed by persisted ``GET /instruments/catalog`` (strict catalog; no ad-hoc tags). */
export function CatalogSymbolSelect({ value, onChange, disabled }: Props) {
  const [options, setOptions] = useState<{ label: string; value: string }[]>([]);
  const [searching, setSearching] = useState(false);
  // null = catalog not loaded yet; 0 = catalog empty (needs sync); >0 = has entries.
  const [catalogTotal, setCatalogTotal] = useState<number | null>(null);
  // Non-null when the last catalog request failed. Kept distinct from "no
  // match" so a network/API error does not masquerade as an empty result set
  // (silent-swallow → looks like "0 hits" when the lookup actually failed).
  const [loadError, setLoadError] = useState<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
      }
    };
  }, []);

  const runSearch = useCallback(async (raw: string) => {
    const q = raw.trim();
    setSearching(true);
    try {
      // Empty query => browse the first page of the catalog so the dropdown
      // shows a pickable list before the user types anything.
      const res = await listInstrumentCatalog({ q: q || undefined, limit: PAGE_LIMIT, offset: 0 });
      setCatalogTotal(res.total);
      setLoadError(null);
      setOptions(
        res.items.map((item) => ({
          label: `${item.display_name ?? "—"} (${item.symbol})`,
          value: item.symbol,
        })),
      );
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
      setOptions([]);
    } finally {
      setSearching(false);
    }
  }, []);

  // Preload the first catalog page on mount so opening the dropdown shows a list.
  useEffect(() => {
    void runSearch("");
  }, [runSearch]);

  const onSearch = (raw: string) => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
    }
    timerRef.current = setTimeout(() => {
      void runSearch(raw);
    }, SEARCH_DEBOUNCE_MS);
  };

  let notFound: ReactNode = null;
  if (searching) {
    notFound = <Spin size="small" />;
  } else if (loadError) {
    notFound = (
      <Typography.Text type="danger" className="!text-xs">
        加载标的目录失败：{loadError}
      </Typography.Text>
    );
  } else if (catalogTotal === 0) {
    notFound = (
      <Typography.Text type="secondary" className="!text-xs">
        标的目录为空，请先在「标的」页同步后再选择。
      </Typography.Text>
    );
  } else if (options.length === 0) {
    notFound = (
      <Typography.Text type="secondary" className="!text-xs">
        没有匹配的标的
      </Typography.Text>
    );
  }

  return (
    <div>
      <Typography.Text type="secondary" className="!text-xs">
        从已入库标的目录选择（需先在「标的」页同步）
      </Typography.Text>
      <Select
        mode="multiple"
        className="mt-1 w-full"
        disabled={disabled}
        allowClear
        showSearch
        filterOption={false}
        placeholder="输入代码或名称搜索目录，选中添加"
        value={value}
        onChange={onChange}
        onSearch={onSearch}
        onDropdownVisibleChange={(open) => {
          // Refresh the default list when reopening with no active query.
          if (open && options.length === 0) {
            void runSearch("");
          }
        }}
        options={options}
        notFoundContent={notFound}
      />
    </div>
  );
}
