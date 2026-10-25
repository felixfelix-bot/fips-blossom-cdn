.PHONY: test syntax ansible-check clean cdn-test cdn-acceptance

test:
	@bash tests/run-all.sh

cdn-test:
	@python3 -m unittest discover -s tests -v

cdn-acceptance:
	@sudo bash scripts/cdn_acceptance.sh

syntax:
	@bash -n scripts/health-check.sh scripts/mk-fips-config.sh scripts/cdn_acceptance.sh
	@python3 -m py_compile scripts/cdn-proxy.py scripts/sync-regions.py \
		scripts/cdn_node.py scripts/cdn_measure.py cdn/*.py
	@echo "syntax OK"

ansible-check:
	@cd ansible && ansible-playbook --syntax-check -i inventory/hosts.yml playbooks/fips-blossom-cdn.yml

clean:
	@find . -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
	@rm -f scripts/*.pyc tests/*.pyc
