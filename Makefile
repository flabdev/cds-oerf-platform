.PHONY: test reset-test reset-demo \
	demo-preflight demo-prep demo-sensor demo-reply demo-status demo-cleanup demo-run

BUTTON ?= running

test:
	pytest -q -m "not aws"

RESETS = reset-test reset-demo
DEMOS = demo-preflight demo-prep demo-sensor demo-reply demo-status demo-cleanup demo-run

ifndef PROFILE
$(RESETS) $(DEMOS):
	$(error PROFILE is required)
else
ifndef TENANT
$(RESETS) $(DEMOS):
	$(error TENANT is required)
else
reset-test:
	python -m scripts.reset --profile $(PROFILE) --tenant $(TENANT)

reset-demo:
	python -m scripts.reset --profile $(PROFILE) --tenant $(TENANT) --yes --reseed

demo-preflight:
	python -m scripts.demo preflight --profile $(PROFILE) --tenant $(TENANT)

demo-prep:
	python -m scripts.demo prep --profile $(PROFILE) --tenant $(TENANT)

demo-sensor:
	python -m scripts.demo sensor --profile $(PROFILE) --tenant $(TENANT)

demo-reply:
	python -m scripts.demo reply --profile $(PROFILE) --tenant $(TENANT) --button $(BUTTON)

demo-status:
	python -m scripts.demo status --profile $(PROFILE) --tenant $(TENANT)

demo-cleanup:
	python -m scripts.demo cleanup --profile $(PROFILE) --tenant $(TENANT)

demo-run:
	python -m scripts.demo run --profile $(PROFILE) --tenant $(TENANT) --button $(BUTTON)
endif
endif
