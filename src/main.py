import src.commands  # noqa: F401 — registers all commands and events as side effect
from src.bot_instance import bot
from src.config import bot_token
from src.web_stream import start_web_stream


def main():
    # Start web stream server (Flask + audio streaming)
    start_web_stream()
    
    # Start Discord bot
    bot.run(bot_token)


if __name__ == "__main__":
    main()
