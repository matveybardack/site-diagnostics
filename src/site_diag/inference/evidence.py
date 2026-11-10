from __future__ import annotations

from collections.abc import Collection, Iterable

from site_diag.exceptions import ValidationError

PRESENT = "present"


def build_evidence(
    codes: Iterable[str],
    known_symptoms: Collection[str],
) -> dict[str, str]:
    """Свидетельства для сети: {код симптома: 'present'}."""
    selected = list(codes)

    not_strings = [c for c in selected if not isinstance(c, str)]
    if not_strings:
        raise ValidationError(f"Код симптома должен быть строкой: {not_strings!r}")

    known = set(known_symptoms)
    unknown = sorted({c for c in selected if c not in known})
    if unknown:
        raise ValidationError(f"Неизвестные коды симптомов: {', '.join(unknown)}")

    return {code: PRESENT for code in sorted(set(selected))}

def find_conflicts(
    codes: Iterable[str],
    conflicts: Collection[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    """Пары несовместимых симптомов, которые оба отмечены пользователем.

    conflicts: пары из thresholds.json. Порядок результата совпадает с порядком
    пар в конфигурации, каждая пара возвращается не больше одного раза.
    Это предупреждение, а не ошибка: расчёт по таким симптомам выполняется.
    """
    selected = set(codes)
    found: list[tuple[str, str]] = []
    for a, b in conflicts:
        pair = (a, b)
        if a in selected and b in selected and pair not in found:
            found.append(pair)
    return tuple(found)
