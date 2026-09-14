from logging.config import fileConfig

from alembic import context
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from shared.alembic_target_safety import (
    AlembicTarget,
    resolve_alembic_target,
    validate_connected_target,
    validate_offline_target,
)
from shared.config import get_settings
from shared.models import Base


config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

settings = get_settings()
target: AlembicTarget = resolve_alembic_target(
    application_database_url=settings.database_url,
    postgres_test_database_url=settings.postgres_test_database_url,
    rehearsal_database_url=settings.factory_rehearsal_database_url,
    operator_gate_rehearsal_database_url=settings.operator_gate_rehearsal_database_url,
)
config.set_main_option("sqlalchemy.url", target.database_url)
target_metadata = Base.metadata


def _target_revision() -> str:
    requested_revision = getattr(getattr(config, "cmd_opts", None), "revision", None)
    if requested_revision:
        return requested_revision
    heads = ScriptDirectory.from_config(config).get_heads()
    return ", ".join(heads) if heads else "<none>"


def _current_revision(connection) -> str:
    if not inspect(connection).has_table("alembic_version"):
        return "<none>"
    revisions = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    return ", ".join(revisions) if revisions else "<none>"


def _log_target(*, database_name: str, current_revision: str) -> None:
    # Deliberately log only the confirmed database name: never credentials or URL.
    config.print_stdout(f"ALEMBIC TARGET DATABASE: {database_name}")
    config.print_stdout(f"CURRENT REVISION: {current_revision}")
    config.print_stdout(f"TARGET REVISION: {_target_revision()}")
    config.print_stdout(f"MODE: {target.mode}")
    config.print_stdout(f"PRODUCTION ALLOWED: {str(target.production_allowed).lower()}")


def run_migrations_offline():
    validate_offline_target(target)
    _log_target(
        database_name=target.url_database_name,
        current_revision="<offline unavailable>",
    )
    context.configure(
        url=target.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    from sqlalchemy import engine_from_config, pool

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        database_name = connection.execute(text("SELECT current_database()")).scalar_one()
        validate_connected_target(target, database_name)
        _log_target(database_name=database_name, current_revision=_current_revision(connection))
        # The safety SELECTs above start a PostgreSQL read transaction. End it before
        # Alembic owns the migration transaction, otherwise its DDL is rolled back on
        # connection close instead of being committed by context.begin_transaction().
        connection.rollback()
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
