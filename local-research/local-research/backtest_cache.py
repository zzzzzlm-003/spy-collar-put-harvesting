"""Disk-backed cache of market data reads, for iterating on a backtest locally.

Historical prices and contract listings do not change, but the engine re-reads them from
the database on every run: roughly 17 queries per calculation date, each a ~38ms round trip
over the VPN. Wall-clock is therefore dominated by network latency, not computation -- a
ten-year backtest spends over 90% of its time blocked on I/O.

Caching by *individual contract and date* rather than by query means the cache survives
changes to the strategy: altering the harvest rule changes which contracts are held, but
the prices of the contracts already seen are still hits.

This is a local development accelerator. It is installed explicitly by a runner script and
is not part of any production path.

Usage::

    from merq.indices.merqube.options.strategies.backtest_cache import BacktestCache

    with BacktestCache(Path("~/.merq_backtest_cache").expanduser()) as cache:
        index.get_portfolios(...)
        print(cache.stats())
"""

import copy
import logging
import pickle
from pathlib import Path
from types import TracebackType
from typing import Any, Iterable

import pandas as pd

from merq.merq_platform.api.components.computation_pipeline.corax_loaders.equities_corporate_actions_loader import (
    EquitiesCorporateActionsLoader,
)
from merq.merq_platform.api.components.computation_pipeline.corax_loaders.ivol_corporate_actions_loader import (
    IvolCorporateActionsLoader,
)
from merq.merq_platform.api.components.pricers.standard_index_pricer import (
    StandardIndexPricer,
)
from merq.merq_platform.impl.services.options.ivol_reference_data_provider import (
    IvolReferenceDataProvider,
)

logger = logging.getLogger(__name__)

PRICES_FILE = "prices.pkl"
CONTRACTS_FILE = "contracts.pkl"
CORAX_FILE = "corax.pkl"

_CORAX_LOADERS = (EquitiesCorporateActionsLoader, IvolCorporateActionsLoader)


