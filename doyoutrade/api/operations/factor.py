from __future__ import annotations

import json as _json
from pathlib import Path as _Path
from typing import Any

import numpy as np
import pandas as pd

from doyoutrade.tools import OperationHandler, ToolResult
from doyoutrade.tools._prose import append_json_payload, format_error_text


def _get_artifacts_root() -> _Path:
    return _Path.home() / ".doyoutrade" / "assistant" / "artifacts"


# ---------------------------------------------------------------------------
# IC computation
# ---------------------------------------------------------------------------


def compute_ic_series(
    factor_df: pd.DataFrame,
    return_df: pd.DataFrame,
    min_common_codes: int = 5,
) -> pd.Series:
    """Compute cross-sectional Spearman IC per date.

    Args:
        factor_df: DataFrame indexed by date, columns = codes, values = factor exposures.
        return_df: DataFrame indexed by date, columns = codes, values = forward returns.
        min_common_codes: Minimum number of common codes required to compute IC for a date.

    Returns:
        Series indexed by date, values = IC (Spearman rank correlation).
    """
    # Align on common dates and columns
    common_dates = factor_df.index.intersection(return_df.index)
    common_codes = factor_df.columns.intersection(return_df.columns)

    ic_values: list[tuple[Any, float]] = []
    for dt in common_dates:
        factor_row = factor_df.loc[dt, common_codes]
        return_row = return_df.loc[dt, common_codes]

        # Drop pairs where either is NaN
        mask = factor_row.notna() & return_row.notna()
        common_codes_t = common_codes[mask.values]
        if len(common_codes_t) < min_common_codes:
            continue

        f_vals = factor_row[common_codes_t].values.astype(float)
        r_vals = return_row[common_codes_t].values.astype(float)

        if len(f_vals) < min_common_codes:
            continue

        # Spearman requires at least 2 observations
        if len(f_vals) < 2:
            continue

        # Spearman = Pearson correlation on ranks
        ic = pd.Series(f_vals).rank().corr(pd.Series(r_vals).rank())
        if not np.isnan(ic):
            ic_values.append((dt, ic))

    return pd.Series(
        [ic for _, ic in ic_values],
        index=pd.DatetimeIndex([dt for dt, _ in ic_values]),
    )


def compute_ic_summary(ic_series: pd.Series) -> dict[str, float]:
    """Compute IC summary statistics from IC series.

    Args:
        ic_series: Series of IC values indexed by date.

    Returns:
        Dict with ic_mean, ic_std, ir, ic_positive_ratio.
    """
    if len(ic_series) == 0:
        return {"ic_mean": 0.0, "ic_std": 0.0, "ir": 0.0, "ic_positive_ratio": 0.0}

    ic_mean = float(ic_series.mean())
    ic_std = float(ic_series.std(ddof=1)) if len(ic_series) > 1 else 0.0
    ir = ic_mean / ic_std if ic_std != 0 else 0.0
    ic_positive_ratio = float((ic_series > 0).sum() / len(ic_series))

    return {
        "ic_mean": ic_mean,
        "ic_std": ic_std,
        "ir": ir,
        "ic_positive_ratio": ic_positive_ratio,
    }


def compute_quantile_group_equity(
    factor_df: pd.DataFrame,
    return_df: pd.DataFrame,
    n_groups: int = 5,
) -> pd.DataFrame:
    """Compute equal-weight return for each quantile group per date.

    For each date: sort codes by factor value, split into n_groups buckets,
    compute equal-weight average return for each bucket.

    Args:
        factor_df: DataFrame indexed by date, columns = codes, values = factor exposures.
        return_df: DataFrame indexed by date, columns = codes, values = forward returns.
        n_groups: Number of quantile groups.

    Returns:
        DataFrame indexed by date, columns = group names (e.g. "group_1" ... "group_N").
        Values are equal-weight portfolio returns for that group.
    """
    common_dates = factor_df.index.intersection(return_df.index)
    common_codes = factor_df.columns.intersection(return_df.columns)

    result_rows = []
    for dt in common_dates:
        factor_row = factor_df.loc[dt, common_codes]
        return_row = return_df.loc[dt, common_codes]

        mask = factor_row.notna() & return_row.notna()
        valid_codes = common_codes[mask.values]

        if len(valid_codes) < n_groups:
            continue

        f_vals = factor_row[valid_codes]
        r_vals = return_row[valid_codes]

        try:
            groups = pd.qcut(f_vals, q=n_groups, labels=False, duplicates="drop")
        except ValueError:
            continue

        group_returns: list[float] = []
        for g in range(n_groups):
            g_mask = groups == g
            if g_mask.sum() > 0:
                group_returns.append(float(r_vals[g_mask].mean()))
            else:
                group_returns.append(np.nan)

        result_rows.append({"date": dt, ** {f"group_{g+1}": group_returns[g] for g in range(n_groups)}})

    if not result_rows:
        return pd.DataFrame(columns=[f"group_{g+1}" for g in range(n_groups)])

    result_df = pd.DataFrame(result_rows).set_index("date")
    return result_df


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


