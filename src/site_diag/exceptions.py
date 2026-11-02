class AppError(Exception):
    """Базовое исключение приложения.

    user_message: короткий текст для пользователя, без технических деталей.
    Полная информация (трассировка, причина) попадает только в лог.
    """

    user_message = "Произошла ошибка приложения."

    def __init__(self, message: str | None = None):
        super().__init__(message or self.user_message)


class ConfigError(AppError):
    """Отсутствует или некорректна конфигурация (.env, переменные окружения)."""

    user_message = "Ошибка конфигурации приложения."


class DatabaseUnavailableError(AppError):
    """База данных недоступна (контейнер остановлен, неверные параметры подключения)."""

    user_message = "База данных недоступна."


class ModelError(AppError):
    """Файл модели отсутствует, повреждён или не согласован со справочниками БД."""

    user_message = "Ошибка модели диагностики."


class ValidationError(AppError):
    """Некорректный ввод пользователя (пустой набор симптомов, неизвестный код, длинный текст)."""

    user_message = "Некорректные данные."


class InferenceError(AppError):
    """Сбой байесовского вывода."""

    user_message = "Не удалось выполнить диагностику."


class AccessDeniedError(AppError):
    """Неверные учётные данные или недостаточно прав."""

    user_message = "Неверный логин или пароль."
