.PHONY: test reset-test reset-demo \
	demo-preflight demo-prep demo-sensor demo-reply demo-status demo-cleanup demo-run \
	dev-preflight dev-seed dev-users dev-sensor dev-reply dev-reset dev-check \
	dev-diff dev-deploy dev-provider-check

BUTTON ?= running
PYTHON ?= python
SHIPMENT ?= PH-2841
DEVICE ?= RF-7781

test:
	pytest -q -m "not aws"

RESETS = reset-test reset-demo
DEMOS = demo-preflight demo-prep demo-sensor demo-reply demo-review-qa demo-status demo-cleanup demo-run

ifndef PROFILE
$(RESETS) $(DEMOS):
	$(error PROFILE is required)
else
ifndef TENANT
$(RESETS) $(DEMOS):
	$(error TENANT is required)
else
ifndef STAGE
$(RESETS) $(DEMOS):
	$(error STAGE is required (main or dev). Main also needs ALLOW_MAIN=1. Use the dev-* targets for the dev stage.)
else
ifeq ($(STAGE),main)
ifneq ($(ALLOW_MAIN),1)
$(RESETS) $(DEMOS):
	$(error refusing STAGE=main. Set ALLOW_MAIN=1 to operate on the main stage.)
else
$(eval INCLUDE_STAGE_RECIPES := 1)
endif
else
$(eval INCLUDE_STAGE_RECIPES := 1)
endif
ifdef INCLUDE_STAGE_RECIPES
reset-test:
	STAGE=$(STAGE) ALLOW_MAIN=$(ALLOW_MAIN) $(PYTHON) -m scripts.reset --stage $(STAGE) --profile $(PROFILE) --tenant $(TENANT)

reset-demo:
	STAGE=$(STAGE) ALLOW_MAIN=$(ALLOW_MAIN) $(PYTHON) -m scripts.reset --stage $(STAGE) --profile $(PROFILE) --tenant $(TENANT) --yes --reseed

ifndef ACCOUNT
$(DEMOS):
	$(error ACCOUNT is required)
else
demo-preflight:
	STAGE=$(STAGE) ALLOW_MAIN=$(ALLOW_MAIN) $(PYTHON) -m scripts.demo preflight --stage $(STAGE) --profile $(PROFILE) --account $(ACCOUNT) --tenant $(TENANT)

demo-prep:
	STAGE=$(STAGE) ALLOW_MAIN=$(ALLOW_MAIN) $(PYTHON) -m scripts.demo prep --stage $(STAGE) --profile $(PROFILE) --account $(ACCOUNT) --tenant $(TENANT)

demo-sensor:
	STAGE=$(STAGE) ALLOW_MAIN=$(ALLOW_MAIN) $(PYTHON) -m scripts.demo sensor --stage $(STAGE) --profile $(PROFILE) --account $(ACCOUNT) --tenant $(TENANT) --shipment $(SHIPMENT) --device $(DEVICE)

demo-reply:
	STAGE=$(STAGE) ALLOW_MAIN=$(ALLOW_MAIN) $(PYTHON) -m scripts.demo reply --stage $(STAGE) --profile $(PROFILE) --account $(ACCOUNT) --tenant $(TENANT) --button $(BUTTON)

# OERF_ID_TOKEN is the Cognito ID token the desk sends. OERF_ACCESS_TOKEN is a deprecated alias.
demo-review-qa:
	@test -n "$(INCIDENT)" || { echo "INCIDENT is required"; exit 1; }
	@test -n "$(DECISION)" || { echo "DECISION is required (qa_pass or qa_fail)"; exit 1; }
	STAGE=$(STAGE) ALLOW_MAIN=$(ALLOW_MAIN) $(PYTHON) -m scripts.demo review-qa --stage $(STAGE) --profile $(PROFILE) --account $(ACCOUNT) --tenant $(TENANT) --incident $(INCIDENT) --decision $(DECISION) $(if $(DISPOSITION),--disposition $(DISPOSITION),)

demo-status:
	STAGE=$(STAGE) ALLOW_MAIN=$(ALLOW_MAIN) $(PYTHON) -m scripts.demo status --stage $(STAGE) --profile $(PROFILE) --account $(ACCOUNT) --tenant $(TENANT)

demo-cleanup:
	STAGE=$(STAGE) ALLOW_MAIN=$(ALLOW_MAIN) $(PYTHON) -m scripts.demo cleanup --stage $(STAGE) --profile $(PROFILE) --account $(ACCOUNT) --tenant $(TENANT)

