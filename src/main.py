import src.commands  # noqa: F401
from src.bot_instance import bot
from src.config import bot_token


def main() -> None:
    bot.run(bot_token)


if __name__ == "__main__":
    main()