"""Backtest runner for the FT_Aug_12 SPY collar spec.

Runs the spec straight out of the ``FT_Aug_12`` file -- no parameters are set here, so
editing that file is the only way to change the strategy.

Requires the annual-tranching guard in ``strategies/spec.py`` to be disabled: the put leg
uses ``number_of_tranches=4`` with a 12-month roll, which that guard rejects.

Do not set ``MERQ_ENV=integration``; it points at a database that is not reachable from a
laptop. Leaving it unset uses the production (read-only) instances.

Set ``PYTHONPATH`` to this checkout's ``python`` directory. The poetry venv is shared with the
main worktree via a ``.pth`` file, so without it ``import merq`` resolves to that other checkout
and this branch's modules go missing.

Usage::

    export PYTHONPATH=$(git rev-parse --show-toplevel)/python
    poetry run python merq/indices/merqube/options/strategies/ft_aug_12_backtest.py
    poetry run python .../ft_aug_12_backtest.py --no-harvest        # control arm
    poetry run python .../ft_aug_12_backtest.py --diag              # per-evaluation CSV
    poetry run python .../ft_aug_12_backtest.py --end 2016-12-30    # shorter window
"""

import argparse
import contextlib
import json
import logging
import traceback
from pathlib import Path
from typing import Any
from unittest import mock

import pandas as pd

from merq.indices.merqube.options.merq_option_strategies.early_rebalance.early_rebalance_condition.put_harvest_condition import (
    PutHarvestEarlyRebalance,
)
from merq.indices.merqube.options.merq_option_strategies.merqube_option_index import (
    OptionStrategiesIndex,
)
from merq.indices.merqube.options.strategies.backtest_cache import BacktestCache
from merq.shared_types import IndexConfig

logger = logging.getLogger(__name__)

SPEC_PATH = Path(__file__).parent / "FT_Aug_12"
TRADING_DAYS_PER_YEAR = 252

_HARVEST_LOGGERS = (
    "merq.indices.merqube.options.merq_option_strategies.early_rebalance."
    "early_rebalance_condition.put_harvest_condition",
    "merq.indices.merqube.options.merq_option_strategies.early_rebalance."
    "early_rebalance_treatment.restrike_treatment",
)


def get_as_of_date() -> pd.Timestamp:
    """Latest date the index can plausibly be computed for: the previous business day."""
    return pd.Timestamp.now().normalize() - pd.offsets.CustomBusinessDay()


def load_spec(disable_harvest: bool = False) -> dict[str, Any]:
    """Read the strategy spec out of the FT_Aug_12 file.

    :param disable_harvest: return the control-arm spec with put harvesting switched off
    """
    spec = json.loads(SPEC_PATH.read_text())["index_class_args"]["spec"]
    if disable_harvest:
        spec["early_rebalance_spec"] = {"trigger_early_exercise": False}
    return spec


def install_harvest_event_capture(events: list[str]) -> None:
    """Collect the INFO lines the harvest condition and treatment emit."""

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            message = record.getMessage()
            if "harvest" in message.lower() or "re-strik" in message.lower():
                events.append(message)

    for name in _HARVEST_LOGGERS:
        harvest_logger = logging.getLogger(name)
        harvest_logger.setLevel(logging.INFO)
        harvest_logger.addHandler(_Capture())


