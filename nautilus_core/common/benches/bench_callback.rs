use pyo3::prelude::*;

use criterion::{black_box, criterion_group, criterion_main, BenchmarkId, Criterion};

pub fn criterion_benchmark(c: &mut Criterion) {
    let mut group = c.benchmark_group("python and rust closure benching");

    // initialize python objects and module
    pyo3::prepare_freethreaded_python();
    let (callback, pymod): (PyObject, PyObject) = Python::with_gil(|py| {
        let code = include_str!("./callback.py");
        let pymod = PyModule::from_code(py, &code, "humpty", "dumpty").unwrap();
        let callback = pymod.getattr("increment").unwrap();
        (callback.into(), pymod.into())
    });

    let count = 100_000;

    // acquire gil per callback
    group.bench_with_input(
        BenchmarkId::new("acquire gil per python callback", count),
        &callback,
        |b, callback: &PyObject| {
            // acquire gil for each callback
            b.iter(|| {
                for _ in 0..count {
                    let _result = Python::with_gil(|py| callback.call0(py));
                }
            });
        },
    );

    // acquire gil for a batch of callbacks
    let callbacks: Vec<PyObject> = (0..count).into_iter().map(|_| callback.clone()).collect();
    group.bench_with_input(
        BenchmarkId::new("acquire gil per batch of python callbacks", count),
        &callbacks,
        |b, callbacks| {
            b.iter(|| {
                Python::with_gil(|py| {
                    callbacks.iter().for_each(|callback| {
                        let _result = callback.call0(py).unwrap();
                    })
                });
            })
        },
    );

    let callback_count: u64 =
        Python::with_gil(|py| pymod.getattr(py, "count").unwrap().extract(py).unwrap());

    println!("{}", callback_count);

    let mut rust_counter = 0;
    let mut rust_callback: Box<dyn FnMut()> = Box::new(|| {
        rust_counter = rust_counter + 1;
    });
    group.bench_function(BenchmarkId::new("boxed rust closure", count), move |b| {
        b.iter(|| {
            for _ in 0..count {
                rust_callback();
            }
        })
    });

    let mut rust_counter = 0;
    let mut rust_callback = || {
        rust_counter = rust_counter + 1;
    };
    group.bench_function(BenchmarkId::new("unboxed rust closure", count), move |b| {
        b.iter(|| {
            for _ in 0..count {
                black_box(rust_callback());
            }
        })
    });
}

criterion_group!(benches, criterion_benchmark);
criterion_main!(benches);
