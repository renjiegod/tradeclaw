import { Select, Space, Spin, Typography } from "antd";
import { useCallback, useEffect, useRef, useState } from "react";

import { searchInstrumentUniverse } from "../api";

export const DEFAULT_INSTRUMENT_SOURCE = "akshare_a";

export const INSTRUMENT_SOURCE_OPTIONS = [{ label: "A股 · akshare", value: "akshare_a" }];

type Props = {
  value?: string[];
  onChange?: (value: string[]) => void;
  disabled?: boolean;
};

const SEARCH_DEBOUNCE_MS = 320;

export function UniverseSymbolSelect({ value, onChange, disabled }: Props) {
  const [source, setSource] = useState(DEFAULT_INSTRUMENT_SOURCE);
  const [options, setOptions] = useState<{ label: string; value: string }[]>([]);
  const [searching, setSearching] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
      }
    };
  }, []);

  const runSearch = useCallback(
    async (raw: string) => {
      const q = raw.trim();
      if (!q) {
        setOptions([]);
        return;
      }
      setSearching(true);
      try {
        const res = await searchInstrumentUniverse({ source, q, limit: 50 });
        setOptions(
          res.items.map((item) => ({
            label: `${item.name} (${item.symbol})`,
            value: item.symbol,
          })),
        );
      } catch {
        setOptions([]);
      } finally {
        setSearching(false);
      }
    },
    [source],
  );

  const onSearch = (raw: string) => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
    }
    timerRef.current = setTimeout(() => {
      void runSearch(raw);
    }, SEARCH_DEBOUNCE_MS);
  };

  return (
    <Space direction="vertical" size="small" className="w-full">
      <div>
        <Typography.Text type="secondary" className="!text-xs">
          列表来源
        </Typography.Text>
        <Select
          className="mt-1 w-full"
          disabled={disabled}
          options={INSTRUMENT_SOURCE_OPTIONS}
          value={source}
          onChange={(v) => {
            setSource(v);
            setOptions([]);
          }}
          aria-label="instrument-universe-source"
        />
      </div>
      <Select
        mode="tags"
        className="w-full"
        disabled={disabled}
        allowClear
        showSearch
        filterOption={false}
        placeholder="输入代码或名称搜索，选中添加；也可直接输入自定义代码后回车"
        value={value}
        onChange={onChange}
        onSearch={onSearch}
        options={options}
        tokenSeparators={[",", " ", "\n"]}
        notFoundContent={searching ? <Spin size="small" /> : null}
      />
    </Space>
  );
}
