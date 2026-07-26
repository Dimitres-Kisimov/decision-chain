"""Stage 0 — ingest: raw Online Retail II -> CleanedTransactions, WeeklyDemand, InvoiceStream.

The load + cleaning pipeline is adapted from my retail-analytics-real repo
(retail/ingest.py and retail/clean.py) STEP FOR STEP, on purpose: identity
check (a) in chain/reconcile.py asserts that this pipeline reproduces that
repo's published cleaned revenue to the penny. Same pipeline, same raw file,
same number — that is the cross-repo consistency proof, so any adaptation
here must not change a single decision.

Cleaning decisions (documented in full in retail-analytics-real):
  1. drop exact duplicate rows;
  2. separate cancellation invoices (C*) into a returns frame;
  3. remove non-product StockCodes (explicit bookkeeping list + gift_ prefix);
  4. drop zero/negative-price rows;
  5. drop residual non-positive quantities;
  6. flag (never drop) missing CustomerID.

Stage-0 specific choices (this repo):
  * Tracked SKUs = top ``N_TOP_SKUS`` (200) product codes by cleaned gross
    revenue. Rationale: the decision chain (inventory, picking, transport)
    is only meaningful for SKUs with recurring demand; the top 200 by revenue
    cover a large, documented share of revenue while keeping every later
    stage inspectable. The exact coverage share is printed in the report,
    not guessed here.
  * Weeks are W-SUN periods; the first and last calendar weeks of the
    dataset are partial and are dropped from the weekly index (same
    convention as retail-analytics-real's weekly revenue).
  * Weekly demand is zero-filled from each SKU's first selling week.

Everything is deterministic given the raw file: stable sorts, no sampling.
"""

from __future__ import annotations

import pandas as pd

from chain import paths
from chain.contracts import CleanedTransactions, CleanStep, InvoiceStream, WeeklyDemand

# adapted from my retail-analytics-real repo (retail/ingest.py)
RENAME = {"Customer ID": "CustomerID"}
COLUMNS = [
    "Invoice",
    "StockCode",
    "Description",
    "Quantity",
    "InvoiceDate",
    "Price",
    "CustomerID",
    "Country",
]

# adapted from my retail-analytics-real repo (retail/clean.py):
# bookkeeping StockCodes that are not merchandise — kept explicit on purpose.
NON_PRODUCT_CODES = {
    "POST",          # postage
    "DOT",           # dotcom postage
    "C2",            # carriage
    "D",             # discount
    "M", "m",        # manual entry
    "S",             # samples
    "B",             # adjust bad debt
    "BANK CHARGES",  # bank charges
    "AMAZONFEE",     # Amazon fees
    "ADJUST", "ADJUST2",  # manual adjustments
    "CRUK",          # charity commission
    "TEST001", "TEST002",  # literal test rows left in the export
    "PADS",          # placeholder at GBP 0.001
}
GIFT_VOUCHER_PREFIX = "gift_"

DEDUP_COLUMNS = [
    "Invoice",
    "StockCode",
    "Description",
    "Quantity",
    "InvoiceDate",
    "Price",
    "CustomerID",
    "Country",
]

N_TOP_SKUS = 200


# --------------------------------------------------------------------------- #
# Load  (adapted from my retail-analytics-real repo, retail/ingest.py)
# --------------------------------------------------------------------------- #
def _type_cast(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns=RENAME)
    return pd.DataFrame(
        {
            "Invoice": df["Invoice"].astype(str),
            "StockCode": df["StockCode"].astype(str),
            "Description": df["Description"].astype(str).where(df["Description"].notna()),
            "Quantity": pd.to_numeric(df["Quantity"], errors="coerce").astype("int64"),
            "InvoiceDate": pd.to_datetime(df["InvoiceDate"], errors="coerce"),
            "Price": pd.to_numeric(df["Price"], errors="coerce").astype("float64"),
            "CustomerID": pd.to_numeric(df["CustomerID"], errors="coerce").astype("Int64"),
            "Country": df["Country"].astype(str),
        }
    )


