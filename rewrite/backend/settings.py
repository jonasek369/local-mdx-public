import json
import os
from dataclasses import dataclass
from typing import Optional, Callable, List
from rewrite.backend.utils import Logger, warning


@dataclass
class Settings:
    onMangaDownloadFinishHandler: Optional[Callable]
    contentRating: List[str]
    translatedLanguage: List[str]
    cacheTokenToDisk: bool
    logger: Logger
    darkTheme: bool


@dataclass
class MangadexCredentials:
    username: Optional[str]
    password: Optional[str]
    clientId: Optional[str]
    clientSecret: Optional[str]

    def is_valid(self) -> bool:
        return all([self.username, self.password, self.clientId, self.clientSecret])

def load_settings() -> Settings:
    try:
        with open('settings.json', 'r') as f:
            json_settings = json.load(f)
        return Settings(
            None,
            json_settings.get('content_rating', ["safe", "suggestive"]),
            json_settings.get('translated_language', []), # [] means all languages will be fetched
            json_settings.get('cache_token_to_disk', True),
            Logger(json_settings.get('log_level', 1), json_settings.get('file_logger', False)),
            json_settings.get('dark_theme', False),
        )
    except FileNotFoundError:
        Logger().log(warning, f"Could not find settings.json in {os.getcwd()} using default")
        # return default if we cant find the settings
        return Settings(None, ["safe", "suggestive"], [], True, Logger(1, False), False)

def load_credentials() -> MangadexCredentials:
    try:
        with open('mangadex_account.json', 'r') as f:
            credentials = json.load(f)
        return MangadexCredentials(
            credentials.get("username", None),
            credentials.get("password", None),
            credentials.get("client_id", None),
            credentials.get("client_secret", None),
        )
    except FileNotFoundError:
        Logger().log(warning, f"Could not find mangadex_account.json in {os.getcwd()} using empty credentials")
        return MangadexCredentials(None, None, None, None)

def credentials_from_json(_json):
    return MangadexCredentials(
        _json.get("username", None),
        _json.get("password", None),
        _json.get("client_id", None),
        _json.get("client_secret", None),
    )