class FactorAnalysisTool(OperationHandler):
    name = "factor_analysis"
    description = (
        "Run IC/IR factor analysis and quantile backtest. "
        "Reads factor CSV and return CSV, outputs ic_series.csv, ic_summary.json, "
        "group_equity.csv to output_dir."
    )
    category = "analysis"
    parameters = {
        "type": "object",
        "properties": {
            "factor_csv": {
                "type": "string",
                "description": "Path to factor value CSV (index=date, columns=codes)",
            },
            "return_csv": {
                "type": "string",
                "description": "Path to return CSV (index=date, columns=codes, forward returns)",
            },
            "output_dir": {
                "type": "string",
                "description": "Output directory",
                "default": "~/.doyoutrade/assistant/artifacts/factor_output/",
            },
            "n_groups": {
                "type": "integer",
                "description": "Number of quantile groups",
                "default": 5,
            },
        },
        "required": ["factor_csv", "return_csv"],
    }

    async def execute(
        self,
        factor_csv: str,
        return_csv: str,
        output_dir: str | None = None,
        n_groups: int = 5,
    ) -> ToolResult:
        # Validate input files
        factor_path = _Path(factor_csv)
        if not factor_path.exists():
            return ToolResult(
                text=format_error_text("factor_csv_not_found", f"factor_csv not found: {factor_csv}"),
                is_error=True,
            )

        return_path = _Path(return_csv)
        if not return_path.exists():
            return ToolResult(
                text=format_error_text("return_csv_not_found", f"return_csv not found: {return_csv}"),
                is_error=True,
            )

        # Resolve output directory
        if output_dir is None:
            output_dir = str(_get_artifacts_root() / "factor_output")
        out_dir = _Path(output_dir).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)

        try:
            factor_df = pd.read_csv(factor_path, index_col=0, parse_dates=True)
        except Exception as exc:
            return ToolResult(
                text=format_error_text("factor_csv_read_failed", f"Failed to read factor_csv: {exc}"),
                is_error=True,
            )

        try:
            return_df = pd.read_csv(return_path, index_col=0, parse_dates=True)
        except Exception as exc:
            return ToolResult(
                text=format_error_text("return_csv_read_failed", f"Failed to read return_csv: {exc}"),
                is_error=True,
            )

        # Compute IC series
        ic_series = compute_ic_series(factor_df, return_df)

        # Compute IC summary
        ic_summary = compute_ic_summary(ic_series)

        # Compute quantile group equity
        group_equity = compute_quantile_group_equity(factor_df, return_df, n_groups=n_groups)

        # Save outputs
        ic_series_path = out_dir / "ic_series.csv"
        ic_summary_path = out_dir / "ic_summary.json"
        group_equity_path = out_dir / "group_equity.csv"

        ic_series.to_csv(ic_series_path, header=["ic"], index=True, index_label="date")

        with open(ic_summary_path, "w") as f:
            _json.dump(ic_summary, f, ensure_ascii=False)

        group_equity.to_csv(group_equity_path, index=True, index_label="date")

        ic_mean = ic_summary.get("ic_mean") if isinstance(ic_summary, dict) else None
        header = (
            f"Factor analysis complete (n_groups={n_groups}). "
            f"IC mean={ic_mean}. Outputs: {ic_series_path}, {ic_summary_path}, {group_equity_path}."
        )
        payload = {
            "status": "ok",
            "factor_csv": str(factor_path),
            "return_csv": str(return_path),
            "n_groups": n_groups,
            "ic_summary": ic_summary,
            "output_files": [
                str(ic_series_path),
                str(ic_summary_path),
                str(group_equity_path),
            ],
        }
        return ToolResult(text=append_json_payload(header, payload))
