# Run the multi-agent cluster locally, one process per agent.
#
# Each agent is a separate uvicorn process talking to the others over real A2A,
# which is the point: the coupling this architecture removes is only observable
# when the agents are genuinely in different processes.
#
#   make up            start all four agents
#   make status        are they up, and on which models
#   make demo          run the human-in-the-loop flow end to end
#   make down          stop them
#
# Targets `serve-<agent>` run one agent in the foreground when you want to watch
# it, e.g. `make serve-math`.

AGENTS := orchestrator math research planner

PORT_orchestrator := 8090
PORT_math         := 8091
PORT_research     := 8092
PORT_planner      := 8093

RUN_DIR := .run
HOST    := 127.0.0.1

# The orchestrator's peers, addressed explicitly. In the cluster these come from
# Kubernetes DNS; locally they are loopback ports (see app/cluster/config.py).
PEERS := research=http://$(HOST):$(PORT_research),math=http://$(HOST):$(PORT_math),planner=http://$(HOST):$(PORT_planner)

# --- Environment -------------------------------------------------------------
#
# Two of these are not preferences, they are bug fixes. `load_dotenv()` runs
# AFTER the injector is built (app/fast_api_app.py), so a value in .env does not
# reach model configuration -- a stale GOOGLE_CLOUD_LOCATION in your shell wins
# and you get a 403 that reads like a model problem. And google-genai prefers an
# API key over Application Default Credentials when one is present, silently
# bypassing the Vertex path these agents are built for.
#
# Override any of them on the command line: `make up GOOGLE_CLOUD_PROJECT=foo`.

# Resolution order: command line > environment > .env > gcloud config. `.env` is
# where this repo already keeps local settings, so honouring it means `make up`
# needs no flags on a machine that is set up.
DOTENV_PROJECT := $(shell [ -f .env ] && sed -n 's/^GOOGLE_CLOUD_PROJECT=//p' .env | tail -1)
GCLOUD_PROJECT := $(shell gcloud config get-value project 2>/dev/null)
GOOGLE_CLOUD_PROJECT ?= $(if $(DOTENV_PROJECT),$(DOTENV_PROJECT),$(GCLOUD_PROJECT))

# `:=`, deliberately, where the project uses `?=`. A stale GOOGLE_CLOUD_LOCATION
# exported in a shell is the exact failure this is guarding against, and `?=`
# would inherit it -- so the environment does NOT get a vote here. A command-line
# override still wins in make, which is the escape hatch:
#   make up GOOGLE_CLOUD_LOCATION=europe-west4
# `global` is the endpoint that serves the Gemini 3 tiers; a regional value is
# the usual cause of "model not found" and "permission denied".
GOOGLE_CLOUD_LOCATION := global
#
# GEMINI_*_MODEL are deliberately NOT pinned: app/shared/model_selection.py
# resolves the newest model per family from the live Vertex catalog at startup.
# Pin them only for hermetic tests (see AGENTS.md).
BASE_ENV := \
	GOOGLE_API_KEY= \
	GEMINI_API_KEY= \
	GOOGLE_CLOUD_PROJECT=$(GOOGLE_CLOUD_PROJECT) \
	GOOGLE_CLOUD_LOCATION=$(GOOGLE_CLOUD_LOCATION) \
	GOOGLE_GENAI_USE_ENTERPRISE=true

.DEFAULT_GOAL := help
.PHONY: help check up down restart status logs demo test lint $(addprefix serve-,$(AGENTS))

help:
	@echo "Local multi-agent cluster"
	@echo ""
	@echo "  make check          verify credentials and configuration first"
	@echo "  make up             start all four agents in the background"
	@echo "  make status         show health, port and resolved model per agent"
	@echo "  make logs           follow every agent's log"
	@echo "  make logs A=math    follow one agent's log"
	@echo "  make demo           run the approval flow end to end"
	@echo "  make down           stop everything"
	@echo "  make restart        down, then up"
	@echo ""
	@echo "  make serve-math     run one agent in the foreground"
	@echo "  make test           hermetic unit tests"
	@echo "  make lint           ruff + codespell + ty"
	@echo ""
	@echo "  project=$(GOOGLE_CLOUD_PROJECT)  location=$(GOOGLE_CLOUD_LOCATION)"
	@echo "  orchestrator UI: http://$(HOST):$(PORT_orchestrator)/dev-ui/"

# --- Preflight ---------------------------------------------------------------

