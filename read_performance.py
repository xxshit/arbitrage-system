"""Additive indexes for PUBLIC read paths. Never changes data or global MySQL."""
from functools import lru_cache
from sqlalchemy import Index, func, inspect, text


@lru_cache(maxsize=8)
def wallet_symbol_case_insensitive(engine):
    if engine.dialect.name != "mysql":
        return False
    with engine.connect() as connection:
        collation = connection.execute(text("SELECT COLLATION_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='transfer_network_snapshot' AND COLUMN_NAME='symbol'")).scalar()
    return bool(collation and collation.endswith("_ci"))


def wallet_symbol_predicate(engine, column, symbols):
    # Existing (exchange, symbol) UNIQUE already supports case-insensitive lookup
    # on production MySQL. Preserve UPPER fallback for binary/SQLite schemas.
    return (column if wallet_symbol_case_insensitive(engine) else func.upper(column)).in_(symbols)


def read_indexes(metadata):
    definitions = (
        ("basis_tracking", "ix_basis_active_peak", ("resolved_at", "max_abs_basis")),
        ("basis_tracking", "ix_basis_latest_path", ("strategy", "symbol", "started_at", "id")),
        ("basis_expansion_log", "ix_basis_log_time", ("tracking_id", "created_at", "id")),
        ("alert_pulse", "ix_pulse_revision_event", ("updated_at", "alert_event_id")),
    )
    result = []
    for table_name, name, columns in definitions:
        table = metadata.tables[table_name]
        existing = next((index for index in table.indexes if index.name == name), None)
        result.append(existing if existing is not None else Index(name, *(table.c[column] for column in columns)))
    return result


def install_read_indexes(connection, metadata):
    """Run explicitly AFTER a verified backup. MySQL must support online DDL."""
    installed = []
    for index in read_indexes(metadata):
        if connection.dialect.name == "mysql":
            exists = connection.execute(text("SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=DATABASE() AND table_name=:table AND index_name=:name"), {"table": index.table.name, "name": index.name}).scalar()
        elif connection.dialect.name == "sqlite":
            exists = connection.execute(text("SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name=:name"), {"name": index.name}).scalar()
        else:
            exists = index.name in {row["name"] for row in inspect(connection).get_indexes(index.table.name)}
        if exists:
            continue
        if connection.dialect.name == "mysql":
            from sqlalchemy.schema import CreateIndex
            ddl = str(CreateIndex(index).compile(dialect=connection.dialect))
            connection.execute(text("SET SESSION lock_wait_timeout=5"))
            connection.execute(text(ddl + " ALGORITHM=INPLACE LOCK=NONE"))
        else:
            index.create(connection)
        installed.append(index.name)
    return installed
