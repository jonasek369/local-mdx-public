import json
from dataclasses import fields
from datetime import datetime
import io
import os
import time
import uuid
from enum import Enum
from typing import Dict, List, Union, Optional

from PIL import Image
import asyncio
from concurrent.futures import ThreadPoolExecutor

from rewrite.backend.schemas import Relationship


def perf_test(func):
    """
    A decorator to measure the execution time of a function.
    """

    def wrapper(*args, **kwargs):
        start_time = time.perf_counter()  # Record the start time
        result = func(*args, **kwargs)  # Call the function
        end_time = time.perf_counter()  # Record the end time
        execution_time = end_time - start_time  # Calculate the elapsed time
        print(f"Function '{func.__name__}' executed in {execution_time:.4f} seconds.")
        return result

    return wrapper


def get_relationships(relationships: List[Relationship], relationship_type_to_find: str) -> Optional[
    List[Relationship]]:
    # from https://api.mangadex.org/docs/3-enumerations/
    allowed_types = {"manga", "chapter", "cover_art", "author", "artist", "scanlation_group", "tag", "user",
                     "custom_list"}
    if relationship_type_to_find not in allowed_types:
        return None
    found = list(filter(lambda relationship: relationship_type_to_find == relationship.type, relationships))
    if not found:
        return None
    return found

# helper that makes the code more readable
def is_expired(created_ts, ttl_seconds):
    return time.time() - created_ts > ttl_seconds


def normalize_language_input(data):
    if isinstance(data, dict):
        return [{k: v} for k, v in data.items()]

    elif isinstance(data, list):
        normalized = []
        for item in data:
            if isinstance(item, dict):
                for k, v in item.items():
                    normalized.append({k: v})
        return normalized

    return []


TRANSLATION_FALLBACK = "ja-ro"


def get_correct_language(
        from_languages: Union[Dict, List[Dict]],
        from_alt: Union[Dict, List[Dict]] | None,
        settings
) -> str | None:
    desired_langs: List[str] = settings.translatedLanguage
    if from_alt is None:
        available_languages = normalize_language_input(from_languages)
    else:
        available_languages = normalize_language_input(from_languages) + normalize_language_input(from_alt)
    fallback_translation = None

    for lang_dict in available_languages:
        if not lang_dict:
            continue
        # Extract the single key-value pair
        try:
            lang_code, translation = next(iter(lang_dict.items()))
        except StopIteration:
            continue

        if not translation:
            continue  # skip empty or None

        if lang_code in desired_langs:
            return translation
        if lang_code == TRANSLATION_FALLBACK and fallback_translation is None:
            fallback_translation = translation

    # Return fallback if no desired language found
    if fallback_translation:
        return fallback_translation

    # Return first non-empty translation
    for lang_dict in available_languages:
        if not lang_dict:
            continue
        translation = next(iter(lang_dict.values()))
        if translation:
            return translation

    return None


def is_uuid4(value: str) -> bool:
    try:
        val = uuid.UUID(value, version=4)
    except (ValueError, AttributeError, TypeError):
        return False
    return str(val) == value.lower()


_executor = ThreadPoolExecutor(max_workers=os.cpu_count() if os.cpu_count() is not None else 4)


def run_async_in_thread(async_func, *args, **kwargs):
    import asyncio

    def runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(async_func(*args, **kwargs))

    return _executor.submit(runner).result()


def run_async(func, *args, **kwargs):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop.run_until_complete(func(*args, **kwargs))


# TODO: Make compression, lossless user changeable
@perf_test
def convert_to_webp(image: bytes, compression=100, lossless=False) -> bytes:
    input_buffer = io.BytesIO(image)
    image = Image.open(input_buffer)

    output_buffer = io.BytesIO()
    if lossless:
        image.save(output_buffer, format="WEBP", lossless=True)
    else:
        image.save(output_buffer, format="WEBP", quality=compression)

    return output_buffer.getvalue()


def colored(rgb, text):
    return "\033[38;2;{};{};{}m{} \033[38;2;255;255;255m".format(rgb[0], rgb[1], rgb[2],
                                                                 text)


class LogType(Enum):
    DEBUG = 1
    INFO = 2
    SUCCESSS = 3
    WARNING = 4
    ERROR = 5
    CRITICAL = 6
    TRACEBACK = 7

debug = LogType.DEBUG
info = LogType.INFO
success = LogType.SUCCESSS
warning = LogType.WARNING
error = LogType.ERROR
critical = LogType.CRITICAL
traceback = LogType.TRACEBACK


class Logger:
    def __init__(self, ll: int = 1, file_logger: bool = False):
        self.log_level = ll
        self.file_logger = file_logger
        if self.file_logger:
            self.file_log = []
            self.file_start = time.time()
            if not os.path.isdir("logs"):
                os.mkdir("logs")
            self.file = open(f"logs\\{self.file_start}.log", "w")
        os.system("cls")

    def log(self, ll: LogType, text):
        if ll.value >= self.log_level:
            match ll:
                case LogType.DEBUG:
                    print(colored([230, 230, 230], f"Debug: {text}"))
                case LogType.INFO:
                    print(colored([0, 100, 255], f"Info: {text}"))
                case LogType.SUCCESSS:
                    print(colored([0, 255, 0], f"Success: {text}"))
                case LogType.WARNING:
                    print(colored([255, 255, 0], f"Warning: {text}"))
                case LogType.ERROR:
                    print(colored([255, 60, 60], f"Error: {text}"))
                case LogType.CRITICAL:
                    print(colored([255, 0, 0], f"Critical: {text}"))
                case LogType.TRACEBACK:
                    print(colored([255, 255, 255], f"Traceback: {text}"))
                case _:
                    raise Exception("Unknown log level")

        if self.file_logger:
            self.file_log.append((datetime.now(), ll, text))

    def __del__(self):
        if not self.file_logger:
            return
        with self.file as file:
            for log_event in self.file_log:
                file.write(f"{log_event[0]}:{log_event[1]}: {log_event[2]}\n")


def input_as_bool(inp: str) -> bool:
    stripped = inp.strip().lower()
    if stripped == "true" or stripped == "1" or stripped == "t" or stripped == "y" or stripped == "yes":
        return True
    return False


EXCLUDED_SETTINGS_FIELDS = {"logger", "onMangaDownloadFinishHandler"}


def settings_to_jsonable_dict(obj):
    result = {}
    for f in fields(obj):
        value = getattr(obj, f.name)
        if not f.name in EXCLUDED_SETTINGS_FIELDS:
            result[f.name] = value
    return result
