import { AutoComplete, Select, Spin, Typography } from "antd";
import type { CSSProperties, ReactNode } from "react";
import { useCallback, useEffect, useRef, useState } from "react";

import { listInstrumentCatalog, searchInstrumentUniverse } from "../api";
import { DEFAULT_INSTRUMENT_SOURCE } from "./UniverseSymbolSelect";

const SEARCH_DEBOUNCE_MS = 320;
const CATALOG_LIMIT = 50;
const UNIVERSE_LIMIT = 50;

export type SymbolOption = { label: string; value: string; name: string | null };

/** Remote symbol suggestions merged from two sources: the persisted local
 * catalog (``GET /instruments/catalog``, browsable with an empty query) and,
 * when ``includeUniverse``, the upstream listed-instrument universe
 * (``GET /instrument-universe/search``, requires a query). Catalog hits come
 * first; universe hits fill in symbols the catalog does not have yet, so
 * suggestions work even before a catalog sync. Callers that must stay strictly
 * on already-persisted instruments (e.g. the watchlist) pass
 * ``includeUniverse: false``. */
function useSymbolOptions(includeUniverse: boolean) {
  const [options, setOptions] = useState<SymbolOption[]>([]);
  const [searching, setSearching] = useState(false);
  // Non-null when every applicable source failed. Kept distinct from "no
  // match" so a network/API error does not masquerade as an empty result set.
  const [loadError, setLoadError] = useState<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const seqRef = useRef(0);

  useEffect(() => {
    return () => {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
      }
    };
  }, []);

  const runSearch = useCallback(async (raw: string) => {
    const q = raw.trim();
    const seq = ++seqRef.current;
    setSearching(true);

    const catalogPromise = listInstrumentCatalog({ q: q || undefined, limit: CATALOG_LIMIT, offset: 0 }).then(
      (res) =>
        res.items.map((item) => ({
          label: `${item.display_name ?? "—"} (${item.symbol})`,
          value: item.symbol,
          name: item.display_name ?? null,
        })),
    );
    // The universe endpoint returns nothing for an empty query; skip the call.
    const universePromise =
      includeUniverse && q
        ? searchInstrumentUniverse({ source: DEFAULT_INSTRUMENT_SOURCE, q, limit: UNIVERSE_LIMIT }).then((res) =>
            res.items.map((item) => ({
              label: `${item.name} (${item.symbol})`,
              value: item.symbol,
              name: item.name || null,
            })),
          )
        : Promise.resolve<SymbolOption[]>([]);

    const [catalogResult, universeResult] = await Promise.allSettled([catalogPromise, universePromise]);
    if (seq !== seqRef.current) {
      return; // stale response; a newer search already started
    }

    const merged: SymbolOption[] = [];
    const seen = new Set<string>();
    for (const result of [catalogResult, universeResult]) {
      if (result.status !== "fulfilled") {
        continue;
      }
      for (const option of result.value) {
        if (!seen.has(option.value)) {
          seen.add(option.value);
          merged.push(option);
        }
      }
    }

    const failures = [catalogResult, universeResult].filter(
      (result): result is PromiseRejectedResult => result.status === "rejected",
    );
    if (merged.length === 0 && failures.length > 0) {
      const reason = failures[0]!.reason;
      setLoadError(reason instanceof Error ? reason.message : String(reason));
    } else {
      setLoadError(null);
    }
    setOptions(merged);
    setSearching(false);
  }, [includeUniverse]);

  const debouncedSearch = useCallback(
    (raw: string) => {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
      }
      timerRef.current = setTimeout(() => {
        void runSearch(raw);
      }, SEARCH_DEBOUNCE_MS);
    },
    [runSearch],
  );

  return { options, searching, loadError, runSearch, debouncedSearch };
}

function notFoundContent(searching: boolean, loadError: string | null): ReactNode {
  if (searching) {
    return <Spin size="small" />;
  }
  if (loadError) {
    return (
      <Typography.Text type="danger" className="!text-xs">
        搜索标的失败：{loadError}
      </Typography.Text>
    );
  }
  return (
    <Typography.Text type="secondary" className="!text-xs">
      没有匹配的标的
    </Typography.Text>
  );
}

type TagsProps = {
  /** Injected by antd Form.Item so the label's ``for`` resolves. */
  id?: string;
  value?: string[];
  onChange?: (value: string[]) => void;
  disabled?: boolean;
  placeholder?: string;
};

