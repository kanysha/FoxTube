PYTHON ?= python3

.PHONY: help bootstrap up down logs ps check test test-browser test-resilience test-capacity agents-check doctor

help:
	@echo 'bootstrap/up  Собрать, применить миграции и запустить проект'
	@echo 'down          Остановить проект с сохранением данных'
	@echo 'logs/ps       Показать журналы/состояние сервисов'
	@echo 'check         Ruff, mypy, ShellCheck и unit-тесты в Docker'
	@echo 'test          Интеграционные тесты в отдельном окружении'
	@echo 'test-browser  Настоящий Chrome: загрузка, H.264, просмотр и перемотка'
	@echo 'test-resilience  Аварии worker/БД, удаление при FFmpeg и рестарт стека'
	@echo 'test-capacity    Загрузка 500 МиБ, память API и десять HTTP-зрителей'
	@echo 'agents-check  Проверить инструкции, профили, навыки и ссылки'
	@echo 'doctor        Проверить обвязку и показать доступность инструментов'

bootstrap up:
	bash scripts/bootstrap.sh

down:
	docker compose down

logs:
	docker compose logs --tail 100 api worker nginx

ps:
	docker compose ps

check:
	docker build --target development -t foxtube-dev:local .
	docker run --rm --network none --read-only --tmpfs /tmp:rw,noexec,nosuid,size=128m foxtube-dev:local bash scripts/check.sh

test:
	bash scripts/test.sh

test-browser:
	bash scripts/test-browser.sh

test-resilience:
	bash scripts/test-resilience.sh

test-capacity:
	bash scripts/test-resilience.sh --capacity

agents-check:
	$(PYTHON) scripts/check_agent_setup.py

doctor:
	$(PYTHON) scripts/check_agent_setup.py --tools
