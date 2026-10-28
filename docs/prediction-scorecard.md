# Prediction Scorecard

The scorecard answers one question: **were the AI's past calls actually any good?**
It grades every saved AI suggestion against what prices did afterwards and pushes a
short report through your normal notification channels. It makes no LLM calls.

It builds on the existing [DecisionSignal](decision-signals.md) outcome engine: every
analysis already saves its suggestion (action, score, confidence, horizon) as a
`DecisionSignal`, and the outcome engine grades it `hit` / `miss` / `neutral`
against forward daily bars with a ±2% neutral band.

## What it reports

| Section | Meaning |
| --- | --- |
| Hit rate | Share of decided calls (hit + miss) where the price moved the predicted way by more than 2%. Moves within ±2% are `neutral` and excluded. |
| Beat the index | Share of calls whose choice did better than simply holding the market index over the same window, plus the average **edge** in percentage points. |
| By window | The same calls graded after 1, 3, 5 and 10 trading days. The headline uses 3 days, the default horizon of post-close analysis signals. |
| By stock / by action | Hit rate and benchmark comparison per stock and per action (`buy`, `hold`, `sell`, …) on the headline window. |
| Confidence check | Hit rate by the AI's own confidence label (high / medium / low) and by score band, with a verdict on whether higher confidence actually meant better calls. |

### How "beat the index" is decided

| Call | Implied choice | Edge |
| --- | --- | --- |
| `buy` / `add` (expects up) | own the stock | stock return − index return |
| `hold` (expects not down) | keep owning the stock | stock return − index return |
| `reduce` / `sell` / `avoid` (expects not up) | switch out of the stock | index return − stock return |

A positive edge means the call's choice did better than holding the index. The index
return is measured over the same number of the index's own sessions after the call.

Benchmarks per market: India `^NSEI` (Nifty 50), US `^GSPC` (S&P 500), Hong Kong
`^HSI`, Japan `^N225`, Korea `^KS11`, Taiwan `^TWII`, China A-shares `000300.SS`
(CSI 300). They are fetched from Yahoo Finance; if that fails, the benchmark lines
are omitted and the rest of the scorecard still renders.

### Confidence verdict

The verdict compares high-confidence calls with low-confidence calls (falling back to
medium when there are too few low-confidence calls). It needs at least 5 decided calls
per group and a gap of at least 5 percentage points before claiming either group did
better; otherwise it reports "no clear difference" or "not enough data yet".

## Running it

```bash
python main.py --scorecard                    # build, save to reports/, and push
python main.py --scorecard --no-notify        # build and save only
python main.py --scorecard --scorecard-days 30
```

Each run:

1. refreshes recent daily bars for stocks with calls in the lookback window, so
   outcomes can complete even for stocks no longer in `STOCK_LIST` (fail-open);
2. evaluates pending outcomes on all four windows;
3. builds the scorecard and saves `reports/prediction_scorecard_YYYYMMDD.md`;
4. pushes it unless `--no-notify` is set.

The report follows `REPORT_LANGUAGE` (`en` or `zh`).

## GitHub Actions

The daily workflow (`.github/workflows/00-daily-analysis.yml`):

- **keeps the database between runs**: `data/stock_analysis.db` is restored from the
  Actions cache before analysis and saved under a new key afterwards, on every run
  including market holidays. Without this, each run would start with an empty
  database and the scorecard would never have history. Caches unused for 7 days are
  evicted, so pausing the workflow for more than a week resets the history.
- **runs the scorecard weekly**, by default on Friday (India time). Set the repository
  variable `SCORECARD_WEEKDAY` to `1`–`7` (Monday = 1) to change the day, or `off` to
  disable the weekly push. `SCORECARD_DAYS` sets the lookback (default 90).
- adds a `scorecard` option to the manual **Run workflow** menu to run it on demand.
- is scheduled at UTC 10:45 (16:15 India time, 18:45 Beijing time), after the
  NSE/BSE close.

A scorecard failure never fails the day's analysis.

## Limits

- The first useful numbers appear about a week after the first run: a call needs its
  window to elapse before it can be graded.
- Hit rate on a handful of calls is noise. Treat the numbers as meaningful only once
  each group has dozens of decided calls.
- The scorecard grades direction and relative performance only. It does not model
  costs, taxes, position sizing, stop-losses or targets.
- Not investment advice.
