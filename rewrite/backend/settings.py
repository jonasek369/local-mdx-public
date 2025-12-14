import json
import os
from dataclasses import dataclass, asdict, fields
from typing import Optional, Callable, List

from rewrite.backend.utils import Logger, warning, error, dataclass_to_jsonable_dict, info


@dataclass
class Settings:
    onMangaDownloadFinishHandler: Optional[Callable]
    contentRating: List[str]
    translatedLanguage: List[str]
    cacheTokenToDisk: bool
    logLevel: int
    fileLogger: bool
    logger: Logger
    darkTheme: bool
    downloaderFileName: Optional[str]


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
            json_settings.get('contentRating', ["safe", "suggestive", "erotica"]),
            json_settings.get('translatedLanguage', ["en"]), # [] means all languages will be fetched
            json_settings.get('cacheTokenToDisk', True),
            json_settings.get('logLevel', 1),
            json_settings.get('fileLogger', False),
            Logger(json_settings.get('logLevel', 1), json_settings.get('fileLogger', False)),
            json_settings.get('darkTheme', True),
            json_settings.get('downloaderFileName', None),
        )
    except (FileNotFoundError, json.decoder.JSONDecodeError):
        Logger().log(warning, f"Could not find settings.json in {os.getcwd()} using default")
        # return default if we cant find the settings
        default = Settings(None, ["safe", "suggestive", "erotica"], ["en"], True, 1, False, Logger(1, False), True, None)
        save_settings(default)
        default.logger.log(warning, "Create settings.json with default values")
        return default

def save_settings(settings: Settings) -> bool:
    try:
        json_dict = dataclass_to_jsonable_dict(settings)
        with open('settings.json', 'w') as f:
            json.dump(json_dict, f, indent=4)
        return True
    except Exception as e:
        Logger().log(error, f"Could not save settings.json: {e}")
        return False

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


def save_credentials(credentials: MangadexCredentials):
    with open("mangadex_account.json", "w") as file:
        json.dump({
            "username": credentials.username,
            "password": credentials.password,
            "client_id": credentials.clientId,
            "client_secret": credentials.clientSecret
        }, file)