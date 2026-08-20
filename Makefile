# Run the multi-agent cluster locally, one process per agent.
#
# Each agent is a separate uvicorn process talking to the others over real A2A,
# which is the point: the coupling this architecture removes is only observable
# when the agents are genuinely in different processes.
#
#   make up            start every agent
#   make status        are they up, and on which models
#   make demo          run the human-in-the-loop flow end to end
#   make down          stop them
#   make image         build and push the container image with Cloud Build
#
# Targets `serve-<agent>` run one agent in the foreground when you want to watch
# it, e.g. `make serve-math`.

AGENTS := orchestrator math research planner trades currency

PORT_orchestrator := 8090
PORT_math         := 8091
PORT_research     := 8092
PORT_planner      := 8093
PORT_trades       := 8094
PORT_currency     := 8095

# The same ports again, in a form a shell loop can look up. Every loop below
# iterates over $(AGENTS) and needs that agent's port; a `case` inside each loop
# meant four places to edit when an agent was added, and missing one yields an
# empty port and a curl to `http://127.0.0.1:` -- which reads as "the agent is
# down". Use it as `port=$(PORT_OF)` with `$$a` holding the agent name.
PORTS  := orchestrator=$(PORT_orchestrator) math=$(PORT_math) \
          research=$(PORT_research) planner=$(PORT_planner) \
          trades=$(PORT_trades) currency=$(PORT_currency)
PORT_OF = $$(echo "$(PORTS)" | tr ' ' '\n' | sed -n "s/^$$a=//p")

RUN_DIR := .run
HOST    := 127.0.0.1

# Peers, addressed explicitly. In the cluster these come from Kubernetes DNS;
# locally they are loopback ports (see app/cluster/config.py).
#
# There are TWO sets, because delegation is not one level deep: `math` sends
# currency conversions on to `currency`. Omit MATH_PEERS and everything still
# starts and still reports healthy -- the math agent just has no currency tool,
# and a conversion quietly turns into something it declines to do.
PEERS      := research=http://$(HOST):$(PORT_research),math=http://$(HOST):$(PORT_math),planner=http://$(HOST):$(PORT_planner),trades=http://$(HOST):$(PORT_trades)
MATH_PEERS := currency=http://$(HOST):$(PORT_currency)

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
	@echo "  make up             start every agent in the background"
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
	@echo "  make image          build + push the image with Cloud Build"
	@echo "  make image TAG=x    ... under an explicit tag"
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
		port=$(PORT_OF); \
		case $$a in \
			orchestrator) peers="A2A_PEERS=$(PEERS)";; \
			math)         peers="A2A_PEERS=$(MATH_PEERS)";; \
			*)            peers=;; \
		esac; \
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
		port=$(PORT_OF); \
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
		port=$(PORT_OF); \
		pids=$$(lsof -ti tcp:$$port 2>/dev/null); \
		if [ -n "$$pids" ]; then kill $$pids 2>/dev/null; echo "stop  $$a ($$port)"; \
		else echo "--    $$a not running"; fi; \
		rm -f $(RUN_DIR)/$$a.pid; \
	done
	@sleep 2
	@for a in $(AGENTS); do \
		port=$(PORT_OF); \
		pids=$$(lsof -ti tcp:$$port 2>/dev/null); \
		if [ -n "$$pids" ]; then kill -9 $$pids 2>/dev/null || true; fi; \
	done

restart:
	@$(MAKE) --no-print-directory down
	@$(MAKE) --no-print-directory up

status:
	@for a in $(AGENTS); do \
		port=$(PORT_OF); \
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

# The math agent gets peers of its own: it delegates currency conversions on to
# the currency specialist rather than applying a rate it recalled.
serve-math:
	@env $(BASE_ENV) A2A_PEERS=$(MATH_PEERS) AGENT_NAME=math \
		APP_URL=http://$(HOST):$(PORT_math) \
		uv run uvicorn app.fast_api_app:app --host $(HOST) --port $(PORT_math)

serve-trades:
	@env $(BASE_ENV) AGENT_NAME=trades APP_URL=http://$(HOST):$(PORT_trades) \
		uv run uvicorn app.fast_api_app:app --host $(HOST) --port $(PORT_trades)

serve-currency:
	@env $(BASE_ENV) AGENT_NAME=currency APP_URL=http://$(HOST):$(PORT_currency) \
		uv run uvicorn app.fast_api_app:app --host $(HOST) --port $(PORT_currency)

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

# --- Image -------------------------------------------------------------------
#
# Builds in Cloud Build, not here. Only the source tarball leaves this machine
# (a few hundred KB -- see .gcloudignore); the ~400 MB of wheels and the ~550 MB
# of pushed layers move inside Google's network. It also removes the
# `--platform linux/amd64` trap: Cloud Build workers are amd64, so an arm64
# workstation can no longer produce an image whose pods die with
# `exec format error`.
#
#   make image                 build and push :$(TAG)
#   make image TAG=demo-2      ... under a different tag
#
# Keep TAG in step with newTag in infra/kustomize/overlays/dev/kustomization.yaml
# -- the cluster pulls what that file names, not what was built last.

TAG    ?= demo-1
REGION ?= europe-west4
REPO   ?= agents

# The image lives where the CLUSTER lives, which is not necessarily the project
# you run models against locally. Keeping this separate from
# GOOGLE_CLOUD_PROJECT means `make up` and `make image` can disagree without
# either being wrong -- and without a build quietly pushing into the wrong
# registry. Override with `make image BUILD_PROJECT=other-project`.
BUILD_PROJECT ?= msteiner
BUILDER       ?= agent-builder@$(BUILD_PROJECT).iam.gserviceaccount.com

.PHONY: image
image:
	@echo "building $(REGION)-docker.pkg.dev/$(BUILD_PROJECT)/$(REPO)/agent:$(TAG)"
	@gcloud builds submit \
		--config cloudbuild.yaml \
		--project $(BUILD_PROJECT) \
		--service-account projects/$(BUILD_PROJECT)/serviceAccounts/$(BUILDER) \
		--substitutions _REGION=$(REGION),_REPO=$(REPO),_TAG=$(TAG)
	@echo ""
	@echo "set newTag: $(TAG) in infra/kustomize/overlays/dev/kustomization.yaml,"
	@echo "then deploy it -- which needs a human, not this Makefile."

# --- Quality -----------------------------------------------------------------

test:
	@GEMINI_FAST_MODEL=gemini-2.5-flash-lite \
	GEMINI_BALANCED_MODEL=gemini-2.5-flash \
	GEMINI_CAPABLE_MODEL=gemini-2.5-pro \
	GEMINI_EMBEDDING_MODEL=gemini-embedding-001 \
		uv run pytest tests/unit app/shared/tests -q

lint:
	@agents-cli lint