class BacktestCache:
    """Memoises option/equity prices and contract listings to disk across runs."""

    def __init__(self, cache_dir: Path, read_only: bool = False):
        """
        :param cache_dir: directory holding the pickled cache files
        :param read_only: load the cache but do not write new entries back
        """
        self.cache_dir = cache_dir
        self.read_only = read_only
        self._prices: dict[tuple[str, pd.Timestamp], Any] = {}
        self._contracts: dict[tuple, Any] = {}
        self._price_hits = 0
        self._price_misses = 0
        self._contract_hits = 0
        self._contract_misses = 0
        self._corax: dict[tuple, Any] = {}
        self._corax_hits = 0
        self._corax_misses = 0
        self._original_load_prices: Any = None
        self._original_get_contracts: Any = None
        self._original_corax: dict[Any, Any] = {}

    def __enter__(self) -> "BacktestCache":
        self.load()
        self.install()
        return self

    def __exit__(self, exc_type: type | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        self.uninstall()
        if not self.read_only:
            self.save()

    def load(self) -> None:
        self._prices = self._read(PRICES_FILE)
        self._contracts = self._read(CONTRACTS_FILE)
        self._corax = self._read(CORAX_FILE)
        logger.info(
            f"Loaded cache: {len(self._prices)} prices, {len(self._contracts)} contract lookups, "
            f"{len(self._corax)} corax lookups"
        )

    def save(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._write(PRICES_FILE, self._prices)
        self._write(CONTRACTS_FILE, self._contracts)
        self._write(CORAX_FILE, self._corax)
        logger.info(
            f"Saved cache: {len(self._prices)} prices, {len(self._contracts)} contract lookups, "
            f"{len(self._corax)} corax lookups"
        )

    def stats(self) -> str:
        price_total = self._price_hits + self._price_misses
        contract_total = self._contract_hits + self._contract_misses
        price_rate = f"{self._price_hits / price_total:.1%}" if price_total else "n/a"
        contract_rate = f"{self._contract_hits / contract_total:.1%}" if contract_total else "n/a"
        corax_total = self._corax_hits + self._corax_misses
        corax_rate = f"{self._corax_hits / corax_total:.1%}" if corax_total else "n/a"
        return (
            f"prices {self._price_hits}/{price_total} hit ({price_rate}), "
            f"contracts {self._contract_hits}/{contract_total} hit ({contract_rate}), "
            f"corax {self._corax_hits}/{corax_total} hit ({corax_rate})"
        )

    def install(self) -> None:
        """Wrap the two read paths that dominate a backtest's wall-clock.

        The replacements are plain functions rather than bound methods of this cache: only a
        plain function is a descriptor, so only it receives the pricer/provider as its first
        argument when looked up on the instance.
        """
        cache = self
        self._original_load_prices = StandardIndexPricer.load_prices_for_merq_ids
        self._original_get_contracts = IvolReferenceDataProvider.get_active_contracts

        def load_prices_for_merq_ids(pricer_self, price_date, merq_ids, raise_on_missing_prices=True):  # type: ignore[no-untyped-def]
            return cache._cached_load_prices(pricer_self, price_date, merq_ids, raise_on_missing_prices)

        def get_active_contracts(provider_self, *args, **kwargs):  # type: ignore[no-untyped-def]
            return cache._cached_get_contracts(provider_self, *args, **kwargs)

        StandardIndexPricer.load_prices_for_merq_ids = load_prices_for_merq_ids  # type: ignore[method-assign]
        IvolReferenceDataProvider.get_active_contracts = get_active_contracts  # type: ignore[method-assign]

        for loader_cls in _CORAX_LOADERS:
            original = loader_cls.load_corporate_actions
            self._original_corax[loader_cls] = original

            def load_corporate_actions(loader_self, *args, _original=original, **kwargs):  # type: ignore[no-untyped-def]
                return cache._cached_corax(_original, loader_self, *args, **kwargs)

            loader_cls.load_corporate_actions = load_corporate_actions  # type: ignore[method-assign]

    def _cached_corax(self, original: Any, loader_self: Any, *args: Any, **kwargs: Any) -> Any:
        """Corporate action collections are mutated downstream, so every caller gets its own copy.

        Handing out the cached object itself lets one calculation date's edits leak into the
        next one, which surfaces as spurious "present when unioning corporate actions" errors.
        """
        key = (type(loader_self).__name__,) + self._contract_key(*args, **kwargs)
        if key in self._corax:
            self._corax_hits += 1
            return copy.deepcopy(self._corax[key])

        self._corax_misses += 1
        result = original(loader_self, *args, **kwargs)
        self._corax[key] = copy.deepcopy(result)
        return result

    def uninstall(self) -> None:
        for loader_cls, original in self._original_corax.items():
            loader_cls.load_corporate_actions = original
        self._original_corax = {}
        if self._original_load_prices is not None:
            StandardIndexPricer.load_prices_for_merq_ids = self._original_load_prices  # type: ignore[method-assign]
        if self._original_get_contracts is not None:
            IvolReferenceDataProvider.get_active_contracts = self._original_get_contracts  # type: ignore[method-assign]

    def _cached_load_prices(
        self,
        pricer_self: Any,
        price_date: pd.Timestamp,
        merq_ids: Iterable[Any],
        raise_on_missing_prices: bool = True,
    ) -> dict:
        """Serve per-contract prices from the cache and fetch only the ones not seen yet."""
        requested = list(merq_ids)
        resolved = {}
        unseen = []
        for merq_id in requested:
            entry = self._prices.get((str(merq_id), price_date))
            if entry is None:
                unseen.append(merq_id)
            else:
                self._price_hits += 1
                if entry is not _MISSING:
                    resolved[merq_id] = entry

        if unseen:
            self._price_misses += len(unseen)
            fetched = self._original_load_prices(
                pricer_self, price_date=price_date, merq_ids=unseen, raise_on_missing_prices=raise_on_missing_prices
            )
            for merq_id in unseen:
                # Remember absence too, so a contract with no price is not re-queried every run.
                self._prices[(str(merq_id), price_date)] = fetched.get(merq_id, _MISSING)
            resolved.update(fetched)

        return resolved

    def _cached_get_contracts(self, provider_self: Any, *args: Any, **kwargs: Any) -> Any:
        key = self._contract_key(*args, **kwargs)
        if key in self._contracts:
            self._contract_hits += 1
            return self._contracts[key]

        self._contract_misses += 1
        result = self._original_get_contracts(provider_self, *args, **kwargs)
        self._contracts[key] = result
        return result

    @staticmethod
    def _contract_key(*args: Any, **kwargs: Any) -> tuple:
        def normalise(value: Any) -> Any:
            if isinstance(value, (list, tuple, set)):
                return tuple(sorted(str(item) for item in value))
            return str(value)

        return tuple(normalise(a) for a in args) + tuple(sorted((k, normalise(v)) for k, v in kwargs.items()))

    def _read(self, filename: str) -> dict:
        path = self.cache_dir / filename
        if not path.exists():
            return {}
        try:
            with path.open("rb") as handle:
                return pickle.load(handle)
        except Exception:  # noqa: BLE001  # pylint: disable=broad-except
            logger.warning(f"Could not read cache file {path}; starting empty")
            return {}

    def _write(self, filename: str, payload: dict) -> None:
        path = self.cache_dir / filename
        with path.open("wb") as handle:
            pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)


class _Missing:
    """Sentinel recording that a contract genuinely has no price on a date."""

    def __repr__(self) -> str:
        return "<no price>"


_MISSING = _Missing()
