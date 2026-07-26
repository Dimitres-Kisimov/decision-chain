"""Regenerate tests/fixtures/sample.csv + expected.json from the real data, deterministically.

Fixture-building approach adapted from my retail-analytics-real repo
(scripts/make_fixture.py), extended for the decision chain: besides the
stratified mess (cancellations, non-product codes, zero prices, duplicate
groups), the fixture must contain a few SKUs with DENSE weekly history so the
forecasting stage has real rolling-origin folds to work with. For that, up to
FOCUS_ROWS rows are sampled from each of the FOCUS_SKUS busiest product codes.

Everything is seeded (SEED = 42) and drawn from the REAL dataset — no
synthetic rows. expected.json records, at generation time, the values the
fixture-mode identity checks verify against from then on (the full-data
revenue constant is only reachable with the real dataset on disk):

    cleaned_revenue_gbp   fixture revenue through the full cleaning pipeline
    sha256                fixture file hash (regeneration determinism check)

Run only when the fixture needs refreshing (requires the raw data):

    python scripts/make_fixture.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from chain import ingest, paths  # noqa: E402

SEED = 42
FOCUS_SKUS = 10     # busiest product codes by line count
FOCUS_ROWS = 300    # rows sampled per focus SKU
COLUMNS = [
    "Invoice", "StockCode", "Description", "Quantity", "InvoiceDate", "Price", "CustomerID", "Country",
]


def build_fixture(df: pd.DataFrame) -> pd.DataFrame:
    df = df[COLUMNS]
    product = df.loc[~ingest.is_non_product(df["StockCode"])]

    line_counts = (
        product.loc[~product["Invoice"].str.startswith("C")]
        .groupby("StockCode", observed=True)
        .size()
        .sort_values(ascending=False, kind="stable")
    )
    focus = line_counts.head(FOCUS_SKUS).index.tolist()
    focus_rows = pd.concat(
        [
            product.loc[product["StockCode"] == sku].sample(
                n=min(FOCUS_ROWS, int((product["StockCode"] == sku).sum())),
                random_state=SEED,
            )
            for sku in focus
        ]
    )

    random_rows = df.sample(n=1200, random_state=SEED)
    cancellations = df.loc[df["Invoice"].str.startswith("C")].sample(n=120, random_state=SEED)
    non_product = df.loc[ingest.is_non_product(df["StockCode"])].sample(n=80, random_state=SEED)
    zero_price = df.loc[df["Price"] <= 0].sample(n=50, random_state=SEED)

    dup_mask = df.duplicated(subset=ingest.DEDUP_COLUMNS, keep=False)
    dup_keys = (
        df.loc[dup_mask, ingest.DEDUP_COLUMNS].drop_duplicates().sample(n=15, random_state=SEED)
    )
    dup_rows = df.loc[dup_mask].merge(dup_keys, on=ingest.DEDUP_COLUMNS, how="inner")

    fixture = pd.concat([focus_rows, random_rows, cancellations, non_product, zero_price, dup_rows])
    return fixture.sample(frac=1.0, random_state=SEED).reset_index(drop=True)


def main() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    fixture = build_fixture(ingest.load_raw())
    paths.FIXTURE_CSV.parent.mkdir(parents=True, exist_ok=True)
    fixture.to_csv(paths.FIXTURE_CSV, index=False, encoding="utf-8", lineterminator="\n")

    # expected values, computed through the SAME pipeline the checks will run
    cleaned = ingest.clean(ingest.load_fixture())
    sha = hashlib.sha256(paths.FIXTURE_CSV.read_bytes()).hexdigest()
    expected = {
        "rows": int(len(fixture)),
        "cleaned_revenue_gbp": round(float(cleaned.sales["Revenue"].sum()), 2),
        "cleaned_sales_rows": int(len(cleaned.sales)),
        "returns_rows": int(len(cleaned.returns)),
        "sha256": sha,
        "seed": SEED,
    }
    paths.FIXTURE_EXPECTED.write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")

    print(f"[OK] wrote {paths.FIXTURE_CSV} ({len(fixture):,} rows)")
    print(f"     cancellations: {int(fixture['Invoice'].str.startswith('C').sum())}")
    print(f"     non-product:   {int(ingest.is_non_product(fixture['StockCode']).sum())}")
    print(f"     price <= 0:    {int((fixture['Price'] <= 0).sum())}")
    print(f"     missing ID:    {int(fixture['CustomerID'].isna().sum())}")
    print(f"     exact dupes:   {int(fixture.duplicated(subset=ingest.DEDUP_COLUMNS).sum())}")
    print(f"     cleaned rev:   GBP {expected['cleaned_revenue_gbp']:,.2f}")
    print(f"     sha256:        {sha[:16]}...")


if __name__ == "__main__":
    main()
