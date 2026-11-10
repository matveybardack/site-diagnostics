from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from site_diag.domain.models import CauseScore
from site_diag.exceptions import InferenceError

# Вероятности, различающиеся меньше чем на 1e-12, считаются равными:
# иначе шум плавающей арифметики решал бы, кто лидер при точной ничьей.
_TIE_DIGITS = 12


@dataclass(frozen=True, slots=True)
class Confidence:
    """Показатели уверенности модели в лидере."""

    leader_code: str
    probability: float  # вероятность лидера
    runner_up_code: str | None  # вторая по вероятности причина (None, если причина одна)
    gap: float  # разрыв между первой и второй причиной


def rank(posterior: Mapping[str, float]) -> list[CauseScore]:
    """Причины по убыванию вероятности; при равенстве по возрастанию кода.

    InferenceError: пустое распределение, вероятность не число или вне [0, 1],
    сумма не равна 1.
    """
    if not posterior:
        raise InferenceError("Пустое распределение вероятностей")

    for code, p in posterior.items():
        if not isinstance(p, (int, float)) or isinstance(p, bool) or not math.isfinite(p):
            raise InferenceError(f"Вероятность причины {code!r} не является числом: {p!r}")
        if not 0.0 <= p <= 1.0:
            raise InferenceError(f"Вероятность причины {code!r} вне [0, 1]: {p}")

    total = sum(posterior.values())
    if not math.isclose(total, 1.0, abs_tol=1e-6):
        raise InferenceError(f"Сумма вероятностей причин {total}, ожидается 1")

    ordered = sorted(
        posterior.items(),
        key=lambda item: (-round(item[1], _TIE_DIGITS), item[0]),
    )
    return [CauseScore(code, float(p)) for code, p in ordered]


def confidence(ranked: Sequence[CauseScore]) -> Confidence:
    """Вероятность лидера и разрыв со второй причиной.

    ranked: результат rank(). Если причина одна, разрыв равен вероятности лидера.
    """
    if not ranked:
        raise InferenceError("Нет причин для расчёта уверенности")

    leader = ranked[0]
    if len(ranked) == 1:
        return Confidence(leader.code, leader.probability, None, leader.probability)

    runner_up = ranked[1]
    return Confidence(
        leader_code=leader.code,
        probability=leader.probability,
        runner_up_code=runner_up.code,
        gap=leader.probability - runner_up.probability,
    )
