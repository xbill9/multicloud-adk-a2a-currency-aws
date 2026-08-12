.PHONY: clean

# Removes generated build and test artifacts only. Deliberately leaves .run/
# (local agent logs), .env and .aws_creds alone -- those are working state and
# credentials, not build output.
clean:
	find . -path ./.git -prune -o -type d -name '__pycache__' -print0 | xargs -0 -r rm -rf
	find . -path ./.git -prune -o -type d -name '*.egg-info' -print0 | xargs -0 -r rm -rf
	rm -rf .pytest_cache .ruff_cache build dist htmlcov
	rm -f .coverage
