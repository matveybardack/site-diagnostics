from __future__ import annotations

from collections.abc import Mapping

from site_diag.exceptions import InferenceError
from site_diag.inference.engine import InferenceEngine

MAX_LINES = 3
# вклад меньше 1 процентного пункта в объяснение не попадает
MIN_CONTRIBUTION = 0.01


def _pct(p: float) -> str:
    return f"{p * 100:.0f}%"


def explain(
    engine: InferenceEngine,
    evidence: Mapping[str, str],
    leader: str,
    *,
    symptom_labels: Mapping[str, str] | None = None,
    cause_labels: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Объяснение результата: 1-3 строки о симптомах, сильнее всего поддержавших лидера.

    Вклад симптома = P(лидер | все свидетельства) - P(лидер | все, кроме этого симптома).
    Пустые свидетельства дают пустой кортеж. Если ни один симптом не повышает
    вероятность лидера заметно, возвращается одна поясняющая строка.

    InferenceError: лидера нет в модели. ValidationError: неверные свидетельства.
    """
    if leader not in engine.cause_codes:
        raise InferenceError(f"Причина {leader!r} отсутствует в модели")
    if not evidence:
        return ()

    sym_names = symptom_labels or {}
    cause_name = (cause_labels or {}).get(leader, leader)

    with_all = engine.posterior(evidence)[leader]

    contributions: list[tuple[float, str, float]] = []
    for code in evidence:
        rest = {k: v for k, v in evidence.items() if k != code}
        without = engine.posterior(rest)[leader]
        contributions.append((with_all - without, code, without))

    # по убыванию вклада, при равенстве по коду (детерминированный порядок)
    contributions.sort(key=lambda item: (-item[0], item[1]))
    useful = [c for c in contributions if c[0] >= MIN_CONTRIBUTION][:MAX_LINES]

    if not useful:
        return (
            f"Ни один из отмеченных симптомов заметно не повышает вероятность "
            f"причины «{cause_name}».",
        )

    return tuple(
        f"«{sym_names.get(code, code)}»: вероятность причины «{cause_name}» "
        f"растёт с {_pct(without)} до {_pct(with_all)} "
        f"(+{delta * 100:.0f} п.п.)."
        for delta, code, without in useful
    )