def load_raw(force: bool = False, use_cache: bool = True) -> pd.DataFrame:
    """Combined 2009-2011 frame from the raw workbook, cached to data/interim/."""
    if use_cache and not force:
        if paths.INTERIM_PARQUET.exists():
            return pd.read_parquet(paths.INTERIM_PARQUET)
        if paths.INTERIM_CSV.exists():
            df = pd.read_csv(paths.INTERIM_CSV, encoding="utf-8")
            df["InvoiceDate"] = pd.to_datetime(df["InvoiceDate"])
            df["CustomerID"] = df["CustomerID"].astype("Int64")
            return df

    raw = paths.find_raw_xlsx()
    if raw is None:
        raise FileNotFoundError(
            f"raw workbook not found at {paths.RAW_XLSX} or {paths.SIBLING_RAW_XLSX}. "
            "Run `python scripts/download_data.py` (the raw xlsx is deliberately not committed)."
        )

    sheets = pd.read_excel(raw, sheet_name=None, engine="openpyxl")
    frames = []
    for name, sheet in sheets.items():
        cast = _type_cast(sheet)
        cast["SourceSheet"] = name
        frames.append(cast)
    df = pd.concat(frames, ignore_index=True)
    df = df.sort_values("InvoiceDate", kind="stable").reset_index(drop=True)

    if use_cache:
        _write_cache(df)
    return df


def _write_cache(df: pd.DataFrame) -> None:
    paths.DATA_INTERIM.mkdir(parents=True, exist_ok=True)
    try:
        df.to_parquet(paths.INTERIM_PARQUET, index=False)
    except (ImportError, ValueError):
        df.to_csv(paths.INTERIM_CSV, index=False, encoding="utf-8", compression="gzip")


def load_fixture() -> pd.DataFrame:
    """The committed ~2k-row REAL fixture, typed like the full frame (CI path)."""
    df = pd.read_csv(paths.FIXTURE_CSV, encoding="utf-8")
    df["Invoice"] = df["Invoice"].astype(str)
    df["StockCode"] = df["StockCode"].astype(str)
    df["InvoiceDate"] = pd.to_datetime(df["InvoiceDate"])
    df["CustomerID"] = df["CustomerID"].astype("Int64")
    df["Quantity"] = df["Quantity"].astype("int64")
    df["Price"] = df["Price"].astype("float64")
    return df


# --------------------------------------------------------------------------- #
# Clean  (adapted from my retail-analytics-real repo, retail/clean.py)
# --------------------------------------------------------------------------- #
def is_non_product(stock_code: pd.Series) -> pd.Series:
    """True for bookkeeping codes (explicit list) and gift-voucher codes (prefix)."""
    s = stock_code.astype(str)
    return s.isin(NON_PRODUCT_CODES) | s.str.lower().str.startswith(GIFT_VOUCHER_PREFIX)


def _log(steps: list[CleanStep], step: str, rows_in: int, rows_out: int, note: str) -> None:
    removed = rows_in - rows_out
    steps.append(
        CleanStep(
            step=step,
            rows_in=rows_in,
            rows_out=rows_out,
            rows_removed=removed,
            pct_removed=(removed / rows_in) if rows_in else 0.0,
            note=note,
        )
    )


def _add_derived_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["Revenue"] = df["Quantity"].astype("float64") * df["Price"]
    df["Date"] = df["InvoiceDate"].dt.normalize()
    iso = df["InvoiceDate"].dt.isocalendar()
    df["ISOYear"] = iso["year"].astype("int64")
    df["ISOWeek"] = iso["week"].astype("int64")
    return df


def clean(df: pd.DataFrame) -> CleanedTransactions:
    """Run the documented pipeline; returns the stage-0 CleanedTransactions contract."""
    steps: list[CleanStep] = []

    n0 = len(df)
    df = df.drop_duplicates(subset=DEDUP_COLUMNS).copy()
    _log(steps, "drop exact duplicates", n0, len(df), "identical invoice/code/qty/time/price/customer")

    n1 = len(df)
    is_cancel = df["Invoice"].str.startswith("C").fillna(False)
    returns = df.loc[is_cancel].copy()
    df = df.loc[~is_cancel].copy()
    _log(steps, "separate cancellations (C*)", n1, len(df), "kept as returns frame, excluded from sales")

    n2 = len(df)
    df = df.loc[~is_non_product(df["StockCode"])].copy()
    _log(steps, "remove non-product StockCodes", n2, len(df), "POST/D/M/DOT/BANK CHARGES/ADJUST/TEST/gift_ etc.")

    n3 = len(df)
    df = df.loc[df["Price"] > 0].copy()
    _log(steps, "drop zero/negative-price rows", n3, len(df), "no revenue information; mostly stock notes")

    n4 = len(df)
    df = df.loc[df["Quantity"] > 0].copy()
    _log(steps, "drop non-cancellation qty <= 0", n4, len(df), "stock adjustments outside C-invoices")

    n5 = len(df)
    df["KnownCustomer"] = df["CustomerID"].notna()
    flagged = int((~df["KnownCustomer"]).sum())
    _log(steps, "flag missing CustomerID", n5, len(df), f"{flagged:,} rows flagged, kept for revenue")

    df = _add_derived_columns(df)

    returns = returns.loc[~is_non_product(returns["StockCode"])].copy()
    returns = _add_derived_columns(returns)

    return CleanedTransactions(
        sales=df.reset_index(drop=True),
        returns=returns.reset_index(drop=True),
        steps=steps,
    )


