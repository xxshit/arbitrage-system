"""Synthetic public-maintenance paths for the isolated read/write soak only.

No upstream I/O: the real publication, named-lock and listing code runs against
the caller's disposable DB. Quotes, accounts and production settings are never
loaded here. Every simulated network wait asserts this thread owns no SQL slot.
"""
import threading
import time
from unittest.mock import patch


class MaintenanceWorkload:
    def __init__(self, module):
        self.module = module
        self.cycles = self.network_calls = self.max_own_connections = 0
        self.expected_rows = {}
        self.catalog = [dict(symbol=f"MAINT{i}USDT", status="TRADING", contractType="PERPETUAL") for i in range(1001)]
        self.expected_events = 0

    def payload(self, network):
        return {
            "data": [{
                "attributes": {
                    "address": "synthetic-pool", "base_token_price_usd": str(self.cycles + 1),
                    "reserve_in_usd": "250000", "volume_usd": {"h1": "80000", "h24": "600000"},
                    "price_change_percentage": {"m5": "4", "h1": "18", "h6": "45", "h24": "70"},
                    "transactions": {"h1": {"buys": 180, "sells": 90}},
                },
                "relationships": {"base_token": {"data": {"id": network + "_base"}},
                                  "quote_token": {"data": {"id": network + "_quote"}}},
            }],
            "included": [
                {"id": network + "_base", "attributes": {"address": "synthetic-base", "symbol": "MAINT", "name": "Synthetic"}},
                {"id": network + "_quote", "attributes": {"address": "synthetic-quote", "symbol": "USDT", "name": "Synthetic quote"}},
            ],
        }

    def step(self):
        module = self.module
        owner = threading.current_thread().name
        def own_holders():
            return [item for item in module.BACKGROUND_RUNTIME.snapshot()["db_holders"] if item["worker"] == owner]

        def checkout(*args):
            self.max_own_connections = max(self.max_own_connections, len(own_holders()))
            assert len(own_holders()) <= 1, "publication borrowed more than one connection"

        def network(url, **kwargs):
            assert not own_holders(), "SQL connection held during simulated upstream wait"
            self.network_calls += 1
            time.sleep(.01)
            assert not own_holders(), "SQL connection held across simulated upstream wait"
            network = url.split("/networks/", 1)[1].split("/", 1)[0]
            # Partial public outages retain the previous network's snapshot.
            if self.cycles % 3 == 2 and network == "bsc":
                raise TimeoutError("synthetic public outage")
            return self.payload(network)

        module.sa_event.listen(module.db.engine, "checkout", checkout)
        try:
            # Alternate removal/reactivation; existing identities reappearing
            # do not fabricate new listing events under the current semantics.
            instruments = self.catalog + ([dict(symbol="OPTIONALUSDT", status="TRADING", contractType="PERPETUAL")] if self.cycles % 2 == 0 else [])
            module.sync_binance_listing_events(instruments)
            if self.cycles % 2 == 1:
                self.expected_events += 1
            with patch.object(module, "get_onchain_json", side_effect=network):
                result = module.refresh_onchain_opportunities()
            for name in result["updated_networks"]:
                self.expected_rows[name] = float(self.cycles + 1)
            assert len(result["updated_networks"]) == (3 if self.cycles % 3 == 2 else 4), result
            assert not own_holders(), "maintenance failed to return its connection"
        finally:
            module.sa_event.remove(module.db.engine, "checkout", checkout)
        self.cycles += 1

    def verify(self):
        module = self.module
        rows = module.OnchainOpportunitySnapshot.query.all()
        actual = {row.network: row.price_usd for row in rows}
        assert actual == self.expected_rows and len(rows) == 4, "onchain publication values differ"
        assert module.ListingState.query.count() == 1002
        assert module.ListingEvent.query.count() == self.expected_events
        optional = module.ListingState.query.filter_by(symbol="OPTIONALUSDT").one()
        assert optional.active == ((self.cycles - 1) % 2 == 0)
        assert self.network_calls == 4 * self.cycles
        assert self.max_own_connections == 1
        module.db.session.remove()
        return dict(cycles=self.cycles, network_calls=self.network_calls, network_holders=0,
                    max_publication_connections=self.max_own_connections, onchain_rows=4,
                    listing_rows=1002, listing_events=self.expected_events, values="exact")
