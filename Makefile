.PHONY: demo

# Build and start the stack (the web service migrates before it reports healthy), then seed a
# demo company, import its bank statement and reconcile it in the Celery worker. Running it again
# finishes an interrupted demo or prints the result again.
# The containers run as uid 1000 and write uploads and PDFs into the bind-mounted media
# directory, which may belong to another user on Linux (CI runners use uid 1001).
demo:
	mkdir -p media && chmod a+rwx media
	docker compose up -d --build --wait
	docker compose exec -T web python manage.py seed_demo
