from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from site_diag.domain.models import Certainty
from site_diag.exceptions import InferenceError, ModelError
from site_diag.inference.ranking import Confidence

# допуск на шум плавающей арифметики при сравнении с порогами
_EPS = 1e-9


@dataclass(frozen=True, slots=True)
class Thresholds:
    established: float  # порог «причина установлена»
    probable: float  # порог «вероятные причины»
    min_gap: float  # минимальный разрыв между первой и второй причиной
    severity: dict[str, int]  # тяжесть по причинам, 1-5
    conflicts: tuple[tuple[str, str], ...]  # пары несовместимых симптомов


def load_thresholds(path: Path) -> Thresholds:
    """Читает thresholds.json. ModelError: файл не найден, повреждён или неверен."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ModelError(f"Файл порогов не найден: {path}") from None
    except OSError as exc:
        raise ModelError(f"Не удалось прочитать файл порогов {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ModelError(f"Файл порогов {path} не является корректным JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ModelError("Корень файла порогов должен быть объектом")
    for key in ("established", "probable", "min_gap", "severity", "conflicts"):
        if key not in data:
            raise ModelError(f"В файле порогов нет ключа {key!r}")

    def number(key: str) -> float:
        v = data[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v < 1:
            raise ModelError(f"Порог {key!r} должен быть числом из интервала (0, 1): {v!r}")
        return float(v)

    established, probable, min_gap = (
        number("established"), number("probable"), number("min_gap"),
    )
    if not probable < established:
        raise ModelError("Должно выполняться probable < established")

    severity = data["severity"]
    if (
        not isinstance(severity, dict)
        or not severity
        or not all(
            isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 5
            for v in severity.values()
        )
    ):
        raise ModelError("severity: ожидается объект {причина: целое число 1-5}")

    raw_conflicts = data["conflicts"]
    if not isinstance(raw_conflicts, list) or not all(
        isinstance(p, list)
        and len(p) == 2
        and all(isinstance(s, str) for s in p)
        and p[0] != p[1]
        for p in raw_conflicts
    ):
        raise ModelError("conflicts: ожидается список пар разных кодов симптомов")

    return Thresholds(
        established=established,
        probable=probable,
        min_gap=min_gap,
        severity=dict(severity),
        conflicts=tuple((a, b) for a, b in raw_conflicts),
    )


def categorize(confidence: Confidence, thresholds: Thresholds) -> Certainty:
    """Категория определённости по вероятности лидера p1 и разрыву со второй причиной.

    established: p1 >= established и разрыв >= min_gap
    probable:    p1 >= probable (если не established)
    insufficient: иначе
    """
    p1 = confidence.probability
    if p1 >= thresholds.established - _EPS and confidence.gap >= thresholds.min_gap - _EPS:
        return "established"
    if p1 >= thresholds.probable - _EPS:
        return "probable"
    return "insufficient"


def importance(leader: str, confidence: float, thresholds: Thresholds) -> int:
    """Важность поломки 1-5: тяжесть причины, на 1 ниже при слабой уверенности.

    confidence: вероятность лидера. Если она ниже порога probable, важность
    уменьшается на 1, но не опускается ниже 1.
    """
    try:
        base = thresholds.severity[leader]
    except KeyError:
        raise InferenceError(f"Для причины {leader!r} не задана тяжесть") from None
    if not isinstance(confidence, (int, float)) or not math.isfinite(confidence):
        raise InferenceError(f"Уверенность должна быть числом: {confidence!r}")

    if confidence < thresholds.probable - _EPS:
        return max(1, base - 1)
    return base
