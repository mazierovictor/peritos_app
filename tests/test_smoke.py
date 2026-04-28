def test_schema_cria_tabela_campanhas(db_temp):
    from app.db import get_conn
    with get_conn() as conn:
        nomes = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert "campanhas" in nomes  # vai falhar até a Task 2
