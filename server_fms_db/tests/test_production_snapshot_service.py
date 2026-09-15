from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api_server.services.production_snapshot_service import ProductionSnapshotService
from shared.models import Base
from shared.models.factory import JobStatus, Product, ProductionJob


def test_production_snapshot_uses_postgresql_model_data_and_filters_terminal_history() -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    now = datetime(2026, 8, 14, 3, 0, tzinfo=UTC)  # 12:00 Asia/Seoul
    try:
        with factory() as session:
            product = Product(product_code="HOUSE_UNITY", product_name="Unity Test House")
            session.add(product)
            session.flush()
            session.add_all([
                ProductionJob(job_code="NONTERMINAL", product_code=product.product_code, status=JobStatus.RUNNING, requested_at=now),
                ProductionJob(job_code="TODAY-COMPLETED", product_code=product.product_code, status=JobStatus.COMPLETED, requested_at=now, completed_at=now),
                ProductionJob(job_code="TODAY-CANCELED", product_code=product.product_code, status=JobStatus.CANCELED, requested_at=now, completed_at=now),
                ProductionJob(job_code="TODAY-FAILED", product_code=product.product_code, status=JobStatus.FAILED, requested_at=now - timedelta(days=1), started_at=now - timedelta(days=1), failed_at=now),
                ProductionJob(job_code="OLD-COMPLETED", product_code=product.product_code, status=JobStatus.COMPLETED, requested_at=now - timedelta(days=2), completed_at=now - timedelta(days=2)),
                ProductionJob(job_code="OLD-CANCELED", product_code=product.product_code, status=JobStatus.CANCELED, requested_at=now - timedelta(days=2), completed_at=now - timedelta(days=2)),
                ProductionJob(job_code="OLD-FAILED", product_code=product.product_code, status=JobStatus.FAILED, requested_at=now - timedelta(days=2), started_at=now - timedelta(days=2), failed_at=now - timedelta(days=2)),
            ])
            session.commit()

        snapshot = ProductionSnapshotService(factory).get_snapshot(now=now)
        expected = {"TODAY-FAILED", "TODAY-CANCELED", "TODAY-COMPLETED", "NONTERMINAL"}
        assert {job["job_code"] for job in snapshot["jobs"]} == expected
        assert snapshot["jobs"][0]["product_code"] == "HOUSE_UNITY"
        assert snapshot["jobs"][0]["control_state"] == "ACTIVE"
        assert snapshot["transports"] == []
        assert snapshot["active_errors"] == []
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
