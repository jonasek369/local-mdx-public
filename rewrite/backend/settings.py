import json
from dataclasses import dataclass
from typing import Optional, Callable, List
from rewrite.backend.utils import Logger

@dataclass
class Settings:
    onMangaDownloadFinishHandler: Optional[Callable]
    contentRating: List[str]
    logger: Logger


def load_settings() -> Settings:
    try:
        with open('settings.json', 'r') as f:
            json_settings = json.load(f)
        return Settings(
            None,
            json_settings.get('content_rating', ["safe", "suggestive"]),
            Logger(json_settings.get('log_level', 1), json_settings.get('file_logger', False)),
        )
    except FileNotFoundError:
        # return default if we cant find the settings
        return Settings(None, ["safe", "suggestive"], Logger(1, False))