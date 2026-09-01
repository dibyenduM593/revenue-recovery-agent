.PHONY: demo demo-verify test migrate

# The literal Day 12 target. Works wherever `make` exists; scripts/demo.py
# also runs directly (`python scripts/demo.py`) for anyone who doesn't
# have make -- Windows dev boxes especially -- so make is never required
# to see this run.
demo:
	python scripts/demo.py

# Gate D's actual acceptance check: run the full chain twice from an
# empty database and confirm the batch report matches exactly.
demo-verify:
	python scripts/demo.py --verify-determinism

test:
	python -m pytest tests/ -v

migrate:
	python -m alembic upgrade head