def install_evaluation_capture(rows: list[dict[str, Any]]) -> None:
    """Record every harvest evaluation: the inputs, the ratio, and the decision.

    This is the tool for tuning ``appreciation_multiple`` -- it shows how close each
    assessment came to firing, not just whether it did.
    """
    original = PutHarvestEarlyRebalance._should_harvest  # pylint: disable=protected-access

    def _should_harvest(self, merq_option_id, position, spot, date, pricer, tranche_spec, cbd) -> bool:
        entry_premium = self._get_entry_premium(  # pylint: disable=protected-access
            merq_option_id=merq_option_id, position=position, pricer=pricer, tranche_spec=tranche_spec, cbd=cbd
        )
        try:
            current_premium = pricer.load_prices_for_merq_ids(merq_ids=[merq_option_id], price_date=date)[
                merq_option_id
            ].get_close_adjusted_price()
        except Exception:  # noqa: BLE001  # pylint: disable=broad-except
            current_premium = None

        harvested = original(
            self,
            merq_option_id=merq_option_id,
            position=position,
            spot=spot,
            date=date,
            pricer=pricer,
            tranche_spec=tranche_spec,
            cbd=cbd,
        )
        usable = isinstance(entry_premium, float) and isinstance(current_premium, float) and entry_premium > 0
        rows.append(
            {
                "date": date.date(),
                "tranche": tranche_spec.uuid,
                "strike": merq_option_id.strike,
                "expiry": merq_option_id.expiry.date(),
                "spot": round(float(spot), 2),
                "days_to_expiry": (merq_option_id.expiry - date).days,
                "itm": bool(spot < merq_option_id.strike),
                "entry_premium": round(entry_premium, 4) if isinstance(entry_premium, float) else None,
                "current_premium": round(current_premium, 4) if isinstance(current_premium, float) else None,
                "multiple": round(current_premium / entry_premium, 3) if usable else None,
                "entry_date_stamped": position.entry_date.date() if position.entry_date is not None else None,
                "harvested": harvested,
            }
        )
        return harvested

    PutHarvestEarlyRebalance._should_harvest = _should_harvest  # pylint: disable=protected-access


def daily_portfolio_frame(index: OptionStrategiesIndex) -> pd.DataFrame:
    """Flatten every close portfolio to one row per position per day.

    The close portfolio is the one the level is computed from, and valuation goes through the
    index's own level computer rather than a re-implementation, so ``market_value`` summed over
    a date (plus that day's ``post_corax_adjustment``) reproduces the published level.
    """
    level_computer = index.get_level_computer()
    core_data = index.get_core_data()
    rows: list[dict[str, Any]] = []

    for date, portfolio in sorted(core_data.portfolios.items()):
        daily_prices = core_data.prices.get(date)
        if daily_prices is None:  # base date carries no prices
            continue
        prices = daily_prices.close_prices
        for merq_id, position in portfolio.positions.items():
            price = prices.get(merq_id)
            rows.append(
                {
                    "date": date.date(),
                    "merq_id": str(merq_id),
                    "position_type": position.position_type.value,
                    "strategy_id": position.strategy_id,
                    "strike": getattr(merq_id, "strike", None),
                    "expiry": expiry.date() if (expiry := getattr(merq_id, "expiry", None)) is not None else None,
                    "units": position.quantity,
                    "price": price.get_close_adjusted_price() if price is not None else None,
                    "market_value": level_computer.compute_value_for_position(date, position, prices),
                    "currency": getattr(position, "currency", None),
                    "post_corax_adjustment": portfolio.post_corax_adjustment,
                }
            )

    return pd.DataFrame(rows)


def reconcile_to_levels(portfolios: pd.DataFrame, levels: pd.DataFrame) -> float:
    """Largest absolute gap between the summed position values and the published level."""
    by_date = portfolios.groupby("date").agg(
        value=("market_value", "sum"), adjustment=("post_corax_adjustment", "first")
    )
    rebuilt = by_date["value"] + by_date["adjustment"]
    published = pd.Series(levels["level"].to_numpy(), index=[ts.date() for ts in levels.index])
    return float((rebuilt - published.reindex(rebuilt.index)).abs().max())


def summarise(levels: pd.DataFrame) -> dict[str, Any]:
    """Headline metrics for a computed level series."""
    level = levels["level"]
    total_growth = float(level.iloc[-1]) / float(level.iloc[0])
    return {
        "days": len(level),
        "first_date": levels.index[0],
        "last_date": levels.index[-1],
        "first_level": round(float(level.iloc[0]), 2),
        "final_level": round(float(level.iloc[-1]), 2),
        "annualised_return": total_growth ** (TRADING_DAYS_PER_YEAR / len(level)) - 1,
        "max_drawdown": float((level / level.cummax() - 1).min()),
    }


