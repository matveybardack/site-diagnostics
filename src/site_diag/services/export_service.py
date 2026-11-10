from __future__ import annotations

import csv
import logging
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.figure import Figure

from site_diag.domain.models import ReportSummary, RequestRecord
from site_diag.exceptions import ExportError, ValidationError

log = logging.getLogger(__name__)

CSV_HEADER = (
    "id", "время", "симптомы", "причина", "уверенность, %",
    "важность", "определённость", "версия модели", "описание",
)
_CERTAINTY_RU = {
    "established": "установлена",
    "probable": "вероятна",
    "insufficient": "недостаточно данных",
}
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_A4_PORTRAIT = (8.27, 11.69)


def _safe_cell(value: str) -> str:
    """Защита от CSV-инъекции: текст, начинающийся с символа формулы, станет текстом."""
    return "'" + value if value.startswith(_FORMULA_PREFIXES) else value


def _atomic_write(path: Path, writer: Callable[[Path], None]) -> None:
    """Пишет во временный файл рядом с целевым и подменяет целевой при успехе.

    При сбое остаётся прежний файл (или его нет), а не обрезанный.
    """
    path = Path(path)
    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        os.close(fd)
        writer(Path(tmp_name))
        os.replace(tmp_name, path)
        tmp_name = None
    except OSError as exc:
        raise ExportError(f"Не удалось записать файл {path}: {exc}") from exc
    finally:
        if tmp_name and os.path.exists(tmp_name):
            os.unlink(tmp_name)


def export_csv(
    rows: Sequence[RequestRecord],
    path: Path,
    *,
    symptom_labels: Mapping[str, str] | None = None,
    cause_labels: Mapping[str, str] | None = None,
    tz: str = "UTC",
    delimiter: str = ";",
) -> int:
    """Записывает запросы в CSV (UTF-8 с BOM, чтобы Excel распознал кириллицу).

    Возвращает число записанных строк. ValidationError: нет строк.
    ExportError: файл не записан.
    """
    if not rows:
        raise ValidationError("Нет данных для экспорта.")
    path = Path(path)
    zone = ZoneInfo(tz)
    sym = symptom_labels or {}
    cau = cause_labels or {}

    def write(tmp: Path) -> None:
        with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=delimiter)
            w.writerow(CSV_HEADER)
            for r in rows:
                w.writerow([
                    r.id,
                    r.submitted_at.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S"),
                    _safe_cell(", ".join(sym.get(c, c) for c in r.symptom_codes)),
                    _safe_cell(cau.get(r.top_cause_code, r.top_cause_code)),
                    f"{r.confidence * 100:.1f}",
                    r.importance,
                    _CERTAINTY_RU.get(r.certainty_category, r.certainty_category),
                    r.model_version,
                    _safe_cell((r.free_text or "").replace("\r", " ").replace("\n", " ")),
                ])

    _atomic_write(path, write)
    log.info("CSV экспортирован: %s, строк %d", path, len(rows))
    return len(rows)


def _summary_figure(
    summary: ReportSummary,
    cause_labels: Mapping[str, str],
    filter_description: str | None,
    tz: str,
) -> Figure:
    fig = Figure(figsize=_A4_PORTRAIT)
    ax = fig.add_axes((0.08, 0.05, 0.84, 0.88))
    ax.axis("off")
    now = datetime.now(timezone.utc).astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")

    lines: list[tuple[str, int, str]] = [
        ("Сводный отчёт по диагностике отказов сайта", 15, "bold"),
        (f"Сформирован: {now}", 9, "normal"),
        (f"Фильтры: {filter_description or 'не заданы'}", 9, "normal"),
        ("", 9, "normal"),
        (f"Всего запросов: {summary.total}", 11, "normal"),
        (
            "Средняя уверенность: "
            + (f"{summary.avg_confidence * 100:.1f}%" if summary.avg_confidence is not None else "—"),
            11, "normal",
        ),
        ("", 9, "normal"),
        ("Распределение по причинам:", 11, "bold"),
    ]
    for code, n in summary.cause_counts:
        lines.append((f"   {cause_labels.get(code, code)}: {n}", 10, "normal"))
    lines += [("", 9, "normal"), ("Наиболее важные поломки:", 11, "bold")]
    for r in summary.top_important:
        when = r.submitted_at.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")
        lines.append((
            f"   [{r.importance}] {when}  {cause_labels.get(r.top_cause_code, r.top_cause_code)}"
            f"  ({r.confidence * 100:.0f}%)", 10, "normal",
        ))

    y = 1.0
    for text, size, weight in lines:
        ax.text(0, y, text, fontsize=size, fontweight=weight, va="top", transform=ax.transAxes)
        y -= 0.011 * size / 9 + 0.012
    return fig


def export_report(
    summary: ReportSummary,
    figure: Figure,
    path: Path,
    *,
    cause_labels: Mapping[str, str] | None = None,
    filter_description: str | None = None,
    tz: str = "UTC",
) -> None:
    """Экспорт отчёта по расширению path: .pdf или .png.

    PDF: страница со сводкой и страница с графиком. PNG: только график.
    ValidationError: другое расширение. ExportError: файл не записан.
    """
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in (".pdf", ".png"):
        raise ValidationError("Поддерживаются форматы PDF и PNG.")

    def write(tmp: Path) -> None:
        if ext == ".png":
            figure.savefig(tmp, format="png", dpi=150, bbox_inches="tight")
        else:
            with PdfPages(tmp) as pdf:
                fig = _summary_figure(summary, cause_labels or {}, filter_description, tz)
                pdf.savefig(fig)
                pdf.savefig(figure, bbox_inches="tight")

    _atomic_write(path, write)
    log.info("Отчёт экспортирован: %s", path)
