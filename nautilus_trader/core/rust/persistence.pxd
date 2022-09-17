# Warning, this file is autogenerated by cbindgen. Don't modify this manually. */

from cpython.object cimport PyObject
from libc.stdint cimport uintptr_t, uint64_t
from nautilus_trader.core.rust.core cimport CVec

cdef extern from "../includes/persistence.h":

    cdef enum ParquetReaderType:
        File # = 0,
        Buffer # = 1,

    # Types that implement parquet reader writer traits should also have a
    # corresponding enum so that they can be passed across the ffi.
    cdef enum ParquetType:
        QuoteTick # = 0,
        TradeTick # = 1,

    # ParquetWriter is generic for any writer however for ffi it only supports
    # byte buffer writers. This is so that the byte buffer can be returned after
    # the writer is ended.
    #
    # # Safety
    # - Assumes `file_path` is borrowed from a valid Python UTF-8 `str`.
    # - Assumes `metadata` is borrowed from a valid Python `dict`.
    void *parquet_writer_new(ParquetType parquet_type, PyObject *metadata);

    # Writer is flushed, consumed and dropped. The underlying writer is returned.
    # While this is generic for ffi it only considers and returns a vector of bytes
    # if the underlying writer is anything else it will fail.
    #
    # # Safety
    # - Assumes `writer` is a valid `*mut ParquetWriter<Struct>` where the struct
    # has a corresponding ParquetType enum.
    CVec parquet_writer_drop(void *writer, ParquetType parquet_type);

    # # Safety
    # - Assumes `writer` is a valid `*mut ParquetWriter<Struct>` where the struct
    # has a corresponding ParquetType enum.
    # - Assumes  `data` is a non-null valid pointer to a contiguous block of
    # C-style structs with `len` number of elements
    void parquet_writer_write(void *writer, ParquetType parquet_type, void *data, uintptr_t len);

    # # Safety
    # - Assumes `file_path` is a valid `*mut ParquetReader<QuoteTick>`.
    void *parquet_reader_file_new(PyObject *file_path,
                                  ParquetType parquet_type,
                                  uintptr_t chunk_size);

    # # Safety
    # - Assumes `data` is a valid CVec with an underlying byte buffer
    void *parquet_reader_buffer_new(CVec data, ParquetType parquet_type, uintptr_t chunk_size);

    # # Safety
    # - Assumes `reader` is a valid `*mut ParquetReader<Struct>` where the struct
    # has a corresponding [ParquetType] enum.
    void parquet_reader_free(void *reader, ParquetType parquet_type, ParquetReaderType reader_type);

    # # Safety
    # - Assumes `reader` is a valid `*mut ParquetReader<Struct>` where the struct
    # has a corresponding ParquetType enum.
    CVec parquet_reader_next_chunk(void *reader,
                                   ParquetType parquet_type,
                                   ParquetReaderType reader_type);

    # TODO: Is this needed?
    #
    # # Safety
    # - Assumes `chunk` is a valid `ptr` pointer to a contiguous array.
    void *parquet_reader_index_chunk(CVec chunk, ParquetType reader_type, uintptr_t index);

    # # Safety
    # - Assumes `chunk` is a valid `ptr` pointer to a contiguous array.
    void parquet_reader_drop_chunk(CVec chunk, ParquetType parquet_type);