demo-run:
	STAGE=$(STAGE) ALLOW_MAIN=$(ALLOW_MAIN) $(PYTHON) -m scripts.demo run --stage $(STAGE) --profile $(PROFILE) --account $(ACCOUNT) --tenant $(TENANT) --button $(BUTTON)
endif
endif
endif
endif
endif

dev-preflight:
	@test -n "$(PROFILE)" || { echo "PROFILE is required"; exit 1; }
	STAGE=dev $(PYTHON) -m scripts.dev_preflight --stage dev --profile $(PROFILE)

dev-seed:
	@test -n "$(PROFILE)" || { echo "PROFILE is required"; exit 1; }
	@test -n "$(TENANT)" || { echo "TENANT is required"; exit 1; }
	STAGE=dev $(PYTHON) -m scripts.seed_runtime --stage dev --profile $(PROFILE) --tenant $(TENANT) --yes
	STAGE=dev $(PYTHON) -m scripts.verify_seed --stage dev --profile $(PROFILE) --tenant $(TENANT)

dev-users:
	@test -n "$(PROFILE)" || { echo "PROFILE is required"; exit 1; }
	@test -n "$(TENANT)" || { echo "TENANT is required"; exit 1; }
	@test -n "$(ROLE)" || { echo "ROLE is required"; exit 1; }
	@test -n "$(EMAIL)" || { echo "EMAIL is required"; exit 1; }
	STAGE=dev $(PYTHON) -m scripts.seed_demo_users --stage dev --profile $(PROFILE) --tenant $(TENANT) --role $(ROLE) --email $(EMAIL) --prompt-password --yes

dev-sensor:
	@test -n "$(PROFILE)" || { echo "PROFILE is required"; exit 1; }
	@test -n "$(ACCOUNT)" || { echo "ACCOUNT is required"; exit 1; }
	@test -n "$(TENANT)" || { echo "TENANT is required"; exit 1; }
	@test -n "$(SHIPMENT)" || { echo "SHIPMENT is required"; exit 1; }
	@test -n "$(DEVICE)" || { echo "DEVICE is required"; exit 1; }
	@test -n "$(SCENARIO)" || { echo "SCENARIO is required"; exit 1; }
	STAGE=dev $(PYTHON) -m scripts.demo sensor --stage dev --profile $(PROFILE) --account $(ACCOUNT) --tenant $(TENANT) --shipment $(SHIPMENT) --device $(DEVICE) --scenario $(SCENARIO)

dev-reply:
	@test -n "$(PROFILE)" || { echo "PROFILE is required"; exit 1; }
	@test -n "$(ACCOUNT)" || { echo "ACCOUNT is required"; exit 1; }
	@test -n "$(TENANT)" || { echo "TENANT is required"; exit 1; }
	STAGE=dev $(PYTHON) -m scripts.demo reply --stage dev --profile $(PROFILE) --account $(ACCOUNT) --tenant $(TENANT) --button $(BUTTON)

dev-reset:
	@test -n "$(PROFILE)" || { echo "PROFILE is required"; exit 1; }
	@test -n "$(TENANT)" || { echo "TENANT is required"; exit 1; }
	STAGE=dev $(PYTHON) -m scripts.reset --stage dev --profile $(PROFILE) --tenant $(TENANT) --yes --reseed

dev-check:
	@test -n "$(PROFILE)" || { echo "PROFILE is required"; exit 1; }
	@test -n "$(TENANT)" || { echo "TENANT is required"; exit 1; }
	STAGE=dev $(PYTHON) -m scripts.verify_seed --stage dev --profile $(PROFILE) --tenant $(TENANT)

dev-provider-check:
	@test -n "$(PROFILE)" || { echo "PROFILE is required"; exit 1; }
	STAGE=dev $(PYTHON) -m scripts.dev_provider_check --stage dev --profile $(PROFILE)

dev-diff dev-deploy:
	@test -n "$(PROFILE)" || { echo "PROFILE is required"; exit 1; }
	@test -n "$(ACCOUNT)" || { echo "ACCOUNT is required"; exit 1; }
	@echo "Normal dev deploys happen by merging to develop. dev-deploy and dev-diff are for laptop use."
	STAGE=dev CHANNELS_LIVE=$(CHANNELS_LIVE) $(PYTHON) -m scripts.dev_deploy check --profile $(PROFILE) --account $(ACCOUNT)
	npm ci --prefix web
	npm run build --prefix web
	cdk $(if $(filter dev-deploy,$@),deploy --require-approval never,diff --fail-on never) --profile $(PROFILE) \
		$$(STAGE=dev CHANNELS_LIVE=$(CHANNELS_LIVE) $(PYTHON) -m scripts.dev_deploy argv --profile $(PROFILE) --account $(ACCOUNT))
