from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalogWriter
from tests.test_kit.stubs.data import TestDataStubs


class TestParquetDataCatalogWriter:
    def setup(self):
        self.catalog = ParquetDataCatalogWriter(path="/", fs_protocol="memory")

    def test_write_quote_ticks_pyarrow(self):
        # Arrange
        ticks = [
            TestDataStubs.quote_tick_3decimal(bid=str(price - 5), ask=str(price + 5))
            for price in range(100, 200, 50)
        ]

        # Act
        self.catalog.write_objects(ticks)

        # Assert

    def test_write_quote_ticks_rust(self):
        ticks = [
            TestDataStubs.quote_tick_3decimal(bid=str(price - 5), ask=str(price + 5))
            for price in range(100, 200, 50)
        ]
        self.catalog.write_objects(ticks, use_rust=True)
