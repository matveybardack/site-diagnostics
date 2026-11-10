from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import datetime

from site_diag.data.reference_repository import ReferenceRepository
from site_diag.data.audit_repository import AuditEvent, AuditRepository
from site_diag.data.diagnosis_repository import DiagnosisRepository
from site_diag.domain.models import DiagnosisResult
from site_diag.exceptions import ModelError, ValidationError
from site_diag.inference.engine import InferenceEngine
from site_diag.inference.evidence import build_evidence, find_conflicts
from site_diag.inference.explanation import explain
from site_diag.inference.interpretation import Thresholds, categorize, importance
from site_diag.inference.ranking import confidence, rank

log = logging.getLogger(__name__)

DEFAULT_MAX_FREE_TEXT = 1000

_IMPOSSIBLE_TEXT = (
    "Отмеченные симптомы несовместимы с моделью: такое сочетание в ней "
    "невозможно, поэтому результат ненадёжен."
)

def _diff(title: str, db_codes: set[str], model_codes: set[str]) -> str | None:
    only_db = sorted(db_codes - model_codes)
    only_model = sorted(model_codes - db_codes)
    if not only_db and not only_model:
        return None
    parts = []
    if only_db:
        parts.append("только в БД: " + ", ".join(only_db))
    if only_model:
        parts.append("только в модели: " + ", ".join(only_model))
    return f"{title} ({'; '.join(parts)})"

class DiagnosisService:
    """Диагностика: принять симптомы, вывести причину, при необходимости сохранить.

    Тексты ValidationError написаны для пользователя: форма показывает их как есть.
    """

    def __init__(
        self,
        engine: InferenceEngine,
        thresholds: Thresholds,
        diagnosis_repo: DiagnosisRepository,
        audit_repo: AuditRepository,
        *,
        symptom_labels: Mapping[str, str] | None = None,
        cause_labels: Mapping[str, str] | None = None,
        max_free_text_length: int = DEFAULT_MAX_FREE_TEXT,
        reference_repo: ReferenceRepository | None = None,
    ):
        self._engine = engine
        self._thresholds = thresholds
        self._diagnosis_repo = diagnosis_repo
        self._audit_repo = audit_repo
        self._symptom_labels = dict(symptom_labels or {})
        self._cause_labels = dict(cause_labels or {})
        self._max_text = max_free_text_length
        self._reference_repo = reference_repo

    # проверка ввода

    def _evidence(self, codes: Iterable[str]) -> tuple[list[str], dict[str, str]]:
        if isinstance(codes, str):
            raise ValidationError("Симптомы должны передаваться списком кодов.")
        codes = list(codes)
        evidence = build_evidence(codes, self._engine.symptom_codes)
        if not evidence:
            raise ValidationError("Выберите хотя бы один симптом.")
        return codes, evidence

    def _check_text(self, free_text: str | None) -> None:
        if free_text is None:
            return
        if not isinstance(free_text, str):
            raise ValidationError("Текст описания должен быть строкой.")
        length = len(free_text.strip())
        if length > self._max_text:
            raise ValidationError(
                f"Текст описания слишком длинный: не более {self._max_text} "
                f"символов (сейчас {length})."
            )

    def validate(self, codes: Iterable[str], free_text: str | None = None) -> None:
        """ValidationError: нет симптомов, неизвестный код, слишком длинный текст."""
        self._evidence(codes)
        self._check_text(free_text)

    # согласованность

    def check_consistency(self) -> None:
        """Сверяет коды БД, модели и файла порогов. Вызывается при старте.

        ModelError: перечисляет все найденные расхождения сразу.
        DatabaseUnavailableError: БД недоступна (обрабатывается при старте, I-1).
        """
        if self._reference_repo is None:
            raise RuntimeError("check_consistency() требует reference_repo")

        db_symptoms = {s.code for s in self._reference_repo.list_symptoms()}
        db_causes = {c.code for c in self._reference_repo.list_causes()}
        model_symptoms = set(self._engine.symptom_codes)
        model_causes = set(self._engine.cause_codes)

        problems = [
            p
            for p in (
                _diff("симптомы", db_symptoms, model_symptoms),
                _diff("причины", db_causes, model_causes),
            )
            if p
        ]

        severity_codes = set(self._thresholds.severity)
        if severity_codes != model_causes:
            problems.append(
                "тяжесть в thresholds.json задана не для тех причин, что в модели "
                f"(нет: {', '.join(sorted(model_causes - severity_codes)) or '-'}; "
                f"лишние: {', '.join(sorted(severity_codes - model_causes)) or '-'})"
            )

        unknown = sorted(
            {c for pair in self._thresholds.conflicts for c in pair} - model_symptoms
        )
        if unknown:
            problems.append(
                "в парах конфликтов thresholds.json есть неизвестные симптомы: "
                + ", ".join(unknown)
            )

        if problems:
            raise ModelError(
                "Модель не согласована со справочниками: " + "; ".join(problems)
            )
        log.info(
            "Справочники согласованы с моделью: %d симптомов, %d причин",
            len(model_symptoms), len(model_causes),
        )

    # диагностика

    def diagnose(
        self, codes: Iterable[str], free_text: str | None = None
    ) -> DiagnosisResult:
        """Результат диагностики. Ничего не пишет в БД, не зависит от Qt."""
        codes, evidence = self._evidence(codes)
        self._check_text(free_text)

        possible = self._engine.evidence_possible(evidence)
        ranked = rank(self._engine.posterior(evidence))
        conf = confidence(ranked)

        # невозможное для модели сочетание симптомов: всегда «недостаточно данных»
        certainty = (
            categorize(conf, self._thresholds) if possible else "insufficient"
        )
        explanation = (
            explain(
                self._engine,
                evidence,
                conf.leader_code,
                symptom_labels=self._symptom_labels,
                cause_labels=self._cause_labels,
            )
            if possible
            else (_IMPOSSIBLE_TEXT,)
        )

        result = DiagnosisResult(
            scores=tuple(ranked),
            leader_code=conf.leader_code,
            confidence=conf.probability,
            certainty=certainty,
            importance=importance(conf.leader_code, conf.probability, self._thresholds),
            explanation=explanation,
            model_version=self._engine.version,
            conflicts=find_conflicts(codes, self._thresholds.conflicts),
        )
        log.info(
            "Диагностика: лидер=%s p=%.3f категория=%s важность=%d симптомов=%d",
            result.leader_code, result.confidence, result.certainty,
            result.importance, len(evidence),
        )
        return result

    # сохранение

    def save(
        self,
        result: DiagnosisResult,
        codes: Iterable[str],
        submitted_at: datetime,
        free_text: str | None = None,
    ) -> int:
        """Сохраняет запрос и пишет событие аудита. Возвращает id запроса.

        ValidationError: те же проверки, что в validate(); naive-время.
        DatabaseUnavailableError: БД недоступна (результат расчёта не теряется,
        вызывающий код может повторить сохранение).
        """
        codes, _ = self._evidence(codes)
        self._check_text(free_text)

        request_id = self._diagnosis_repo.save(
            result, sorted(set(codes)), submitted_at, free_text
        )
        # сбой журнала не отменяет сохранение (AuditRepository.log не бросает исключений)
        self._audit_repo.log(
            AuditEvent.DIAGNOSIS_SAVED,
            None,
            {
                "request_id": request_id,
                "leader": result.leader_code,
                "certainty": result.certainty,
                "model_version": result.model_version,
            },
        )
        return request_id
