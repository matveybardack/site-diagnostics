from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

# Категории определённости совпадают со значениями CHECK в diagnosis_requests
Certainty = Literal["established", "probable", "insufficient"]


@dataclass(frozen=True, slots=True)
class Symptom:
    code: str
    label: str
    group: str
    sort_order: int


@dataclass(frozen=True, slots=True)
class Cause:
    code: str
    label: str
    base_severity: int  # 1-5


@dataclass(frozen=True, slots=True)
class User:
    id: int
    login: str
    role: str  # 'admin' или 'operator'
    password_hash: str = field(repr=False)  # не попадает в логи и repr


@dataclass(frozen=True, slots=True)
class CauseScore:
    code: str
    probability: float  # 0..1


@dataclass(frozen=True, slots=True)
class DiagnosisResult:
    scores: tuple[CauseScore, ...]  # все причины, по убыванию вероятности
    leader_code: str
    confidence: float  # вероятность лидера, 0..1
    certainty: Certainty
    importance: int  # 1-5
    explanation: tuple[str, ...]  # 0-3 строки
    model_version: str
    # пары несовместимых симптомов, которые ввёл пользователь (I-3)
    conflicts: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class RequestRecord:
    """Сохранённый запрос. В списке scores пуст, get_by_id заполняет его."""

    id: int
    submitted_at: datetime
    free_text: str | None
    top_cause_code: str
    confidence: float
    importance: int
    certainty_category: Certainty
    model_version: str
    symptom_codes: tuple[str, ...] = ()
    scores: tuple[CauseScore, ...] = ()


@dataclass(frozen=True, slots=True)
class ReportFilter:
    """Условия отбора для отчётов. None означает «без ограничения»."""

    date_from: datetime | None = None
    date_to: datetime | None = None  # правая граница не включается
    cause_code: str | None = None
    min_confidence: float | None = None
    max_confidence: float | None = None
    min_importance: int | None = None
    max_importance: int | None = None


@dataclass(frozen=True, slots=True)
class HistogramPoint:
    bucket: int | date  # час 0-23 или календарная дата
    count: int

SortColumn = Literal["submitted_at", "top_cause_code", "confidence", "importance"]


@dataclass(frozen=True, slots=True)
class SortSpec:
    """Сортировка списка запросов. Колонка проверяется в слое данных."""

    column: SortColumn
    descending: bool = True

HistogramMode = Literal["hour", "day"]


@dataclass(frozen=True, slots=True)
class RequestPage:
    items: tuple[RequestRecord, ...]
    total: int  # всего записей по фильтру
    page: int  # номер страницы, с 0
    page_size: int


@dataclass(frozen=True, slots=True)
class ReportSummary:
    total: int
    avg_confidence: float | None  # None, если запросов нет
    cause_counts: tuple[tuple[str, int], ...]  # (код причины, число), по убыванию числа
    top_important: tuple[RequestRecord, ...]
