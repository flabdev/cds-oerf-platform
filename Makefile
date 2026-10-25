.PHONY: test reset-test reset-demo

test:
	pytest -q -m "not aws"

ifndef PROFILE
reset-test reset-demo:
	$(error PROFILE is required)
else
ifndef TENANT
reset-test reset-demo:
	$(error TENANT is required)
else
reset-test:
	python -m scripts.reset --profile $(PROFILE) --tenant $(TENANT)

reset-demo:
	python -m scripts.reset --profile $(PROFILE) --tenant $(TENANT) --yes --reseed
endif
endif
