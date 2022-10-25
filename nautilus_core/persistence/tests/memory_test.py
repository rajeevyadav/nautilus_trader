import gc

from nautilus_trader.model.data.tick import QuoteTick
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity


def get_obj(values):
    return QuoteTick(
        instrument_id=InstrumentId.from_str(values["instrument_id"]),
        bid=Price.from_str(values["bid"]),
        ask=Price.from_str(values["ask"]),
        bid_size=Quantity.from_str(values["bid_size"]),
        ask_size=Quantity.from_str(values["ask_size"]),
        ts_event=values["ts_event"],
        ts_init=values["ts_init"],
    )


def to_obj(data):
    rsv = []
    for e in data:
        o = get_obj(e)
        rsv.append(o)
    return rsv


def run():
    data = {
        "bid": "107.02",
        "bid_size": "1.78",
        "ask": "107.04",
        "ask_size": "1.78",
        "ts_event": "1648425600000000000",
        "ts_init": "1648425600000000000",
        "instrument_id": "SOLUSDT.BINANCE",
    }
    obj = get_obj(data)
    print(obj.ts_event)
    del obj
    print("here")
    obj = None
    print(obj)
    gc.collect()


def run_large():
    data = {
        "bid": "107.02",
        "bid_size": "1.78",
        "ask": "107.04",
        "ask_size": "1.78",
        "ts_event": 1648425600000000000,
        "ts_init": 1648425600000000000,
        "instrument_id": "SOLUSDT.BINANCE",
    }
    obj = None
    for _ in range(20):
        for _ in range(1000000):
            obj = get_obj(data)
        print("here")
        obj = None
        print(obj)
        gc.collect()


if __name__ == "__main__":
    run()