/** Multi-select with remote symbol suggestions. ``tags`` mode: unknown codes
 * can still be typed and confirmed with Enter, and comma / whitespace pasting
 * splits into individual symbols. */
export function SymbolTagsSelect({ id, value, onChange, disabled, placeholder }: TagsProps) {
  const { options, searching, loadError, runSearch, debouncedSearch } = useSymbolOptions(true);

  // Preload the first catalog page so opening the dropdown shows a pickable list.
  useEffect(() => {
    void runSearch("");
  }, [runSearch]);

  return (
    <Select
      id={id}
      mode="tags"
      className="w-full"
      disabled={disabled}
      allowClear
      showSearch
      filterOption={false}
      placeholder={placeholder ?? "输入代码或名称搜索，选中添加；也可输入代码后回车"}
      value={value}
      onChange={onChange}
      onSearch={debouncedSearch}
      options={options}
      tokenSeparators={[",", "，", " ", "\n"]}
      notFoundContent={notFoundContent(searching, loadError)}
    />
  );
}

type SingleProps = {
  /** Injected by antd Form.Item so the label's ``for`` resolves. */
  id?: string;
  value?: string;
  onChange?: (value: string) => void;
  /** Fires with the picked option so callers can capture the display name. */
  onSelectOption?: (option: { symbol: string; name: string | null }) => void;
  disabled?: boolean;
  placeholder?: string;
  /** Also offer upstream listed instruments not yet in the local catalog.
   * Defaults to false: pick strictly from persisted catalog rows. */
  includeUniverse?: boolean;
  /** Shown when the search legitimately matched nothing (not on load errors). */
  emptyHint?: string;
};

/** Single-pick symbol select with remote suggestions. Unlike
 * {@link SymbolAutoComplete} the value must be picked from the option list, so
 * only canonical symbols come out. */
export function SymbolSingleSelect({
  id,
  value,
  onChange,
  onSelectOption,
  disabled,
  placeholder,
  includeUniverse = false,
  emptyHint,
}: SingleProps) {
  const { options, searching, loadError, runSearch, debouncedSearch } = useSymbolOptions(includeUniverse);

  // Preload the first catalog page so opening the dropdown shows a pickable list.
  useEffect(() => {
    void runSearch("");
  }, [runSearch]);

  let notFound = notFoundContent(searching, loadError);
  if (!searching && !loadError && emptyHint) {
    notFound = (
      <Typography.Text type="secondary" className="!text-xs">
        {emptyHint}
      </Typography.Text>
    );
  }

  return (
    <Select
      id={id}
      className="w-full"
      disabled={disabled}
      allowClear
      showSearch
      filterOption={false}
      placeholder={placeholder ?? "输入代码、名称、拼音或首字母搜索"}
      value={value}
      onChange={onChange}
      onSearch={debouncedSearch}
      onSelect={(symbol, option) => {
        onSelectOption?.({ symbol: String(symbol), name: (option as SymbolOption).name ?? null });
      }}
      options={options}
      notFoundContent={notFound}
    />
  );
}

type AutoCompleteProps = {
  /** Injected by antd Form.Item so the label's ``for`` resolves. */
  id?: string;
  value?: string;
  onChange?: (value: string) => void;
  disabled?: boolean;
  placeholder?: string;
  style?: CSSProperties;
};

/** Single free-text input with remote symbol suggestions — a drop-in upgrade
 * for filter boxes that previously used a bare ``Input``: picking a suggestion
 * fills the canonical symbol, while arbitrary typed text still passes through. */
export function SymbolAutoComplete({ id, value, onChange, disabled, placeholder, style }: AutoCompleteProps) {
  const { options, searching, loadError, debouncedSearch } = useSymbolOptions(true);

  return (
    <AutoComplete
      id={id}
      value={value}
      onChange={(next) => onChange?.(next ?? "")}
      onSearch={debouncedSearch}
      options={options}
      disabled={disabled}
      allowClear
      style={style}
      placeholder={placeholder ?? "标的代码 / 名称 / 拼音搜索"}
      // A filter box accepts arbitrary text, so an empty suggestion list is not
      // an error state; only surface the spinner and real load failures.
      notFoundContent={searching ? <Spin size="small" /> : loadError ? notFoundContent(false, loadError) : null}
    />
  );
}
