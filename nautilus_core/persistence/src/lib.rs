// -------------------------------------------------------------------------------------------------
//  Copyright (C) 2015-2022 Nautech Systems Pty Ltd. All rights reserved.
//  https://nautechsystems.io
//
//  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
//  You may not use this file except in compliance with the License.
//  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
//
//  Unless required by applicable law or agreed to in writing, software
//  distributed under the License is distributed on an "AS IS" BASIS,
//  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//  See the License for the specific language governing permissions and
//  limitations under the License.
// -------------------------------------------------------------------------------------------------

pub mod parquet;

use std::ffi::c_void;

use nautilus_core::cvec::CVec;
use nautilus_model::data::tick::{QuoteTick, TradeTick};
use parquet::{GroupFilterArg, ParquetReader, ParquetType};
use pyo3::prelude::*;

#[pyclass]
struct PythonParquetReader {
    reader: *mut c_void,
    tag: ParquetType,
}

/// Empty derivation for Send to satisfy `pyclass` requirements
/// however this is only designed for single threaded use for now
unsafe impl Send for PythonParquetReader {}

#[pymethods]
impl PythonParquetReader {
    #[new]
    fn new(file_path: &str, chunk_size: usize, tag: ParquetType) -> Self {
        let reader = match tag {
            ParquetType::QuoteTick => {
                let reader =
                    ParquetReader::<QuoteTick>::new(file_path, chunk_size, GroupFilterArg::None);
                let reader = Box::new(reader);
                Box::into_raw(reader) as *mut c_void
            }
            ParquetType::TradeTick => {
                let reader =
                    ParquetReader::<TradeTick>::new(file_path, chunk_size, GroupFilterArg::None);
                let reader = Box::new(reader);
                Box::into_raw(reader) as *mut c_void
            }
        };

        PythonParquetReader { reader, tag }
    }

    /// the reader implements an iterator
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    /// each iteration returns a chunk of values read from the parquet file
    fn __next__(mut slf: PyRefMut<'_, Self>) -> Option<PyObject> {
        let chunk: Option<CVec> = match slf.tag {
            ParquetType::QuoteTick => {
                let reader = unsafe { &mut *(slf.reader as *mut ParquetReader<QuoteTick>) };
                reader.next().map_or_else(|| None, |data| Some(data.into()))
            }
            ParquetType::TradeTick => {
                let reader = unsafe { &mut *(slf.reader as *mut ParquetReader<TradeTick>) };
                reader.next().map_or_else(|| None, |data| Some(data.into()))
            }
        };

        match chunk {
            Some(cvec) => {
                println!("{}", cvec.len);
                Python::with_gil(|py| Some(cvec.into_py(py)))
            }
            None => {
                println!("{}", 0);
                None
            }
        }
    }

    /// After reading is complete the reader must be dropped, otherwise it will
    /// leak memory and resources
    fn drop(mut slf: PyRefMut<'_, Self>) {
        match slf.tag {
            ParquetType::QuoteTick => {
                let reader = unsafe { Box::from_raw(slf.reader as *mut ParquetReader<QuoteTick>) };
                drop(reader);
            }
            ParquetType::TradeTick => {
                let reader = unsafe { Box::from_raw(slf.reader as *mut ParquetReader<TradeTick>) };
                drop(reader);
            }
        }
    }

    /// Chunks generated by iteration must be dropped after use, otherwise
    /// it will leak memory
    fn drop_chunk(slf: PyRef<'_, Self>, chunk: CVec) {
        let CVec { ptr, len, cap } = chunk;
        match slf.tag {
            ParquetType::QuoteTick => {
                let data: Vec<u64> = unsafe { Vec::from_raw_parts(ptr as *mut u64, len, cap) };
                drop(data);
            }
            ParquetType::TradeTick => {
                let data: Vec<u64> = unsafe { Vec::from_raw_parts(ptr as *mut u64, len, cap) };
                drop(data);
            }
        }
    }
}

#[pymodule]
fn nautilus_persistence(_: Python<'_>, m: &PyModule) -> PyResult<()> {
    m.add_class::<PythonParquetReader>()?;
    m.add_class::<ParquetType>()?;
    m.add_class::<CVec>()?;
    Ok(())
}
