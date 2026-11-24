"""Gunicorn lifecycle configuration for NeptonBot.

The bot's polling and queue workers must start after Gunicorn forks its worker,
not in the master process. The master cannot keep Python background threads alive
across a fork.
"""
import os

# bot.py reads this during import. Runtime services are started in post_worker_init.
os.environ["NEPTON_DISABLE_AUTOSTART"] = "1"


def post_worker_init(worker):
    import bot

    bot.start_runtime_services()
