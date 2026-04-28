"""
Orquestração de campanhas persistentes: CRUD, transições de estado,
loop do worker (daemon thread por campanha ativa), reidratação no boot.

A tabela `campanhas` (em db.py) é a fonte da verdade do estado persistente.
O estado runtime das threads vivas fica em `_threads_runtime` (memória).
"""
from __future__ import annotations


def parse_dias_semana(s: str) -> set[int]:
    """Converte CSV '0,1,2' em set {0,1,2}. 0=segunda, 6=domingo."""
    if not s.strip():
        return set()
    out: set[int] = set()
    for tok in s.split(","):
        tok = tok.strip()
        v = int(tok)  # ValueError se não-inteiro
        if v < 0 or v > 6:
            raise ValueError(f"Dia da semana fora do intervalo 0-6: {v}")
        out.add(v)
    return out


def format_dias_semana(dias: set[int]) -> str:
    return ",".join(str(d) for d in sorted(dias))