check:
	@fail=0; \
	if [ -z "$(GOOGLE_CLOUD_PROJECT)" ]; then \
		echo "FAIL  no project. Set one: make up GOOGLE_CLOUD_PROJECT=your-project"; fail=1; \
	else echo "ok    project  $(GOOGLE_CLOUD_PROJECT)"; fi; \
	if ! gcloud auth application-default print-access-token >/dev/null 2>&1; then \
		echo "FAIL  no Application Default Credentials."; \
		echo "      run: gcloud auth application-default login"; fail=1; \
	else echo "ok    ADC      present"; fi; \
	echo "ok    location $(GOOGLE_CLOUD_LOCATION)"; \
	if [ -n "$$GOOGLE_API_KEY$$GEMINI_API_KEY" ]; then \
		echo "note  GOOGLE_API_KEY/GEMINI_API_KEY set in your shell; these targets"; \
		echo "      clear them so the agents use ADC against Vertex."; \
	fi; \
	echo "...   checking Vertex reachability"; \
	if $(BASE_ENV) uv run python -c "from google.genai import Client; \
Client(vertexai=True, project='$(GOOGLE_CLOUD_PROJECT)', location='$(GOOGLE_CLOUD_LOCATION)') \
.models.generate_content(model='gemini-3.7-flash', contents='ok')" >/dev/null 2>&1; then \
		echo "ok    vertex   gemini-3.7-flash reachable"; \
	else \
		echo "FAIL  cannot call gemini-3.7-flash in $(GOOGLE_CLOUD_PROJECT)/$(GOOGLE_CLOUD_LOCATION)."; \
		echo "      a 403 here is usually the project, not the model."; fail=1; \
	fi; \
	exit $$fail

# --- Lifecycle ---------------------------------------------------------------

up: $(RUN_DIR)
	@$(MAKE) --no-print-directory check >/dev/null 2>&1 || { \
		echo "preflight failed -- running it in full:"; echo; \
		$(MAKE) --no-print-directory check; \
		echo; echo "not starting. fix the above, or:"; \
		echo "  make up GOOGLE_CLOUD_PROJECT=<a project that can call Vertex>"; \
		exit 1; }
	@for a in $(AGENTS); do \
		eval port=\$$PORT_$$a; \
		case $$a in orchestrator) port=$(PORT_orchestrator); peers="A2A_PEERS=$(PEERS)";; \
			math) port=$(PORT_math); peers=;; \
			research) port=$(PORT_research); peers=;; \
			planner) port=$(PORT_planner); peers=;; esac; \
		if lsof -ti tcp:$$port >/dev/null 2>&1; then \
			echo "skip  $$a already listening on $$port"; continue; fi; \
		env $(BASE_ENV) $$peers AGENT_NAME=$$a APP_URL=http://$(HOST):$$port \
			nohup uv run uvicorn app.fast_api_app:app --host $(HOST) --port $$port \
			> $(RUN_DIR)/$$a.log 2>&1 & \
		echo $$! > $(RUN_DIR)/$$a.pid; \
		echo "start $$a on $$port"; \
	done
	@echo "waiting for agent cards..."
	@for a in $(AGENTS); do \
		case $$a in orchestrator) port=$(PORT_orchestrator);; math) port=$(PORT_math);; \
			research) port=$(PORT_research);; planner) port=$(PORT_planner);; esac; \
		for i in $$(seq 1 60); do \
			if curl -sf -o /dev/null -m 2 http://$(HOST):$$port/a2a/app/.well-known/agent-card.json; \
				then break; fi; sleep 1; \
		done; \
	done
	@$(MAKE) --no-print-directory status

$(RUN_DIR):
	@mkdir -p $(RUN_DIR)

down:
	@for a in $(AGENTS); do \
		case $$a in orchestrator) port=$(PORT_orchestrator);; math) port=$(PORT_math);; \
			research) port=$(PORT_research);; planner) port=$(PORT_planner);; esac; \
		pids=$$(lsof -ti tcp:$$port 2>/dev/null); \
		if [ -n "$$pids" ]; then kill $$pids 2>/dev/null; echo "stop  $$a ($$port)"; \
		else echo "--    $$a not running"; fi; \
		rm -f $(RUN_DIR)/$$a.pid; \
	done
	@sleep 2
	@for a in $(AGENTS); do \
		case $$a in orchestrator) port=$(PORT_orchestrator);; math) port=$(PORT_math);; \
			research) port=$(PORT_research);; planner) port=$(PORT_planner);; esac; \
		pids=$$(lsof -ti tcp:$$port 2>/dev/null); \
		if [ -n "$$pids" ]; then kill -9 $$pids 2>/dev/null || true; fi; \
	done

restart:
	@$(MAKE) --no-print-directory down
	@$(MAKE) --no-print-directory up

