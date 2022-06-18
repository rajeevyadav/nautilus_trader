# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2022 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
# -------------------------------------------------------------------------------------------------

import asyncio
import hashlib
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

import msgspec
import orjson

from nautilus_trader.accounting.factory import AccountFactory
from nautilus_trader.adapters.betfair.client.core import BetfairClient
from nautilus_trader.adapters.betfair.client.exceptions import BetfairAPIError
from nautilus_trader.adapters.betfair.client.schema.streaming import OrderChangeMessage
from nautilus_trader.adapters.betfair.client.schema.streaming import StatusMessage
from nautilus_trader.adapters.betfair.client.schema.streaming import UnmatchedOrder
from nautilus_trader.adapters.betfair.common import B2N_ORDER_STREAM_SIDE
from nautilus_trader.adapters.betfair.common import BETFAIR_QUANTITY_PRECISION
from nautilus_trader.adapters.betfair.common import BETFAIR_VENUE
from nautilus_trader.adapters.betfair.common import price_to_probability
from nautilus_trader.adapters.betfair.common import probability_to_price
from nautilus_trader.adapters.betfair.parsing import bet_to_order_status_report
from nautilus_trader.adapters.betfair.parsing import betfair_account_to_account_state
from nautilus_trader.adapters.betfair.parsing import order_cancel_all_to_betfair
from nautilus_trader.adapters.betfair.parsing import order_cancel_to_betfair
from nautilus_trader.adapters.betfair.parsing import order_submit_to_betfair
from nautilus_trader.adapters.betfair.parsing import order_update_to_betfair
from nautilus_trader.adapters.betfair.parsing import parse_handicap
from nautilus_trader.adapters.betfair.providers import BetfairInstrumentProvider
from nautilus_trader.adapters.betfair.sockets import BetfairOrderStreamClient
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.clock import LiveClock
from nautilus_trader.common.logging import LogColor
from nautilus_trader.common.logging import Logger
from nautilus_trader.core.correctness import PyCondition
from nautilus_trader.core.datetime import millis_to_nanos
from nautilus_trader.core.datetime import nanos_to_secs
from nautilus_trader.core.datetime import secs_to_nanos
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import CancelAllOrders
from nautilus_trader.execution.messages import CancelOrder
from nautilus_trader.execution.messages import ModifyOrder
from nautilus_trader.execution.messages import SubmitOrder
from nautilus_trader.execution.messages import SubmitOrderList
from nautilus_trader.execution.reports import OrderStatusReport
from nautilus_trader.execution.reports import PositionStatusReport
from nautilus_trader.execution.reports import TradeReport
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.model.currency import Currency
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import LiquiditySide
from nautilus_trader.model.enums import OMSType
from nautilus_trader.model.enums import OrderType
from nautilus_trader.model.events.account import AccountState
from nautilus_trader.model.identifiers import AccountId
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import TradeId
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.model.objects import Money
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity
from nautilus_trader.model.orders.base import Order
from nautilus_trader.msgbus.bus import MessageBus


