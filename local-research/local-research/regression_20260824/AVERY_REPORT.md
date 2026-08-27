# Avery PR 10780 — listed-option regression report (2026-08-24)

## Checkout
- Worktree: `/Users/luomengzhou/Projects/dev-put-harvesting` @ `1cd61ed21` (`luomeng/put-harvesting`)
- Baseline for non-opt-in: `/Users/luomengzhou/Projects/dev` @ `main`
- FT Aug 12 runner/CSVs intentionally **not** in the PR (gitignored)

## 1. Existing listed-option index (non-opt-in)

**Index used:** integration fixture `spy_3m_cc` (`CARTER_SPY_DEMO_SAMPLE`) — SPY 3m covered call through 2024-10-01. No `PUT_HARVEST` / `RESTRIKE_AT_MARKET` / `allow_annual_multi_tranche`.

**Also:** `test_early_exercise` (CSCO legacy early-exercise path).

### Commands
```bash
VENV=/Users/luomengzhou/Library/Caches/pypoetry/virtualenvs/merq-YPsgvTkp-py3.10
# poetry broken on branch after main merge (source.default=true); use venv python

cd /Users/luomengzhou/Projects/dev-put-harvesting/python
export PYTHONPATH="$PWD"; unset MERQ_ENV
"$VENV/bin/python" - <<'PY'
from tests.integration.indices.merqube.options.merq_option_strategies.test_merqube_option_index import (
    test_spy_3m_cc, test_early_exercise,
)
test_spy_3m_cc(); print("PASS spy_3m_cc")
test_early_exercise(); print("PASS early_exercise")
PY

# main vs branch dumps (same script, PYTHONPATH switched)
"$VENV/bin/python" /tmp/dump_spy_3m_cc.py …/regression_20260824/{main,branch}
```

### Results
| Check | Result |
|---|---|
| `test_spy_3m_cc` | **PASS** |
| `test_early_exercise` | **PASS** |
| main vs branch levels | **identical** (71 days, max abs diff 0.0, same SHA256) |
| main vs branch portfolios | **identical** (210 rows / 70 days, qty & post_corax diff 0.0) |

**Verdict:** PR does not change results for configs that do not opt into put harvesting.

### Optional OBUS0FM4
- Loaded prod history via CodeCommit; compared to SecAPI `price_return` over 2926 overlap days → **max abs diff 0.0**.
- Fresh `get_portfolios(2015-01-02→01-31)` after `load_index` failed with duplicate rebalance on base date (expected when history already loaded) — not a PR regression.

## 2. Put-harvest FT Aug 12 (level ↔ portfolio)

### Command
```bash
cd /Users/luomengzhou/Projects/dev-put-harvesting/python
export PYTHONPATH="$PWD"; unset MERQ_ENV
"$VENV/bin/python" merq/indices/merqube/options/strategies/ft_aug_12_backtest.py \
  --out ~/Projects/dev-put-harvesting/ft_aug_12_out/rerun_avery \
  --cache ~/.merq_backtest_cache
```

### Results
| Check | Result |
|---|---|
| Level ↔ portfolio reconcile | `max \|sum(market_value) - level\| = 9.09e-13` (runner); with post_corax: `1.36e-12` |
| vs prior Aug 21 baseline levels | overlap **3373** days, **max abs diff 0.0** |
| vs prior portfolio through 2026-08-12 | **23210/23210** rows identical (units/price/MV) |
| Extra days vs prior | 2026-08-13 … 2026-08-21 (calendar rolled forward; expected) |
| Harvest events | 2018-12-21, 2020-03-20, 2022-06-17 (unchanged) |

**CSVs not added to PR.**

## Blockers / notes
- `poetry run` fails on branch after merge: `tool.poetry.source[0] must not contain {'default'}` — used existing Poetry venv python + `PYTHONPATH` instead.
- Did not set `MERQ_ENV=integration`.
