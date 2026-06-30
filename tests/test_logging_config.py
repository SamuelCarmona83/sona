import logging
import unittest

from src.logging_config import _CONFIG_SENTINEL, configure_logging


class LoggingConfigTests(unittest.TestCase):
    def test_configure_logging_is_idempotent(self) -> None:
        root = logging.getLogger()
        original_handlers = list(root.handlers)
        original_level = root.level
        original_discord_level = logging.getLogger("discord").level
        original_sentinel = getattr(root, _CONFIG_SENTINEL, False)

        try:
            root.handlers = []
            if hasattr(root, _CONFIG_SENTINEL):
                delattr(root, _CONFIG_SENTINEL)

            configure_logging()
            handlers_after_first_call = list(root.handlers)

            configure_logging()
            handlers_after_second_call = list(root.handlers)

            self.assertEqual(len(handlers_after_first_call), 1)
            self.assertEqual(len(handlers_after_second_call), 1)
            self.assertEqual(
                handlers_after_first_call[0],
                handlers_after_second_call[0],
            )
            self.assertEqual(logging.getLogger("discord").level, logging.WARNING)
        finally:
            root.handlers = original_handlers
            root.setLevel(original_level)
            logging.getLogger("discord").setLevel(original_discord_level)
            if original_sentinel:
                setattr(root, _CONFIG_SENTINEL, True)
            elif hasattr(root, _CONFIG_SENTINEL):
                delattr(root, _CONFIG_SENTINEL)


if __name__ == "__main__":
    unittest.main()
