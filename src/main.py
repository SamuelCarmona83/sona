import src.commands  # noqa: F401 — registers all commands and events as side effect
from src.bot_instance import bot
from src.config import bot_token


def main():
    bot.run(bot_token)


if __name__ == "__main__":
    main()
