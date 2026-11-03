-- 001: схема приложения диагностики отказа сайта

-- Роли пользователей
CREATE TABLE roles (
    id    smallint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    code  text NOT NULL UNIQUE CHECK (code IN ('admin', 'operator')),
    label text NOT NULL
);

CREATE TABLE users (
    id            integer PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    login         text NOT NULL UNIQUE CHECK (char_length(login) BETWEEN 1 AND 64),
    password_hash text NOT NULL,
    role_id       smallint NOT NULL REFERENCES roles (id),
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- Справочники. Код (латиница, snake_case) совпадает с кодом узла в модели сети
CREATE TABLE symptoms (
    code       text PRIMARY KEY CHECK (code ~ '^[a-z][a-z0-9_]{0,39}$'),
    label      text NOT NULL,
    group_name text NOT NULL,
    sort_order integer NOT NULL UNIQUE
);

CREATE TABLE causes (
    code          text PRIMARY KEY CHECK (code ~ '^[a-z][a-z0-9_]{0,39}$'),
    label         text NOT NULL,
    base_severity smallint NOT NULL CHECK (base_severity BETWEEN 1 AND 5)
);

-- Запрос диагностики
CREATE TABLE diagnosis_requests (
    id                 bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    submitted_at       timestamptz NOT NULL,              -- момент заполнения формы
    saved_at           timestamptz NOT NULL DEFAULT now(),-- момент записи в БД
    free_text          text,
    top_cause_code     text NOT NULL REFERENCES causes (code),
    confidence         double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    importance         smallint NOT NULL CHECK (importance BETWEEN 1 AND 5),
    certainty_category text NOT NULL
                       CHECK (certainty_category IN ('established', 'probable', 'insufficient')),
    model_version      text NOT NULL
);

-- Симптомы запроса (только отмеченные пользователем)
CREATE TABLE request_symptoms (
    request_id   bigint NOT NULL REFERENCES diagnosis_requests (id) ON DELETE CASCADE,
    symptom_code text   NOT NULL REFERENCES symptoms (code),
    PRIMARY KEY (request_id, symptom_code)
);

-- Вероятности всех причин по запросу
CREATE TABLE inference_results (
    request_id  bigint NOT NULL REFERENCES diagnosis_requests (id) ON DELETE CASCADE,
    cause_code  text   NOT NULL REFERENCES causes (code),
    probability double precision NOT NULL CHECK (probability BETWEEN 0 AND 1),
    PRIMARY KEY (request_id, cause_code)
);

-- Журнал событий
CREATE TABLE audit_log (
    id         bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    event_type text NOT NULL,
    user_id    integer REFERENCES users (id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    details    jsonb
);
