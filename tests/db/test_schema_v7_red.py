from sqlalchemy import inspect, select

from backend.db.migrations import initialize_schema
from backend.db.schema import runtime_runs, schema_version
from backend.db.session import create_db_engine


def test_fresh_schema_has_v7_and_only_the_three_new_runtime_contract_columns(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'fresh-v7.db'}")
    with engine.begin() as connection:
        initialize_schema(connection)
        assert connection.execute(select(schema_version.c.version)).scalar_one() == 7

    columns = {item["name"] for item in inspect(engine).get_columns("runtime_runs")}
    assert {
        "execution_contract_version",
        "authority_mode",
        "execution_contract",
    } <= columns

    expected = {column.name for column in runtime_runs.columns}
    assert columns == expected