# --------------------------------------------------------------------------- #
# Stage-0 outputs: tracked SKUs, weekly demand, invoice stream
# --------------------------------------------------------------------------- #
def tracked_skus(cleaned: CleanedTransactions, n: int = N_TOP_SKUS) -> list[str]:
    """Top ``n`` StockCodes by cleaned gross revenue (deterministic tie-break by code)."""
    revenue = (
        cleaned.sales.groupby("StockCode", observed=True)["Revenue"]
        .sum()
        .sort_values(ascending=False, kind="stable")
    )
    ranked = revenue.reset_index().sort_values(
        ["Revenue", "StockCode"], ascending=[False, True], kind="stable"
    )
    return ranked["StockCode"].head(n).tolist()


def weekly_index(cleaned: CleanedTransactions) -> pd.DatetimeIndex:
    """W-SUN week-end index over the dataset, partial first/last weeks dropped."""
    weekly = cleaned.sales.set_index("InvoiceDate")["Revenue"].resample("W-SUN").sum()
    return weekly.index[1:-1]


def weekly_demand(cleaned: CleanedTransactions, n_skus: int = N_TOP_SKUS) -> WeeklyDemand:
    """Weekly unit demand for the tracked SKUs, zero-filled from first selling week.

    IMPORTANT for identity (b): rows in the two dropped partial edge weeks are
    excluded from BOTH the weekly aggregation and the line-level comparison,
    so the sum over ``demand.Units`` equals the sum of line quantities on the
    same week window exactly.
    """
    skus = tracked_skus(cleaned, n_skus)
    weeks = weekly_index(cleaned)
    lines = sales_in_week_window(cleaned, skus, weeks)

    grouped = (
        lines.set_index("InvoiceDate")
        .groupby("StockCode", observed=True)["Quantity"]
        .resample("W-SUN")
        .sum()
    )
    rows = []
    for sku in skus:
        if sku in grouped.index.get_level_values(0):
            series = grouped.loc[sku].reindex(weeks, fill_value=0).astype(float)
        else:  # tracked SKU whose sales all fall in the dropped edge weeks
            series = pd.Series(0.0, index=weeks)
        nonzero = series.ne(0)
        if nonzero.any():
            series = series.loc[nonzero.idxmax():]
        for week, units in series.items():
            rows.append({"StockCode": sku, "Week": week, "Units": float(units)})
    demand = pd.DataFrame(rows, columns=["StockCode", "Week", "Units"])
    return WeeklyDemand(demand=demand, skus=skus, weeks=weeks)


def sales_in_week_window(
    cleaned: CleanedTransactions, skus: list[str], weeks: pd.DatetimeIndex
) -> pd.DataFrame:
    """Cleaned sales lines for ``skus`` whose W-SUN week lies inside ``weeks``."""
    sales = cleaned.sales
    mask = sales["StockCode"].isin(skus)
    week_end = sales["InvoiceDate"].dt.to_period("W-SUN").dt.end_time.dt.normalize()
    window = (week_end >= weeks[0].normalize()) & (week_end <= weeks[-1].normalize())
    return sales.loc[mask & window].copy()


def invoice_stream(cleaned: CleanedTransactions, demand: WeeklyDemand) -> InvoiceStream:
    """Invoice/line stream for the tracked SKUs on the same week window as demand."""
    lines = sales_in_week_window(cleaned, demand.skus, demand.weeks)
    week_end = lines["InvoiceDate"].dt.to_period("W-SUN").dt.end_time.dt.normalize()
    out = lines[["Invoice", "StockCode", "Quantity", "InvoiceDate"]].copy()
    out["Week"] = week_end
    return InvoiceStream(lines=out.reset_index(drop=True))


def run(raw: pd.DataFrame, n_skus: int = N_TOP_SKUS) -> tuple[CleanedTransactions, WeeklyDemand, InvoiceStream]:
    """Stage 0 end-to-end: raw frame -> the three stage-0 contract objects."""
    cleaned = clean(raw)
    demand = weekly_demand(cleaned, n_skus)
    stream = invoice_stream(cleaned, demand)
    return cleaned, demand, stream
