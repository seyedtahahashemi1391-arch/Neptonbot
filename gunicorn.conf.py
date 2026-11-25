# Gunicorn lifecycle: keep the polling runtime inside the worker process.
preload_app = False
graceful_timeout = 35


def post_worker_init(worker):
    import bot

    if not bot.start_runtime_services():
        bot._start_runtime_acquirer()


def worker_exit(server, worker):
    try:
        import bot

        bot.release_runtime_lease_for_process(worker.pid)
    except Exception:
        server.log.exception(
            "Could not release runtime lease after Gunicorn worker exit (pid=%s)",
            worker.pid,
        )
