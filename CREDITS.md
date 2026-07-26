# Credits

## Data

- **Chen, D. (2019). Online Retail II [Dataset]. UCI Machine Learning Repository.**
  https://doi.org/10.24432/C5CG6D — https://archive.ics.uci.edu/dataset/502/online+retail+ii
  Licensed CC BY 4.0. Real transactions of a UK-based online giftware retailer,
  December 2009 - December 2011. The dataset is read from disk at build time
  (sibling `retail-analytics-real` checkout or `scripts/download_data.py`) and is
  not redistributed here; the small committed test fixture is real rows sampled
  from it under CC BY 4.0.

## Adapted from my own repositories

- **retail-analytics-real** — the documented cleaning pipeline (`chain/ingest.py`
  adapts `retail/ingest.py` + `retail/clean.py` step for step: reproducing that
  repo's published revenue number to the penny is identity check (a)), the
  lag-linear regression model, the rolling-origin CV design, the download-script
  and fixture-testing approach.
- **ml-models-lab** — the MASE convention (in-sample naive-walk scaling) and the
  honest-evaluation reporting style.

## Methods

- **MASE**: Hyndman, R.J. & Koehler, A.B. (2006), "Another look at measures of
  forecast accuracy"; Hyndman, R.J. & Athanasopoulos, G., *Forecasting:
  Principles and Practice*.
- **Croston's method**: Croston, J.D. (1972), "Forecasting and stock control for
  intermittent demands". **SBA correction**: Syntetos, A.A. & Boylan, J.E. (2005),
  "The accuracy of intermittent demand estimates".
- **Demand classification quadrants** (ADI 1.32 / CV^2 0.49 cutoffs):
  Syntetos, Boylan & Croston (2005).

## Tools

- Python, numpy, scipy, pandas, matplotlib, openpyxl — the entire stack.
  No ML libraries; every model is implemented in this repository.
- pytest and ruff for the quality gates; GitHub Actions for CI.

## Authorship

Design, code and documentation: Dimitres Kisimov.
