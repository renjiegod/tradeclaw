const INSTRUMENT_TYPE_LABEL_MAP: Record<string, string> = {
  stock: "股票",
  etf: "ETF",
  index: "指数",
};

/**
 * Map an ``instrument_type`` value from ``instrument_catalog`` to a 中文 label.
 * Known types (stock / etf / index) get their label; other non-empty values
 * pass through unchanged; ``null`` / ``undefined`` / empty render as ``—``.
 */
export function instrumentTypeLabel(type: string | null | undefined): string {
  if (type == null || type === "") {
    return "—";
  }
  return INSTRUMENT_TYPE_LABEL_MAP[type] ?? type;
}