class BetfairExecutionClient(LiveExecutionClient):
    """
    Provides an execution client for Betfair.

    Parameters
    ----------
    loop : asyncio.AbstractEventLoop
        The event loop for the client.
    client : BetfairClient
        The Betfair HttpClient.
    base_currency : Currency
        The account base currency for the client.
    msgbus : MessageBus
        The message bus for the client.
    cache : Cache
        The cache for the client.
    clock : LiveClock
        The clock for the client.
    logger : Logger
        The logger for the client.
    market_filter : dict
        The market filter.
    instrument_provider : BetfairInstrumentProvider, optional
        The instrument provider.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        client: BetfairClient,
        base_currency: Currency,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        logger: Logger,
        market_filter: Dict,
        instrument_provider: BetfairInstrumentProvider,
    ):
        super().__init__(
            loop=loop,
            client_id=ClientId(BETFAIR_VENUE.value),
            venue=BETFAIR_VENUE,
            oms_type=OMSType.NETTING,
            account_type=AccountType.BETTING,
            base_currency=base_currency,
            instrument_provider=instrument_provider
            or BetfairInstrumentProvider(client=client, logger=logger, filters=market_filter),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            logger=logger,
        )

        self._client: BetfairClient = client
        self.stream = BetfairOrderStreamClient(
            client=self._client,
            logger=logger,
            message_handler=self.handle_order_stream_update,
        )

        self.venue_order_id_to_client_order_id: Dict[VenueOrderId, ClientOrderId] = {}
        self.pending_update_order_client_ids: Set[Tuple[ClientOrderId, VenueOrderId]] = set()
        self.published_executions: Dict[ClientOrderId, TradeId] = defaultdict(list)

        self._set_account_id(AccountId(f"{BETFAIR_VENUE}-001"))
        AccountFactory.register_calculated_account(BETFAIR_VENUE.value)

    # -- CONNECTION HANDLERS ----------------------------------------------------------------------

    def connect(self):
        self._log.info("Connecting...")
        self._loop.create_task(self._connect())

    def disconnect(self):
        self._log.info("Disconnecting...")
        self._loop.create_task(self._disconnect())

    async def _connect(self):
        self._log.info("Connecting to BetfairClient...")
        await self._client.connect()
        self._log.info("BetfairClient login successful.", LogColor.GREEN)

        aws = [
            self.stream.connect(),
            self.connection_account_state(),
            self.check_account_currency(),
        ]
        await asyncio.gather(*aws)
        self.create_task(self.watch_stream())
        self._set_connected(True)
        assert self.is_connected
        self._log.info("Connected.")

    async def _disconnect(self) -> None:
        # Close socket
        self._log.info("Closing streaming socket...")
        await self.stream.disconnect()

        # Ensure client closed
        self._log.info("Closing BetfairClient...")
        await self._client.disconnect()

        self._set_connected(False)
        self._log.info("Disconnected.")

    async def watch_stream(self):
        """Ensure socket stream is connected"""
        while True:
            if not self.stream.is_connected:
                self.stream.connect()
            await asyncio.sleep(1)

    # -- ERROR HANDLING ---------------------------------------------------------------------------
    async def on_api_exception(self, ex: BetfairAPIError):
        if ex.kind == "INVALID_SESSION_INFORMATION":
            # Session is invalid, need to reconnect
            self._log.warning("Invalid session error, reconnecting..")
            await self._client.disconnect()
            await self._connect()
            self._log.info("Reconnected.")

    # -- ACCOUNT HANDLERS -------------------------------------------------------------------------

    async def connection_account_state(self):
        account_details = await self._client.get_account_details()
        account_funds = await self._client.get_account_funds()
        timestamp = self._clock.timestamp_ns()
        account_state: AccountState = betfair_account_to_account_state(
            account_detail=account_details,
            account_funds=account_funds,
            event_id=UUID4(),
            ts_event=timestamp,
            ts_init=timestamp,
        )
        self._log.debug(f"Received account state: {account_state}, sending")
        self._send_account_state(account_state)
        self._log.debug("Initial Account state completed")

    # -- EXECUTION REPORTS ------------------------------------------------------------------------

    async def generate_order_status_report(
        self,
        instrument_id: InstrumentId,
        client_order_id: Optional[ClientOrderId] = None,
        venue_order_id: Optional[VenueOrderId] = None,
    ) -> Optional[OrderStatusReport]:
        assert venue_order_id is not None
        orders = await self._client.list_current_orders(
            bet_ids=[venue_order_id],
        )

        if not orders:
            self._log.warning(f"Could not find order for venue_order_id={venue_order_id}")
            return None
        # We have a response, check list length and grab first entry
        assert len(orders) == 1
        order = orders[0]
        instrument = self._instrument_provider.get_betting_instrument(
            market_id=str(order["marketId"]),
            selection_id=str(order["selectionId"]),
            handicap=parse_handicap(order["handicap"]),
        )
        venue_order_id = VenueOrderId(order["betId"])
        return bet_to_order_status_report(
            order=order,
            account_id=self.account_id,
            instrument_id=instrument.id,
            venue_order_id=venue_order_id,
            client_order_id=self._cache.client_order_id(venue_order_id),
            report_id=UUID4(),
            ts_init=self._clock.timestamp_ns(),
        )

    async def generate_order_status_reports(
        self,
        instrument_id: InstrumentId = None,
        start: datetime = None,
        end: datetime = None,
        open_only: bool = False,
    ) -> List[OrderStatusReport]:
        self._log.warning("Cannot generate OrderStatusReports: not yet implemented.")

        return []

    async def generate_trade_reports(
        self,
        instrument_id: InstrumentId = None,
        venue_order_id: VenueOrderId = None,
        start: datetime = None,
        end: datetime = None,
    ) -> List[TradeReport]:
        self._log.warning("Cannot generate TradeReports: not yet implemented.")

        return []

    async def generate_position_status_reports(
        self,
        instrument_id: InstrumentId = None,
        start: datetime = None,
        end: datetime = None,
    ) -> List[PositionStatusReport]:
        self._log.warning("Cannot generate PositionStatusReports: not yet implemented.")

        return []

    # -- COMMAND HANDLERS -------------------------------------------------------------------------

    def submit_order(self, command: SubmitOrder) -> None:
        PyCondition.not_none(command, "command")

        self.create_task(self._submit_order(command))

    async def _submit_order(self, command: SubmitOrder) -> None:
        self._log.debug(f"Received submit_order {command}")

        self.generate_order_submitted(
            instrument_id=command.instrument_id,
            strategy_id=command.strategy_id,
            client_order_id=command.order.client_order_id,
            ts_event=self._clock.timestamp_ns(),
        )
        self._log.debug("Generated _generate_order_submitted")

        instrument = self._cache.instrument(command.instrument_id)
        PyCondition.not_none(instrument, "instrument")
        client_order_id = command.order.client_order_id

        place_order = order_submit_to_betfair(command=command, instrument=instrument)
        try:
            result = await self._client.place_orders(**place_order)
        except Exception as ex:
            if isinstance(ex, BetfairAPIError):
                await self.on_api_exception(ex=ex)
            self._log.warning(f"Submit failed: {ex}")
            self.generate_order_rejected(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=client_order_id,
                reason="client error",  # type: ignore
                ts_event=self._clock.timestamp_ns(),
            )
            return

        self._log.debug(f"result={result}")
        for report in result["instructionReports"]:
            if result["status"] == "FAILURE":
                reason = f"{result['errorCode']}: {report['errorCode']}"
                self._log.warning(f"Submit failed - {reason}")
                self.generate_order_rejected(
                    strategy_id=command.strategy_id,
                    instrument_id=command.instrument_id,
                    client_order_id=client_order_id,
                    reason=reason,  # type: ignore
                    ts_event=self._clock.timestamp_ns(),
                )
                self._log.debug("Generated _generate_order_rejected")
                return
            else:
                venue_order_id = VenueOrderId(report["betId"])
                self._log.debug(
                    f"Matching venue_order_id: {venue_order_id} to client_order_id: {client_order_id}"
                )
                self.venue_order_id_to_client_order_id[venue_order_id] = client_order_id  # type: ignore
                self.generate_order_accepted(
                    strategy_id=command.strategy_id,
                    instrument_id=command.instrument_id,
                    client_order_id=client_order_id,
                    venue_order_id=venue_order_id,  # type: ignore
                    ts_event=self._clock.timestamp_ns(),
                )
                self._log.debug("Generated _generate_order_accepted")

    def submit_order_list(self, command: SubmitOrderList):
        # TODO(cs): Implement
        raise NotImplementedError("method must be implemented in the subclass")  # pragma: no cover

    def modify_order(self, command: ModifyOrder) -> None:
        PyCondition.not_none(command, "command")

        self.create_task(self._modify_order(command))

    async def _modify_order(self, command: ModifyOrder) -> None:
        self._log.debug(f"Received modify_order {command}")
        client_order_id: ClientOrderId = command.client_order_id
        instrument = self._cache.instrument(command.instrument_id)
        PyCondition.not_none(instrument, "instrument")
        existing_order = self._cache.order(client_order_id)  # type: Order

        self.generate_order_pending_update(
            strategy_id=command.strategy_id,
            instrument_id=command.instrument_id,
            client_order_id=command.client_order_id,
            venue_order_id=command.venue_order_id,
            ts_event=self._clock.timestamp_ns(),
        )

        if existing_order is None:
            self._log.warning(
                f"Attempting to update order that does not exist in the cache: {command}"
            )
            self.generate_order_modify_rejected(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=client_order_id,
                venue_order_id=command.venue_order_id,
                reason="ORDER NOT IN CACHE",
                ts_event=self._clock.timestamp_ns(),
            )
            return
        if existing_order.venue_order_id is None:
            self._log.warning(f"Order found does not have `id` set: {existing_order}")
            PyCondition.not_none(command.strategy_id, "command.strategy_id")
            PyCondition.not_none(command.instrument_id, "command.instrument_id")
            PyCondition.not_none(client_order_id, "client_order_id")
            self.generate_order_modify_rejected(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=client_order_id,
                venue_order_id=VenueOrderId("-1"),
                reason="ORDER MISSING VENUE_ORDER_ID",
                ts_event=self._clock.timestamp_ns(),
            )
            return

        # Send order to client
        kw = order_update_to_betfair(
            command=command,
            venue_order_id=existing_order.venue_order_id,
            side=existing_order.side,
            instrument=instrument,
        )
        self.pending_update_order_client_ids.add(
            (command.client_order_id, existing_order.venue_order_id)
        )
        try:
            result = await self._client.replace_orders(**kw)
        except Exception as ex:
            if isinstance(ex, BetfairAPIError):
                await self.on_api_exception(ex=ex)
            self._log.warning(f"Modify failed: {ex}")
            self.generate_order_modify_rejected(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=command.client_order_id,
                venue_order_id=existing_order.venue_order_id,
                reason="client error",
                ts_event=self._clock.timestamp_ns(),
            )
            return

        self._log.debug(f"result={result}")

        for report in result["instructionReports"]:
            if report["status"] == "FAILURE":
                reason = f"{result['errorCode']}: {report['errorCode']}"
                self._log.warning(f"replace failed - {reason}")
                self.generate_order_rejected(
                    strategy_id=command.strategy_id,
                    instrument_id=command.instrument_id,
                    client_order_id=command.client_order_id,
                    reason=reason,
                    ts_event=self._clock.timestamp_ns(),
                )
                return

            # Check the venue_order_id that has been deleted currently exists on our order
            deleted_bet_id = report["cancelInstructionReport"]["instruction"]["betId"]
            self._log.debug(f"{existing_order}, {deleted_bet_id}")
            assert existing_order.venue_order_id == VenueOrderId(deleted_bet_id)

            update_instruction = report["placeInstructionReport"]
            venue_order_id = VenueOrderId(update_instruction["betId"])
            self.venue_order_id_to_client_order_id[venue_order_id] = client_order_id
            self.generate_order_updated(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=client_order_id,
                venue_order_id=VenueOrderId(update_instruction["betId"]),
                quantity=Quantity(
                    update_instruction["instruction"]["limitOrder"]["size"],
                    precision=BETFAIR_QUANTITY_PRECISION,
                ),
                price=price_to_probability(
                    str(update_instruction["instruction"]["limitOrder"]["price"])
                ),
                trigger_price=None,  # Not applicable for Betfair
                ts_event=self._clock.timestamp_ns(),
                venue_order_id_modified=True,
            )

    def cancel_order(self, command: CancelOrder) -> None:
        PyCondition.not_none(command, "command")

        self.create_task(self._cancel_order(command))

    async def _cancel_order(self, command: CancelOrder) -> None:
        self._log.debug(f"Received cancel order: {command}")
        self.generate_order_pending_cancel(
            strategy_id=command.strategy_id,
            instrument_id=command.instrument_id,
            client_order_id=command.client_order_id,
            venue_order_id=command.venue_order_id,
            ts_event=self._clock.timestamp_ns(),
        )

        instrument = self._cache.instrument(command.instrument_id)
        PyCondition.not_none(instrument, "instrument")

        # Format
        cancel_order = order_cancel_to_betfair(command=command, instrument=instrument)  # type: ignore
        self._log.debug(f"cancel_order {cancel_order}")

        # Send to client
        try:
            result = await self._client.cancel_orders(**cancel_order)
        except Exception as ex:
            if isinstance(ex, BetfairAPIError):
                await self.on_api_exception(ex=ex)
            self._log.warning(f"Cancel failed: {ex}")
            self.generate_order_cancel_rejected(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=command.client_order_id,
                venue_order_id=command.venue_order_id,
                reason="client error",
                ts_event=self._clock.timestamp_ns(),
            )
            return
        self._log.debug(f"result={result}")

        # Parse response
        for report in result["instructionReports"]:
            venue_order_id = VenueOrderId(report["instruction"]["betId"])
            if report["status"] == "FAILURE":
                reason = f"{result.get('errorCode', 'Error')}: {report['errorCode']}"
                self._log.warning(f"cancel failed - {reason}")
                self.generate_order_cancel_rejected(
                    strategy_id=command.strategy_id,
                    instrument_id=command.instrument_id,
                    client_order_id=command.client_order_id,
                    venue_order_id=venue_order_id,
                    reason=reason,
                    ts_event=self._clock.timestamp_ns(),
                )
                return

            self._log.debug(
                f"Matching venue_order_id: {venue_order_id} to client_order_id: {command.client_order_id}"
            )
            self.venue_order_id_to_client_order_id[venue_order_id] = command.client_order_id  # type: ignore
            self.generate_order_canceled(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=command.client_order_id,
                venue_order_id=venue_order_id,  # type: ignore
                ts_event=self._clock.timestamp_ns(),
            )
            self._log.debug("Sent order cancel")

    def cancel_all_orders(self, command: CancelAllOrders) -> None:
        PyCondition.not_none(command, "command")

        open_orders = self._cache.open_orders(instrument_id=command.instrument_id)

        # TODO(cs): Temporary solution generating individual cancels for all open orders
        for order in open_orders:
            command = CancelOrder(
                trader_id=command.trader_id,
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=order.client_order_id,
                venue_order_id=order.venue_order_id,
                command_id=UUID4(),
                ts_init=self._clock.timestamp_ns(),
            )

            self.cancel_order(command)

        # TODO(cs): Relates to below _cancel_all_orders
        # Format
        # cancel_orders = order_cancel_all_to_betfair(instrument=instrument)  # type: ignore
        # self._log.debug(f"cancel_orders {cancel_orders}")
        #
        # self.create_task(self._cancel_order(command))

    # TODO(cs): Currently not in use as old behavior restored to cancel orders individually
    async def _cancel_all_orders(self, command: CancelAllOrders) -> None:
        # TODO(cs): I've had to duplicate the logic as couldn't refactor and tease
        #  apart the cancel rejects and trade report. This will possibly fail
        #  badly if there are any API errors...
        self._log.debug(f"Received cancel all orders: {command}")

        instrument = self._cache.instrument(command.instrument_id)
        PyCondition.not_none(instrument, "instrument")

        # Format
        cancel_orders = order_cancel_all_to_betfair(instrument=instrument)  # type: ignore
        self._log.debug(f"cancel_orders {cancel_orders}")

        # Send to client
        try:
            result = await self._client.cancel_orders(**cancel_orders)
        except Exception as ex:
            if isinstance(ex, BetfairAPIError):
                await self.on_api_exception(ex=ex)
            self._log.error(f"Cancel failed: {ex}")
            # TODO(cs): Will probably just need to recover the client order ID
            #  and order ID from the trade report?
            # self.generate_order_cancel_rejected(
            #     strategy_id=command.strategy_id,
            #     instrument_id=command.instrument_id,
            #     client_order_id=command.client_order_id,
            #     venue_order_id=command.venue_order_id,
            #     reason="client error",
            #     ts_event=self._clock.timestamp_ns(),
            # )
            return
        self._log.debug(f"result={result}")

        # Parse response
        for report in result["instructionReports"]:
            venue_order_id = VenueOrderId(report["instruction"]["betId"])
            if report["status"] == "FAILURE":
                reason = f"{result.get('errorCode', 'Error')}: {report['errorCode']}"
                self._log.error(f"cancel failed - {reason}")
                # TODO(cs): Will probably just need to recover the client order ID
                #  and order ID from the trade report?
                # self.generate_order_cancel_rejected(
                #     strategy_id=command.strategy_id,
                #     instrument_id=command.instrument_id,
                #     client_order_id=command.client_order_id,
                #     venue_order_id=venue_order_id,
                #     reason=reason,
                #     ts_event=self._clock.timestamp_ns(),
                # )
                # return

            self._log.debug(
                f"Matching venue_order_id: {venue_order_id} to client_order_id: {command.client_order_id}"
            )
            self.venue_order_id_to_client_order_id[venue_order_id] = command.client_order_id  # type: ignore
            self.generate_order_canceled(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=command.client_order_id,
                venue_order_id=venue_order_id,  # type: ignore
                ts_event=self._clock.timestamp_ns(),
            )
            self._log.debug("Sent order cancel")

    # cpdef void bulk_submit_order(self, list commands):
    # betfair allows up to 200 inserts per request
    #     raise NotImplementedError

    # cpdef void bulk_submit_update(self, list commands):
    # betfair allows up to 60 updates per request
    #     raise NotImplementedError

    # cpdef void bulk_submit_delete(self, list commands):
    # betfair allows up to 60 cancels per request
    #     raise NotImplementedError

    # -- ACCOUNT ----------------------------------------------------------------------------------

    async def check_account_currency(self):
        """
        Check account currency against BetfairClient
        """
        self._log.debug("Checking account currency")
        PyCondition.not_none(self.base_currency, "self.base_currency")
        details = await self._client.get_account_details()
        currency_code = details["currencyCode"]
        self._log.debug(f"Account {currency_code=}, {self.base_currency.code=}")
        assert currency_code == self.base_currency.code
        self._log.debug("Base currency matches client details")

    # -- DEBUGGING --------------------------------------------------------------------------------

    def create_task(self, coro):
        self._loop.create_task(self._check_task(coro))

    async def _check_task(self, coro):
        try:
            awaitable = await coro
            return awaitable
        except Exception as ex:
            self._log.exception("Unhandled exception", ex)

    def client(self) -> BetfairClient:
        return self._client

    # -- ORDER STREAM API -------------------------------------------------------------------------

    async def handle_order_stream_update(self, raw: bytes) -> None:
        """Handle an update from the order stream socket"""
        if raw.startswith(b'{"op":"ocm"'):
            order_change_message = msgspec.json.decode(raw, type=OrderChangeMessage)
            await self._handle_order_stream_update(order_change_message=order_change_message)
        elif raw.startswith(b'{"op":"connection"'):
            pass
        elif raw.startswith(b'{"op":"status"'):
            update = msgspec.json.decode(raw, type=StatusMessage)
            self._handle_status_message(update=update)
        else:
            raise RuntimeError

    async def _handle_order_stream_update(self, order_change_message: OrderChangeMessage):
        for market in order_change_message.oc:
            # market_id = market["id"]
            for selection in market.orc:
                if selection.fullImage:
                    # TODO (bm) - need to replace orders for this selection - probably via a recon
                    self._log.debug("Received full order image")
                for unmatched_order in selection.uo:
                    await self._check_order_update(unmatched_order=unmatched_order)
                    if unmatched_order.status == "E":
                        self._handle_stream_executable_order_update(unmatched_order=unmatched_order)
                    elif unmatched_order.status == "EC":
                        self._handle_stream_execution_complete_order_update(
                            unmatched_order=unmatched_order
                        )
                    else:
                        self._log.warning(f"Unknown order state: {unmatched_order}")

    async def _check_order_update(self, unmatched_order: UnmatchedOrder):
        """
        Ensure we have a client_order_id, instrument and order for this venue order update
        """
        venue_order_id = VenueOrderId(str(unmatched_order.id))
        client_order_id = await self.wait_for_order(
            venue_order_id=venue_order_id, timeout_seconds=10.0
        )
        if client_order_id is None:
            self._log.warning(f"Can't find client_order_id for {unmatched_order}")
            return
        PyCondition.type(client_order_id, ClientOrderId, "client_order_id")
        order = self._cache.order(client_order_id)
        PyCondition.not_none(order, "order")
        instrument = self._cache.instrument(order.instrument_id)
        PyCondition.not_none(instrument, "instrument")

    def _handle_stream_executable_order_update(self, unmatched_order: UnmatchedOrder) -> None:
        """
        Handle update containing "E" (executable) order update
        """
        venue_order_id = VenueOrderId(unmatched_order.id)
        client_order_id = self.venue_order_id_to_client_order_id[venue_order_id]
        order = self._cache.order(client_order_id)
        instrument = self._cache.instrument(order.instrument_id)

        # Check if this is the first time seeing this order (backtest or replay)
        if venue_order_id in self.venue_order_id_to_client_order_id:
            # We've already sent an accept for this order in self._submit_order
            self._log.debug(
                f"Skipping order_accept as order exists: venue_order_id={unmatched_order.id}"
            )
        else:
            raise RuntimeError()
            # self.generate_order_accepted(
            #     strategy_id=order.strategy_id,
            #     instrument_id=instrument.id,
            #     client_order_id=client_order_id,
            #     venue_order_id=venue_order_id,
            #     ts_event=millis_to_nanos(order_update["pd"]),
            # )

        # Check for any portion executed
        if unmatched_order.sm > 0 and unmatched_order.sm > order.filled_qty:
            trade_id = create_trade_id(unmatched_order)
            if trade_id not in self.published_executions[client_order_id]:
                fill_qty = unmatched_order.sm - order.filled_qty
                fill_price = self._determine_fill_price(
                    unmatched_order=unmatched_order, order=order
                )
                self.generate_order_filled(
                    strategy_id=order.strategy_id,
                    instrument_id=order.instrument_id,
                    client_order_id=client_order_id,
                    venue_order_id=venue_order_id,
                    venue_position_id=None,  # Can be None
                    trade_id=trade_id,
                    order_side=B2N_ORDER_STREAM_SIDE[unmatched_order.side],
                    order_type=OrderType.LIMIT,
                    last_qty=Quantity(fill_qty, BETFAIR_QUANTITY_PRECISION),
                    last_px=price_to_probability(str(fill_price)),
                    # avg_px=Decimal(order['avp']),
                    quote_currency=instrument.quote_currency,
                    commission=Money(0, self.base_currency),
                    liquidity_side=LiquiditySide.NONE,
                    ts_event=millis_to_nanos(unmatched_order.md),
                )
                self.published_executions[client_order_id].append(trade_id)

    def _determine_fill_price(self, unmatched_order: UnmatchedOrder, order: Order):
        if not unmatched_order.avp:
            # We don't have any specifics about the fill, assume it was filled at our price
            return unmatched_order.p
        if order.filled_qty == 0:
            # New fill, simply return average price
            return unmatched_order.avp
        else:
            new_price = price_to_probability(str(unmatched_order.avp))
            prev_price = order.avg_px
            if prev_price == new_price:
                # Matched at same price
                return unmatched_order.avp
            else:
                prev_price = probability_to_price(order.avg_px)
                prev_size = order.filled_qty
                new_price = Price.from_str(str(unmatched_order.avp))
                new_size = unmatched_order.sm - prev_size
                total_size = prev_size + new_size
                price = (new_price - ((prev_price * (prev_size / total_size)))) / (
                    new_size / total_size
                )
                self._log.debug(
                    f"Calculating fill price {prev_price=} {prev_size=} {new_price=} {new_size=} == {price=}"
                )
                return price

    def _handle_stream_execution_complete_order_update(
        self, unmatched_order: UnmatchedOrder
    ) -> None:
        """
        Handle "EC" (execution complete) order updates
        """
        venue_order_id = VenueOrderId(str(unmatched_order.id))
        client_order_id = self._cache.client_order_id(venue_order_id=venue_order_id)
        order = self._cache.order(client_order_id=client_order_id)
        instrument = self._cache.instrument(order.instrument_id)

        if unmatched_order.sm > 0 and unmatched_order.sm > order.filled_qty:
            self._log.debug("")
            trade_id = create_trade_id(unmatched_order)
            if trade_id not in self.published_executions[client_order_id]:
                fill_qty = unmatched_order.sm - order.filled_qty
                fill_price = self._determine_fill_price(
                    unmatched_order=unmatched_order, order=order
                )
                # At least some part of this order has been filled
                self.generate_order_filled(
                    strategy_id=order.strategy_id,
                    instrument_id=instrument.id,
                    client_order_id=client_order_id,
                    venue_order_id=venue_order_id,
                    venue_position_id=None,  # Can be None
                    trade_id=trade_id,
                    order_side=B2N_ORDER_STREAM_SIDE[unmatched_order.side],
                    order_type=OrderType.LIMIT,
                    last_qty=Quantity(fill_qty, BETFAIR_QUANTITY_PRECISION),
                    last_px=price_to_probability(str(fill_price)),
                    quote_currency=instrument.quote_currency,
                    # avg_px=order['avp'],
                    commission=Money(0, self.base_currency),
                    liquidity_side=LiquiditySide.TAKER,  # TODO - Fix this?
                    ts_event=millis_to_nanos(unmatched_order.md),
                )
                self.published_executions[client_order_id].append(trade_id)

        cancel_qty = unmatched_order.sc + unmatched_order.sl + unmatched_order.sv
        if cancel_qty > 0 and not order.is_closed:
            assert (
                unmatched_order.sm + cancel_qty == unmatched_order.s
            ), f"Size matched + canceled != total: {unmatched_order}"
            # If this is the result of a ModifyOrder, we don't want to emit a cancel

            key = (client_order_id, venue_order_id)
            self._log.debug(
                f"cancel key: {key}, pending_update_order_client_ids: {self.pending_update_order_client_ids}"
            )
            if key not in self.pending_update_order_client_ids:
                # The remainder of this order has been canceled
                cancelled_ts = unmatched_order.cd or unmatched_order.ld or unmatched_order.md
                if cancelled_ts is not None:
                    cancelled_ts = millis_to_nanos(cancelled_ts)
                else:
                    cancelled_ts = self._clock.timestamp_ns()
                self.generate_order_canceled(
                    strategy_id=order.strategy_id,
                    instrument_id=instrument.id,
                    client_order_id=client_order_id,
                    venue_order_id=venue_order_id,
                    ts_event=cancelled_ts,
                )
                if venue_order_id in self.venue_order_id_to_client_order_id:
                    del self.venue_order_id_to_client_order_id[venue_order_id]
        # Market order will not be in self.published_executions
        if client_order_id in self.published_executions:
            # This execution is complete - no need to track this anymore
            del self.published_executions[client_order_id]

    async def wait_for_order(
        self, venue_order_id: VenueOrderId, timeout_seconds=10.0
    ) -> Optional[ClientOrderId]:
        """
        We may get an order update from the socket before our submit_order
        response has come back (with our betId).

        As a precaution, wait up to `timeout_seconds` for the betId to be added
        to `self.order_id_to_client_order_id`.
        """
        assert isinstance(venue_order_id, VenueOrderId)
        start = self._clock.timestamp_ns()
        now = start
        while (now - start) < secs_to_nanos(timeout_seconds):
            # self._log.debug(
            #     f"checking venue_order_id={venue_order_id} in {self.venue_order_id_to_client_order_id}"
            # )
            if venue_order_id in self.venue_order_id_to_client_order_id:
                client_order_id = self.venue_order_id_to_client_order_id[venue_order_id]
                self._log.debug(
                    f"Found order in {nanos_to_secs(now - start)} sec: {client_order_id}"
                )
                return client_order_id
            now = self._clock.timestamp_ns()
            await asyncio.sleep(0.1)
        self._log.warning(
            f"Failed to find venue_order_id: {venue_order_id} "
            f"after {timeout_seconds} seconds"
            f"\nexisting: {self.venue_order_id_to_client_order_id})"
        )
        return None

    def _handle_status_message(self, update: StatusMessage):
        if update.statusCode == "FAILURE" and update.connectionClosed:
            # TODO (bm) - self._loop.create_task(self._stream.reconnect())
            self._log.error(str(update))
            raise RuntimeError()


def create_trade_id(uo: UnmatchedOrder) -> TradeId:
    data: bytes = orjson.dumps(
        (
            uo.id,
            uo.p,
            uo.s,
            uo.side,
            uo.pt,
            uo.ot,
            uo.pd,
            uo.md,
            uo.avp,
            uo.sm,
        )
    )
    return TradeId(hashlib.sha1(data).hexdigest())  # noqa (S303 insecure SHA1)
