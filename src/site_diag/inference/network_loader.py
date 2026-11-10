from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any

from pgmpy.factors.discrete import TabularCPD

try:  # pgmpy >= 1.0
    from pgmpy.models import DiscreteBayesianNetwork as _BayesianModel
except ImportError:  # более старые версии
    from pgmpy.models import BayesianNetwork as _BayesianModel

from site_diag.exceptions import ModelError

log = logging.getLogger(__name__)

CAUSE_NODE = "Cause"
SYMPTOM_STATES = ["absent", "present"]  # порядок важен: индекс 1 = present


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ModelError(message)


def _is_probability(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0.0 < value < 1.0
    )


def _validate(data: Any) -> tuple[str, list[str], list[str], dict, dict]:
    """Проверяет JSON и возвращает (version, causes, symptoms, priors, cpts)."""
    _require(isinstance(data, dict), "Корень файла модели должен быть объектом")
    for key in ("version", "nodes", "edges", "priors", "cpts"):
        _require(key in data, f"В файле модели нет раздела {key!r}")

    version = data["version"]
    _require(
        isinstance(version, str) and version.strip(),
        "Версия модели (version) должна быть непустой строкой",
    )

    nodes = data["nodes"]
    _require(isinstance(nodes, dict), "Раздел nodes должен быть объектом")
    _require(CAUSE_NODE in nodes, f"В nodes нет узла {CAUSE_NODE!r}")

    cause_node = nodes[CAUSE_NODE]
    _require(
        isinstance(cause_node, dict) and cause_node.get("type") == "cause",
        f"Узел {CAUSE_NODE} должен иметь тип 'cause'",
    )
    causes = cause_node.get("states")
    _require(
        isinstance(causes, list)
        and len(causes) >= 2
        and all(isinstance(c, str) and c for c in causes)
        and len(set(causes)) == len(causes),
        "Состояния Cause: ожидается список из 2+ уникальных непустых строк",
    )

    symptoms: list[str] = []
    for name, node in nodes.items():
        if name == CAUSE_NODE:
            continue
        _require(
            isinstance(node, dict) and node.get("type") == "symptom",
            f"Узел {name!r}: допустим только тип 'symptom'",
        )
        _require(
            node.get("states") == SYMPTOM_STATES,
            f"Узел {name!r}: состояния должны быть {SYMPTOM_STATES}",
        )
        symptoms.append(name)
    _require(symptoms, "В модели нет ни одного симптома")
    _require(
        not set(symptoms) & set(causes),
        "Коды причин и симптомов пересекаются",
    )

    # структура «звезда»: ровно по одному ребру Cause -> симптом
    edges = data["edges"]
    _require(
        isinstance(edges, list)
        and all(isinstance(e, list) and len(e) == 2 for e in edges),
        "Раздел edges должен быть списком пар [родитель, потомок]",
    )
    edge_set = {tuple(e) for e in edges}
    _require(len(edge_set) == len(edges), "В edges есть повторяющиеся рёбра")
    expected = {(CAUSE_NODE, s) for s in symptoms}
    _require(
        edge_set == expected,
        "Рёбра должны быть ровно Cause -> каждый симптом. "
        f"Лишние: {sorted(edge_set - expected)}; отсутствуют: {sorted(expected - edge_set)}",
    )

    priors = data["priors"]
    _require(isinstance(priors, dict), "Раздел priors должен быть объектом")
    _require(
        set(priors) == set(causes),
        f"Ключи priors не совпадают с состояниями Cause: {sorted(set(priors) ^ set(causes))}",
    )
    bad = [c for c, p in priors.items() if not _is_probability(p)]
    _require(not bad, f"Приоры вне интервала (0, 1): {bad}")
    total = sum(priors.values())
    _require(math.isclose(total, 1.0, abs_tol=1e-6), f"Сумма приоров {total}, ожидается 1")

    cpts = data["cpts"]
    _require(isinstance(cpts, dict), "Раздел cpts должен быть объектом")
    _require(
        set(cpts) == set(symptoms),
        f"CPT заданы не для всех симптомов: {sorted(set(cpts) ^ set(symptoms))}",
    )
    for s in symptoms:
        row = cpts[s]
        _require(isinstance(row, dict), f"CPT симптома {s!r} должна быть объектом")
        _require(
            set(row) == set(causes),
            f"CPT {s!r}: набор причин не совпадает: {sorted(set(row) ^ set(causes))}",
        )
        bad = [c for c, p in row.items() if not _is_probability(p)]
        _require(not bad, f"CPT {s!r}: значения вне интервала (0, 1): {bad}")

    return version, causes, symptoms, priors, cpts


def load_network(path: Path) -> tuple[Any, str]:
    """Загружает модель из JSON. Возвращает (модель pgmpy, версия модели).

    ModelError: файл не найден, повреждён, не прошёл проверку структуры
    или `check_model()`.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ModelError(f"Файл модели не найден: {path}") from None
    except OSError as exc:
        raise ModelError(f"Не удалось прочитать файл модели {path}: {exc}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ModelError(f"Файл модели {path} не является корректным JSON: {exc}") from exc

    version, causes, symptoms, priors, cpts = _validate(data)

    try:
        model = _BayesianModel([(CAUSE_NODE, s) for s in symptoms])

        cpds = [
            TabularCPD(
                variable=CAUSE_NODE,
                variable_card=len(causes),
                values=[[priors[c]] for c in causes],
                state_names={CAUSE_NODE: causes},
            )
        ]
        for s in symptoms:
            present = [cpts[s][c] for c in causes]
            absent = [1.0 - p for p in present]
            cpds.append(
                TabularCPD(
                    variable=s,
                    variable_card=2,
                    values=[absent, present],  # строки: absent, present
                    evidence=[CAUSE_NODE],
                    evidence_card=[len(causes)],
                    state_names={s: SYMPTOM_STATES, CAUSE_NODE: causes},
                )
            )
        model.add_cpds(*cpds)

        if not model.check_model():
            raise ModelError("pgmpy: check_model() вернул False")
    except ModelError:
        raise
    except Exception as exc:  # noqa: BLE001 - ошибки pgmpy разных типов
        raise ModelError(f"Модель не прошла проверку pgmpy: {exc}") from exc

    log.info(
        "Модель загружена: версия %s, причин %d, симптомов %d",
        version, len(causes), len(symptoms),
    )
    return model, version
