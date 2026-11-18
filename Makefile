# Экспериментальный стенд: одна команда на каждый шаг.
#   make setup        — окружение хоста (uv sync по uv.lock)
#   make test         — линтер и модульные тесты (без Docker)
#   make smoke        — поднять оба варианта SUT и проверить контракт и метрики
#   make experiment   — полный эксперимент: A/A, калибровка, E1, E2, E3 (~6 ч)
#   make experiment CONFIG=configs/e2-abr.yaml   — один эксперимент
#   make ci-experiment — сокращённая конфигурация (как в CI, ~10 мин)
#   make analyze      — таблицы и графики из results/raw
#   make down         — остановить и удалить контейнеры стенда
SHELL := bash
UV ?= $(shell command -v uv 2>/dev/null || echo .devtools/Scripts/uv.exe)
RUN := PYTHONUTF8=1 $(UV) run --frozen
CONFIGS ?= configs/aa-e1.yaml configs/calibration-e1.yaml configs/e1-architecture.yaml \
           configs/aa-e2.yaml configs/e2-abr.yaml configs/e3-joint.yaml
CONFIG ?=

.PHONY: setup data-verify lint test build smoke pilot experiment ci-experiment analyze up down clean-results

setup:
	$(UV) sync --frozen

data-verify:
	$(RUN) python tools/prepare_data.py --verify

lint:
	$(RUN) ruff check .

test: data-verify lint
	$(RUN) pytest -q

build:
	docker compose --profile mono --profile micro --profile tools build

smoke:
	$(RUN) python runner/smoke.py

pilot:
	$(RUN) python runner/run_experiment.py configs/pilot-e1.yaml

experiment: data-verify
ifeq ($(CONFIG),)
	set -e; for c in $(CONFIGS); do $(RUN) python runner/run_experiment.py $$c; done
else
	$(RUN) python runner/run_experiment.py $(CONFIG)
endif

ci-experiment: data-verify
	set -e; for c in configs/ci/ci-e1.yaml configs/ci/ci-e2.yaml; do $(RUN) python runner/run_experiment.py $$c; done

analyze:
	$(RUN) python analysis/analyze.py

up:
	docker compose --profile $${ARCH:-mono} --profile monitoring up -d --wait

down:
	docker compose --profile mono --profile micro --profile monitoring --profile dashboards --profile tools down --remove-orphans

clean-results:
	rm -rf results/raw results/processed results/figures
