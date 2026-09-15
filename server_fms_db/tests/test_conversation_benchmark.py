import pytest

from benchmarks.conversation.benchmark_conversation import (
    DEFAULT_DATASET,
    assert_benchmark_database,
    load_dataset,
    percentile,
)


def test_conversation_dataset_v2_is_frozen_shape() -> None:
    dataset = load_dataset(DEFAULT_DATASET)

    assert dataset.version == "v2"
    assert len(dataset.scenarios) == 30
    assert sum(len(scenario["turns"]) for scenario in dataset.scenarios) > 30


def test_conversation_benchmark_refuses_non_benchmark_database() -> None:
    assert assert_benchmark_database(
        "postgresql+psycopg://user:secret@localhost/smart_factory_benchmark",
        "smart_factory_benchmark",
    ).database == "smart_factory_benchmark"

    with pytest.raises(ValueError, match="Benchmark DB safety guard"):
        assert_benchmark_database(
            "postgresql+psycopg://user:secret@localhost/smart_factory_db",
            "smart_factory_benchmark",
        )


def test_percentile_uses_linear_interpolation() -> None:
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5
    assert percentile([1.0, 2.0, 3.0, 4.0], 95) == pytest.approx(3.85)
