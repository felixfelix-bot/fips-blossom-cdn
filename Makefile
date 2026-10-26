.PHONY: test syntax ansible-check clean cdn-test cdn-acceptance cdn-deploy cdn-fleet-test

test:
	@bash tests/run-all.sh

cdn-test:
	@python3 -m unittest discover -s tests -v

cdn-acceptance:
	@sudo bash scripts/cdn_acceptance.sh

# Deploy the fleet nodes (one origin + three replicas) by config-as-code.
cdn-deploy:
	@cd ansible && ansible-playbook -i inventory/fleet.yml playbooks/cdn.yml

# Measure the six fleet claims against the live nodes and write JSON evidence.
# Run from a host with real mesh paths; the manager reaches the DE nodes only.
# The test is expected to take minutes: the transatlantic path is slow.
cdn-fleet-test:
	@python3 scripts/fleet_cdn_test.py \
		--node hz-de-a=http://[fdb0:bf34:d648:96ac:f65a:3fd6:358:710b]:18080 \
		--node hz-de-b=http://[fd97:77d4:cd27:a6ae:1b29:e92e:fd96:dee8]:18080 \
		--node hz-us-a=http://[fdb8:d8ff:833c:6814:5865:55ac:3c98:afd9]:18080 \
		--node hz-us-b=http://[fdfd:c0e5:3717:6cb1:bb60:de97:987e:7149]:18080 \
		--out evidence/fleet-cdn-results.json

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