status:
	@for a in $(AGENTS); do \
		case $$a in orchestrator) port=$(PORT_orchestrator);; math) port=$(PORT_math);; \
			research) port=$(PORT_research);; planner) port=$(PORT_planner);; esac; \
		code=$$(curl -s -o /dev/null -m 5 -w "%{http_code}" \
			http://$(HOST):$$port/a2a/app/.well-known/agent-card.json 2>/dev/null); \
		model=$$(grep -oE 'gemini-[0-9][^"'"'"' ,]*' $(RUN_DIR)/$$a.log 2>/dev/null | head -1); \
		if [ "$$code" = "200" ]; then \
			printf "up    %-13s :%s  %s\n" "$$a" "$$port" "$${model:-(no model call yet)}"; \
		else \
			printf "DOWN  %-13s :%s\n" "$$a" "$$port"; \
		fi; \
	done
	@echo ""
	@echo "orchestrator UI: http://$(HOST):$(PORT_orchestrator)/dev-ui/"
	@echo "approvals:       curl -s localhost:$(PORT_orchestrator)/cases | jq"

A ?=
logs:
	@if [ -n "$(A)" ]; then tail -f $(RUN_DIR)/$(A).log; \
	else tail -f $(RUN_DIR)/*.log; fi

# --- One agent in the foreground ---------------------------------------------

serve-orchestrator:
	@env $(BASE_ENV) A2A_PEERS=$(PEERS) AGENT_NAME=orchestrator \
		APP_URL=http://$(HOST):$(PORT_orchestrator) \
		uv run uvicorn app.fast_api_app:app --host $(HOST) --port $(PORT_orchestrator)

serve-math:
	@env $(BASE_ENV) AGENT_NAME=math APP_URL=http://$(HOST):$(PORT_math) \
		uv run uvicorn app.fast_api_app:app --host $(HOST) --port $(PORT_math)

serve-research:
	@env $(BASE_ENV) AGENT_NAME=research APP_URL=http://$(HOST):$(PORT_research) \
		uv run uvicorn app.fast_api_app:app --host $(HOST) --port $(PORT_research)

serve-planner:
	@env $(BASE_ENV) AGENT_NAME=planner APP_URL=http://$(HOST):$(PORT_planner) \
		uv run uvicorn app.fast_api_app:app --host $(HOST) --port $(PORT_planner)

# --- Demo --------------------------------------------------------------------
#
# Drives the approval flow through /cases/run rather than the web UI: proposal
# detection lives in that route, so a gated action driven from the UI correctly
# refuses to publish but records no case. See docs/human-in-the-loop.md.

demo:
	@echo "1. asking for something that needs sign-off..."
	@curl -s -m 300 -X POST localhost:$(PORT_orchestrator)/cases/run \
		-H 'content-type: application/json' \
		-d '{"session_id":"make-demo","text":"This is Marc Steiner, my direct line is +353 87 555 0101. Our Q3 revenue for the Ireland desk came to 17 batches of 23 million each. Please work out the total and publish it under the label q3-revenue-ireland."}' \
		> $(RUN_DIR)/demo.json || { echo "   request failed -- is the cluster up?"; exit 1; }
	@uv run python -c "import json; d=json.load(open('$(RUN_DIR)/demo.json')); \
print('   status:', d['status']); \
[print('   case  :', p['proposal_id'], p['proposal']) for p in d['pending']]"
	@echo ""
	@echo "2. the specialist must not have seen the phone number:"
	@if grep -q '555 0101' $(RUN_DIR)/math.log 2>/dev/null; then \
		echo "   LEAKED -- found in math.log"; exit 1; \
	else echo "   ok, absent from math.log"; fi
	@echo ""
	@echo "3. approving it (the effect happens here, not before)..."
	@id=$$(uv run python -c "import json; d=json.load(open('$(RUN_DIR)/demo.json')); \
print(d['pending'][0]['proposal_id'] if d['pending'] else '')"); \
	if [ -z "$$id" ]; then echo "   no case was raised -- nothing to approve"; exit 1; fi; \
	curl -s -m 300 -X POST localhost:$(PORT_orchestrator)/cases/$$id \
		-H 'content-type: application/json' \
		-d '{"approved":true,"decided_by":"cfo@example.com","note":"reconciled"}' \
		| uv run python -c "import json,sys; d=json.load(sys.stdin); \
print('   status:', d['status']); print('   result:', d['case']['result'])"

# --- Quality -----------------------------------------------------------------

test:
	@GEMINI_FAST_MODEL=gemini-2.5-flash-lite \
	GEMINI_BALANCED_MODEL=gemini-2.5-flash \
	GEMINI_CAPABLE_MODEL=gemini-2.5-pro \
	GEMINI_EMBEDDING_MODEL=gemini-embedding-001 \
		uv run pytest tests/unit app/shared/tests -q

lint:
	@agents-cli lint
