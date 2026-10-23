.PHONY: test syntax ansible-check clean

test:
	@bash tests/run-all.sh

syntax:
	@bash -n scripts/health-check.sh scripts/mk-fips-config.sh
	@python3 -m py_compile scripts/cdn-proxy.py scripts/sync-regions.py
	@echo "syntax OK"

ansible-check:
	@cd ansible && ansible-playbook --syntax-check -i inventory/hosts.yml playbooks/fips-blossom-cdn.yml

clean:
	@find . -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
	@rm -f scripts/*.pyc tests/*.pyc
