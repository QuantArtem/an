from contextvars import ContextVar

# Сквозной идентификатор запроса: приходит в заголовке X-Correlation-Id,
# попадает в логи, в конверт события и восстанавливается у потребителя.
correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")
