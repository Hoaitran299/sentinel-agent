.PHONY: install install-s3 db-init telegram-test test test-mysql watch status ui import-sqlite

PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
RUN = PYTHONPATH=src $(PYTHON) -m autofix_agent

install:
	python3 -m venv .venv
	.venv/bin/pip install -q --upgrade pip
	.venv/bin/pip install -q -e .

# Chỉ cần khi AI_FIX_LOG_SOURCE=s3 (AWS S3, Cloudflare R2, MinIO...)
install-s3: install
	.venv/bin/pip install -q -e '.[s3]'

# Tạo database + user MySQL riêng; tự ghi AI_FIX_DATABASE_URL vào .env nếu chưa có.
# Admin login được hỏi trong terminal, hoặc đặt AI_FIX_DB_ADMIN_URL=mysql://root:pass@127.0.0.1:3306
db-init:
	$(RUN) db-init --config config/demo.json

telegram-test:
	$(RUN) telegram-test --config config/demo.json

test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

# Requires AI_FIX_TEST_DATABASE_URL=mysql://user:pass@127.0.0.1:3306/ai_fix_test (tables are dropped!)
test-mysql:
	@test -n "$$AI_FIX_TEST_DATABASE_URL" || (echo "Set AI_FIX_TEST_DATABASE_URL to a disposable MySQL database" && exit 1)
	PYTHONPATH=src $(PYTHON) -m unittest tests.test_storage -v

watch:
	$(RUN) watch --config config/demo.json

status:
	$(RUN) status --config config/demo.json

ui:
	$(RUN) ui --config config/demo.json

import-sqlite:
	$(RUN) import-sqlite --config config/demo.json
