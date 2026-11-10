from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from typing import Any

import numpy as np
from pgmpy.inference import VariableElimination

from site_diag.exceptions import InferenceError, ValidationError
from site_diag.inference.network_loader import CAUSE_NODE, SYMPTOM_STATES

log = logging.getLogger(__name__)


class InferenceEngine:
    """Точный вывод (исключение переменных) по загруженной байесовской сети."""

    def __init__(self, model: Any, version: str):
        self._model = model
        self._version = version

        cause_cpd = model.get_cpds(CAUSE_NODE)
        self._causes: tuple[str, ...] = tuple(cause_cpd.state_names[CAUSE_NODE])
        self._priors: dict[str, float] = dict(
            zip(self._causes, map(float, np.asarray(cause_cpd.values, dtype=float).reshape(-1)))
        )

        self._symptoms: frozenset[str] = frozenset(
            n for n in model.nodes() if n != CAUSE_NODE
        )
        # P(симптом = present | причина): нужно, чтобы распознавать невозможное
        # свидетельство до обращения к pgmpy, поведение которого на нуле зависит от версии
        self._present: dict[str, dict[str, float]] = {}
        for s in self._symptoms:
            cpd = model.get_cpds(s)
            row = cpd.state_names[s].index("present")
            values = np.asarray(cpd.values, dtype=float)[row]
            self._present[s] = dict(zip(cpd.state_names[CAUSE_NODE], map(float, values)))

        self._inference = VariableElimination(model)
        # Worker из QThreadPool может вызывать posterior() из разных потоков,
        # а потокобезопасность объекта pgmpy не гарантирована
        self._lock = threading.Lock()

    @property
    def version(self) -> str:
        return self._version

    @property
    def cause_codes(self) -> tuple[str, ...]:
        return self._causes

    @property
    def symptom_codes(self) -> frozenset[str]:
        return self._symptoms

    def _check_evidence(self, evidence: Mapping[str, str]) -> None:
        for node, state in evidence.items():
            if node not in self._symptoms:
                raise ValidationError(f"Неизвестный симптом в свидетельстве: {node!r}")
            if state not in SYMPTOM_STATES:
                raise ValidationError(
                    f"Недопустимое состояние {state!r} симптома {node!r}"
                )

    def evidence_possible(self, evidence: Mapping[str, str]) -> bool:
        """False, если вероятность такого набора свидетельств равна нулю.

        P(свидетельства) = сумма по причинам: приор * произведение P(симптом | причина).
        При нуле апостериорное распределение не определено.
        """
        self._check_evidence(evidence)
        total = 0.0
        for cause in self._causes:
            p = self._priors[cause]
            for node, state in evidence.items():
                p_present = self._present[node][cause]
                p *= p_present if state == "present" else 1.0 - p_present
            total += p
        return total > 0.0

    def posterior(self, evidence: Mapping[str, str]) -> dict[str, float]:
        """Апостериорное распределение по причинам при заданных свидетельствах.

        evidence: {код симптома: 'present'} (см. build_evidence). Пустой
        словарь даёт приоры. Сумма значений равна 1, порядок ключей совпадает
        с порядком состояний Cause в модели.

        Если набор свидетельств невозможен (вероятность нуль), исключение не
        выбрасывается: возвращаются приоры, а вызывающий код узнаёт о ситуации
        через evidence_possible() и присваивает результату категорию
        «недостаточно данных».

        ValidationError: неизвестный симптом или состояние.
        InferenceError: сбой самого вывода.
        """
        if not self.evidence_possible(evidence):
            log.warning(
                "Свидетельства %s невозможны (вероятность 0), возвращены приоры",
                dict(evidence),
            )
            return {c: self._priors[c] for c in self._causes}

        try:
            with self._lock:
                factor = self._inference.query(
                    [CAUSE_NODE], evidence=dict(evidence), show_progress=False
                )
            names = factor.state_names[CAUSE_NODE]
            values = [float(v) for v in factor.values]
        except Exception as exc:  # noqa: BLE001 - ошибки pgmpy разных типов
            raise InferenceError(
                f"Сбой вывода при свидетельствах {dict(evidence)}: {exc}"
            ) from exc

        result = dict(zip(names, values))
        if set(result) != set(self._causes):
            raise InferenceError("Состояния Cause в результате не совпадают с моделью")
        return {c: result[c] for c in self._causes}
