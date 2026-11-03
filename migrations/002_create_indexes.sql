-- 002: индексы под запросы отчётов администратора

-- Фильтр по периоду (ReportFilter), агрегации по часам и дням (counts_by_hour, counts_by_day)
CREATE INDEX idx_requests_submitted_at
    ON diagnosis_requests (submitted_at);

-- Фильтр по причине, распределение по причинам (cause_distribution)
CREATE INDEX idx_requests_top_cause
    ON diagnosis_requests (top_cause_code);

-- Сортировка по умолчанию «важность ↓, время ↓» (list_requests, top_important)
-- и фильтр по важности: подходит ведущая колонка importance
CREATE INDEX idx_requests_importance_time
    ON diagnosis_requests (importance DESC, submitted_at DESC);

-- Выборка «сколько запросов содержали симптом X»
-- (поиск по request_id уже покрыт первичным ключом таблицы)
CREATE INDEX idx_request_symptoms_symptom
    ON request_symptoms (symptom_code);