def run(
    start_date: pd.Timestamp | None,
    end_date: pd.Timestamp,
    disable_harvest: bool,
    diagnostics: bool,
    output_dir: Path,
    cache_dir: Path | None = None,
) -> None:
    """Compute the index and write levels and daily portfolios (and optionally the evaluation log) to CSV."""
    events: list[str] = []
    evaluations: list[dict[str, Any]] = []

    install_harvest_event_capture(events)
    if diagnostics:
        install_evaluation_capture(evaluations)

    spec = load_spec(disable_harvest=disable_harvest)
    index = OptionStrategiesIndex(spec=spec, index_config=IndexConfig(), svc_injector=mock.MagicMock())

    suffix = "no_harvest" if disable_harvest else "harvest"
    cache = BacktestCache(cache_dir) if cache_dir else contextlib.nullcontext()
    try:
        with cache:
            index.get_portfolios(start_date=start_date, end_date=end_date)
        status = "COMPLETE"
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-except
        # Keep whatever was computed before the failure -- the last good date localises the problem.
        status = f"STOPPED {type(exc).__name__}: {exc}"
        traceback.print_exc()
    if cache_dir:
        print(f"cache            {cache.stats()}")

    levels = index.get_returns_as_dataframe()
    output_dir.mkdir(parents=True, exist_ok=True)
    levels_path = output_dir / f"ft_aug_12_levels_{suffix}.csv"
    levels.to_csv(levels_path)

    print(f"status           {status}")
    if levels.empty:
        print("no levels computed")
        return

    stats = summarise(levels)
    print(f"window           {stats['first_date']:%Y-%m-%d} -> {stats['last_date']:%Y-%m-%d} ({stats['days']} days)")
    print(f"level            {stats['first_level']} -> {stats['final_level']}")
    print(f"annualised       {stats['annualised_return']:.2%}")
    print(f"max drawdown     {stats['max_drawdown']:.2%}")
    if not status.startswith("COMPLETE"):
        print(
            "note             annualised/max drawdown are on the truncated window ending at last_date; "
            "do not treat them as the full-sample strategy return"
        )
    print(f"levels written   {levels_path}")

    portfolios = daily_portfolio_frame(index)
    if not portfolios.empty:
        portfolios_path = output_dir / f"ft_aug_12_portfolio_{suffix}.csv"
        portfolios.to_csv(portfolios_path, index=False)
        print(f"portfolio rows   {len(portfolios)} over {portfolios['date'].nunique()} days")
        print(f"reconciliation   max |sum(market_value) - level| = {reconcile_to_levels(portfolios, levels):.2e}")
        print(f"portfolio written {portfolios_path}")

    if diagnostics and evaluations:
        evaluation_frame = pd.DataFrame(evaluations)
        diagnostics_path = output_dir / f"ft_aug_12_evaluations_{suffix}.csv"
        evaluation_frame.to_csv(diagnostics_path, index=False)
        harvested = int(evaluation_frame["harvested"].sum())
        print(f"evaluations      {len(evaluation_frame)} (in the money: {int(evaluation_frame['itm'].sum())})")
        print(f"harvests         {harvested}")
        print(f"peak multiple    {evaluation_frame['multiple'].max()}")
        print(f"diagnostics      {diagnostics_path}")

    for event in events:
        print(f"event            {event}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--start",
        type=pd.Timestamp,
        default=None,
        help="First calculation date; defaults to the spec's base_date",
    )
    parser.add_argument(
        "--end",
        type=pd.Timestamp,
        default=None,
        help="Last calculation date; defaults to the previous business day",
    )
    parser.add_argument(
        "--no-harvest",
        action="store_true",
        help="Control arm: run the identical spec with put harvesting switched off",
    )
    parser.add_argument(
        "--diag",
        action="store_true",
        help="Log every harvest evaluation to CSV -- use this when tuning appreciation_multiple",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=None,
        help="Directory for a disk cache of market data reads; re-runs over the same dates become "
        "network-free and finish in a fraction of the time",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path.cwd(),
        help="Directory for the CSV output (default: current directory)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(
        start_date=args.start,
        end_date=args.end if args.end is not None else get_as_of_date(),
        disable_harvest=args.no_harvest,
        diagnostics=args.diag,
        output_dir=args.out,
        cache_dir=args.cache,
    )
