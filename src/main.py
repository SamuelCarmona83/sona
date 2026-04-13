import asyncio
import os

APP_MODE = os.getenv("APP_MODE", "discord").lower()


def main():
    if APP_MODE == "local":
        from src.local_mode import run_local

        asyncio.run(run_local())
    else:
        import src.commands  # noqa: F401 — registers all commands and events as side effect
        from src.bot_instance import bot
        from src.config import bot_token

        bot.run(bot_token)


if __name__ == "__main__":
    main()
