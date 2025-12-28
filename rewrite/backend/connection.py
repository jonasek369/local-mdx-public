import asyncio
import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional, Tuple, Dict, Union, List, Any, Sequence, Callable, Deque

import requests
from dateutil.relativedelta import relativedelta

from rewrite.backend.schemas import MangaList, from_json, Manga, MangaIdentifier, ChapterIdentifier, ChapterList, \
    Chapter, \
    DirectSearchManga, DirectSearchChapter, CustomList, CustomListResponse, COVER_ART_256_SIZE, \
    COVER_ART_512_SIZE, COVER_ART_MAX_SIZE, Relationship, MangaAttributes
from rewrite.backend.settings import Settings, MangadexCredentials
from datetime import datetime

from rewrite.backend.utils import error, info, warning, perf_test, get_correct_language, \
    Logger, run_async, is_expired, success, debug
import aiohttp

CACHE_AGGREGATE_TTL = 60 * 5
CACHE_COVER_ART_TTL = 60 * 10
CACHE_MANGA_ATTRIBUTES_TTL = 60 * 2


class DownloaderState(Enum):
    Off = auto()
    Starting = auto()
    Awaiting = auto()
    Downloading = auto()
    Ratelimited = auto()


def downloader_state_to_string(state):
    match state:
        case DownloaderState.Off:
            return "OFF"
        case DownloaderState.Starting:
            return "STARTING"
        case DownloaderState.Awaiting:
            return "AWAITING"
        case DownloaderState.Downloading:
            return "DOWNLOADING"
        case DownloaderState.Ratelimited:
            return "RATELIMITED"
        case _:
            return "UNKNOWN_STATE"


@dataclass
class MangaDownloadJobInDatabase:
    # Stores the pages in database and record (how many pages does the chapter have)
    pages_in_db: Dict  # {"cuuid": [1, 2, 3, 4, 5], ...}
    records: Dict  # {"cuuid": 12, ...}


class MangaDownloadJob:
    def __init__(self, identifier: MangaIdentifier, manga_attribute: MangaAttributes, chapter_list: ChapterList,
                 database_info: MangaDownloadJobInDatabase, settings: Settings):
        self.identifier = identifier
        self.chapter_info = chapter_list
        self.database_info = database_info
        if self.chapter_info is not None and self.chapter_info.data is not None:
            self.title = get_correct_language(manga_attribute.title, manga_attribute.altTitles, settings)
        else:
            self.title = None
        self.settings = settings

    def __repr__(self):
        return f"<MangaDownloadJob(identifier={self.identifier}) at {hex(id(self))}>"

    def __del__(self):
        self.settings.logger.log(warning, f"Deleting job {self.__repr__()}")

    def __eq__(self, other):
        if isinstance(other, MangaDownloadJob):
            return self.identifier == other.identifier
        return False

    # to currently working on (json struct with info about state of download)
    def to_cwo(self):
        return {
            "id": self.identifier,
            "title": self.title,
            "chapter_status": [0, int(len(self.chapter_info.data))]
        }


@dataclass
class MangaDownload:
    pages: int
    data: [bytes]


async def async_get_chapter_page(
        identifier,
        db_pages: Sequence,
        logger: Logger,
        can_continue_download: Optional[Callable[[], bool]] = None,
        page_download_cb: Optional[Callable] = None
) -> Tuple[Optional[MangaDownload], bool]:
    """
    Async version of threaded_get_chapter_page.
    If rate_limit_callback returns False, retries after waiting.
    """

    async with aiohttp.ClientSession() as session:
        was_rate_limited = False
        async with session.get(f"https://api.mangadex.org/at-home/server/{identifier}") as metadata:
            if "X-RateLimit-Remaining" not in metadata.headers:
                logger.log(error, "Metadata do not contain X-RateLimit-Remaining")
                return None, was_rate_limited
            remaining = metadata.headers["X-RateLimit-Remaining"]
            retry_after = metadata.headers["X-RateLimit-Retry-After"]
            logger.log(info, f"RateLimit Rem. {remaining}. RateLimit after {retry_after}")

            if int(remaining) <= 0:
                was_rate_limited = True
                wait_time = (float(retry_after) - time.time()) + 5
                logger.log(debug, f"Rate limited, retrying after {wait_time}s...")
                await asyncio.sleep(wait_time)
                return None, was_rate_limited

            metadata_json = await metadata.json()
        try:
            _hash = metadata_json["chapter"]["hash"]
            base_url = metadata_json['baseUrl']
            pages = len(metadata_json["chapter"]["data"])
        except KeyError as e:
            logger.log(error, f"KeyError {e}: {metadata_json}")
            return None, was_rate_limited
        manga_download = MangaDownload(int(pages), [])

        # when using "asyncio.Semaphore(min(max(pages, 4), os.cpu_count() or 4))" some at-home server time us out for
        # too many request
        sem = asyncio.Semaphore(min(max(pages, 1), 8))

        downloaded_pages = []

        async def download_page(page_count: int, page_digest: str):
            if can_continue_download and not can_continue_download():
                return None

            async with sem:
                url = f"{base_url}/data/{_hash}/{page_digest}"
                try:
                    async with session.get(url) as resp:
                        resp.raise_for_status()
                        content = await resp.read()
                        logger.log(debug, f"Downloaded {page_digest}")

                        if page_download_cb:
                            page_download_cb(identifier, page_count + 1, pages)

                        return page_count + 1, content
                except Exception as e:
                    logger.log(error, f"Error downloading {page_digest}: {e}")
                    return None

        tasks = [
            download_page(page_count, page_digest)
            for page_count, page_digest in enumerate(metadata_json["chapter"]["data"])
            if page_count + 1 not in db_pages
        ]

        results = await asyncio.gather(*tasks)

        for result in filter(None, results):
            downloaded_pages.append(result)

        for i in sorted(downloaded_pages, key=lambda x: x[0]):
            manga_download.data.append(i[1])

        return manga_download, was_rate_limited


class JobsQueue:
    def __init__(self, settings):
        self.queue: Deque[MangaDownloadJob] = deque()
        self.condition = threading.Condition()
        self.logger = settings.logger

    def len(self):
        with self.condition:
            return len(self.queue)

    def snapshot(self) -> List[MangaDownloadJob]:
        with self.condition:
            return list(self.queue)

    def put(self, job: MangaDownloadJob):
        snapshot = self.snapshot()

        for job_in_queue in snapshot:
            if job_in_queue.identifier == job.identifier:
                self.logger.log(warning, "Trying to add job duplicate!")
                return

        with self.condition:
            self.queue.append(job)
            self.condition.notify()

    def pop(self) -> MangaDownloadJob:
        with self.condition:
            while not self.queue:
                self.condition.wait()
            return self.queue.popleft()

    def remove_job(self, identifier: str):
        with self.condition:
            for i, job in enumerate(self.queue):
                if job.identifier == identifier:
                    del self.queue[i]
                    break

    def push_to_front(self, identifier):
        with self.condition:
            for i, job in enumerate(self.queue):
                if job.identifier == identifier:
                    del self.queue[i]
                    self.queue.appendleft(job)
                    break


class MangaDownloader:
    def __init__(self, settings: Settings):
        self.state: DownloaderState = DownloaderState.Off

        self.stop_event = threading.Event()
        # Should be only called on the end
        self.exit_event = threading.Event()
        self.queue = JobsQueue(settings)
        self.finished = {}
        self.currently_working_on: Optional[dict] = None
        self.speed = "NORMAL"

        assert settings.onMangaDownloadFinishHandler is not None, "onMangaDownloadFinishHandler cannot be None"

        self.on_finish_callback = settings.onMangaDownloadFinishHandler
        self.logger = settings.logger
        self.worker_thread = threading.Thread(target=self.__loop)
        self.worker_thread.start()
        self.start()

    def start(self):
        if self.state == DownloaderState.Off:
            self.state = DownloaderState.Starting

    def stop(self):
        self.stop_event.set()

    def exit(self):
        self.exit_event.set()

    def add_chapter(self):
        self.currently_working_on["chapter_status"][0] += 1

    def rate_limit_callback(self, retry_after) -> bool:
        self.logger.log(warning, f"Halting execution of downloader. Sleeping for {retry_after + 30}s")
        # pause execution until we can try again
        time.sleep((retry_after + 5))
        return False

    def can_continue_downloading(self):
        return self.state != DownloaderState.Off or self.state != DownloaderState.Ratelimited

    def __loop(self):
        while not self.exit_event.is_set():  # Outer loop checks exit_event
            if self.state == DownloaderState.Off:
                time.sleep(0.1)
                continue

            if self.stop_event.is_set():
                self.state = DownloaderState.Off
                self.stop_event.clear()
                continue

            self.state = DownloaderState.Awaiting
            job = self.queue.pop()
            if self.exit_event.is_set() or self.stop_event.is_set():
                # the thread was waiting for object att it either got a job or None was sent to wakeup
                continue
            self.state = DownloaderState.Downloading
            self.currently_working_on = job.to_cwo()

            if not job.chapter_info.data:
                self.logger.log(warning, f"{job.identifier} data is empty!")

            for chapter in job.chapter_info.data:
                if self.stop_event.is_set() or self.exit_event.is_set():  # Check both events
                    self.queue.put(job)
                    self.currently_working_on = None
                    break  # Break out of the chapter processing loop

                try:
                    if len(job.database_info.pages_in_db[chapter.id]) == job.database_info.records[chapter.id]:
                        self.add_chapter()
                        self.logger.log(debug,
                                        f"already in database {chapter.attributes.volume} Volume {chapter.attributes.chapter} Chapter")
                        continue
                except KeyError:
                    pass

                pages_in_db = job.database_info.pages_in_db.get(chapter.id, [])

                MAX_RETRIES = 3

                downloaded_data = None
                was_rate_limited = False

                for attempt in range(1, MAX_RETRIES + 1):
                    downloaded_data, was_rate_limited = run_async(
                        async_get_chapter_page,
                        chapter.id,
                        pages_in_db,
                        self.logger,
                        self.can_continue_downloading,
                        None,
                    )

                    if downloaded_data is not None:
                        break

                if downloaded_data is None:
                    self.logger.log(warning, f"Could not download chapter {chapter.id}")
                    continue

                if job.identifier not in self.finished:
                    self.finished[job.identifier] = {}

                self.finished[job.identifier][chapter.id] = downloaded_data.data
                self.on_finish_callback(job.identifier, chapter.id, self)
                job.database_info.pages_in_db[chapter.id] = list(range(1, downloaded_data.pages + 1))
                self.logger.log(debug,
                                f"Downloaded {chapter.attributes.volume} Volume {chapter.attributes.chapter} Chapter")
                self.add_chapter()

                # time.sleep(timeouts[self.speed + "_CHAPTER_FINISH"])
            self.logger.log(debug, f"Finished downloading {job}!")

            if job.identifier in self.finished and len(self.finished[job.identifier]) == 0:
                self.finished.pop(job.identifier)
            self.currently_working_on = None

    def get_downloader_state(self) -> dict:
        queued_jobs = self.queue.snapshot()

        return {
            "state": downloader_state_to_string(self.state),
            "currently_working_on": self.currently_working_on,
            "queue": {job.identifier: job.title for job in queued_jobs},
        }


class CredentialManager:
    def __init__(self, settings: Settings, credentials: MangadexCredentials):
        self.settings = settings
        self.__credentials: MangadexCredentials = credentials
        self.__token = {}

        if not credentials.is_valid():
            self.settings.logger.log(error, "Credentials are not set properly")
            return

    def set_credentials(self, credentials: MangadexCredentials):
        if credentials.is_valid():
            self.__credentials = credentials
        else:
            self.settings.logger.log(error, "Credentials are not valid")

    def get_header_token(self):
        return {"Authorization": f"{self.token['token_type']} {self.token['access_token']}"}

    def __refresh_token(self) -> Optional[dict]:
        if not self.__credentials.is_valid():
            return None
        self.settings.logger.log(info, "refreshing token")
        response = requests.post("https://auth.mangadex.org/realms/mangadex/protocol/openid-connect/token", data={
            "grant_type": "refresh_token",
            "refresh_token": self.__token["refresh_token"],
            "client_id": self.__credentials.clientId,
            "client_secret": self.__credentials.clientSecret,
        })
        if response.status_code != 200:
            self.settings.logger.log(error, f"Failed to refresh token check if credentials are valid")
            return None
        token = response.json()
        token["created"] = time.time()
        return token

    def __get_token(self) -> Optional[dict]:
        if not self.__credentials.is_valid():
            return None
        self.settings.logger.log(info, "getting token")
        response = requests.post("https://auth.mangadex.org/realms/mangadex/protocol/openid-connect/token", data={
            "grant_type": "password",
            "username": self.__credentials.username,
            "password": self.__credentials.password,
            "client_id": self.__credentials.clientId,
            "client_secret": self.__credentials.clientSecret
        })
        if response.status_code != 200:
            self.settings.logger.log(error, f"Failed to get token check if credentials are valid")
            return None
        token = response.json()

        token["created"] = time.time()
        return token

    def validate_token(self, token) -> bool:
        if token is None:
            return False

        if "access_token" not in token:
            return False

        current_time = time.time()

        if token["created"] + token["expires_in"] > current_time:
            return True
        return False

    def __get_cached_token(self) -> Optional[dict]:
        if not os.path.isfile(".token"):
            return None

        with open(".token", "r") as file:
            token = json.load(file)

        if "access_token" not in token:
            return None

        current_time = time.time()

        # Check if the token is still valid
        if token["created"] + token["expires_in"] > current_time:
            return token

        return None

    def __cache_token(self, token: dict):
        self.settings.logger.log(info, "caching token")
        with open(".token", "w") as file:
            json.dump(token, file)

    @property
    def token(self) -> dict:
        if not self.__token:
            self.__token = self.__get_cached_token()

        if not self.__token:
            self.__token = self.__get_token()
            if self.settings.cacheTokenToDisk and self.validate_token(self.__token):
                self.__cache_token(self.__token)
            return self.__token

        if not self.validate_token(self.__token):
            current_time = time.time()
            refresh_expiry = self.__token["created"] + self.__token["refresh_expires_in"]

            if refresh_expiry > current_time:  # Refresh if valid
                self.__token = self.__refresh_token()
            else:
                self.__token = self.__get_token()

            if self.settings.cacheTokenToDisk and self.validate_token(self.__token):
                self.__cache_token(self.__token)

        return self.__token


class MangadexConnection:
    def __init__(self, settings, credentials_manager: CredentialManager):
        self.session = requests.Session()
        self.API = "https://api.mangadex.org"

        self.exclude_groups = []

        # default headers for every api call
        self.default_headers = {"contentRating[]": settings.contentRating}
        if settings.translatedLanguage:
            self.default_headers["translatedLanguage[]"] = settings.translatedLanguage
        self.logger = settings.logger
        self.credentials_manager = credentials_manager

        self.cover_file_name_cache = {}
        self.manga_aggregate_cache = {}
        self.manga_attributes_cache = {}

    def clear_caches(self):
        self.cover_file_name_cache.clear()
        self.manga_aggregate_cache.clear()
        self.manga_attributes_cache.clear()

    def safe_request(self, method: str, url: str, params=None, headers=None, _json=None, default_parameters=True,
                     default_parameter_exclude: List[str] = None) -> Optional[requests.Response]:
        if params is None:
            params = {}
        if headers is None:
            headers = {}
        if default_parameters:
            params.update(self.default_headers)
        if default_parameter_exclude:
            for parameter in default_parameter_exclude:
                params.pop(parameter, None)
        try:
            self.logger.log(debug, F"sending ({method}): {url}")
            return self.session.request(method=method, url=url, params=params, headers=headers, json=_json, timeout=4)
        except requests.exceptions.Timeout:
            self.logger.log(error, f"Request timed out when trying to reach {url}")
            return None
        except requests.exceptions.ConnectionError:
            self.logger.log(error, f"Connection error when trying to reach {url}")
            return None
        except requests.exceptions.RequestException as e:
            self.logger.log(error, f"An error occurred: {e}")
            return None

    def cache_cover_art_from_relationships(self, identifier: str, relationships: [Relationship]):
        for relationship in relationships:
            if relationship.type == "cover_art" and relationship.attributes is not None:
                if identifier not in self.cover_file_name_cache:
                    self.logger.log(debug, f"Caching cover art filename for {identifier}")
                    self.cover_file_name_cache[identifier] = {
                        "timestamp": time.time(),
                        "data": relationship.attributes["fileName"]
                    }

    # most queries that revolve around manga has cover_art filename saving us 1 mangadex api request
    def cache_cover_art_filename(self, manga_object: Any):
        if isinstance(manga_object, MangaList):
            for manga in manga_object.data:
                self.cache_cover_art_from_relationships(manga.id, manga.relationships)
        elif isinstance(manga_object, DirectSearchManga):
            self.cache_cover_art_from_relationships(manga_object.data.id, manga_object.data.relationships)
        elif isinstance(manga_object, Manga):
            self.cache_cover_art_from_relationships(manga_object.id, manga_object.relationships)
        else:
            self.logger.log(warning, f"Unsupported type {type(manga_object)}")

    def search_manga(self, name: str, limit: int) -> Optional[MangaList]:
        params = {"title": name, "limit": limit, "includes[]": ["cover_art"]}
        req = self.safe_request("GET", f"{self.API}/manga", params, default_parameter_exclude=["translatedLanguage[]"])
        if req and req.status_code != 200:
            return None

        query = req.json()

        if query["result"] != "ok":
            return None
        try:
            manga_list = from_json(MangaList, query)
            self.cache_cover_art_filename(manga_list)
            return manga_list
        except AttributeError:
            return None

    def get_manga(self, identifier: MangaIdentifier) -> Optional[Manga]:
        if identifier in self.manga_attributes_cache:
            if is_expired(self.manga_attributes_cache[identifier]["timestamp"], CACHE_MANGA_ATTRIBUTES_TTL):
                del self.manga_attributes_cache[identifier]
            else:
                return self.manga_attributes_cache[identifier]["data"]

        req = self.safe_request("GET", f"{self.API}/manga/{identifier}", params={"includes[]": ["cover_art"]})

        if req and req.status_code != 200:
            return None

        query = req.json()

        if query["result"] != "ok":
            return None
        try:
            direct_manga = from_json(DirectSearchManga, query).data
            self.cache_cover_art_filename(direct_manga)
            self.manga_attributes_cache[identifier] = {"timestamp": time.time(), "data": direct_manga}
            return direct_manga
        except AttributeError:
            return None

    def get_manga_aggregate(self, identifier: MangaIdentifier, params) -> Optional[dict]:
        # Using dict cache because if were reading manga from one group for extended time
        # all the aggregates are same and do not change. Caching makes sense
        groups = ",".join(sorted(params.get("groups[]", [])))
        langs = ",".join(sorted(params.get("translatedLanguage[]", [])))
        cache_key = f"{groups}:{langs}:{identifier}"
        if cache_key in self.manga_aggregate_cache:
            if is_expired(self.manga_aggregate_cache[cache_key]["timestamp"], CACHE_AGGREGATE_TTL):
                del self.manga_aggregate_cache[cache_key]
            else:
                return self.manga_aggregate_cache[cache_key]["data"]
        req = self.safe_request("GET", f"{self.API}/manga/{identifier}/aggregate", params=params,
                                default_parameters=False)
        if req and req.status_code != 200:
            return None

        query = req.json()

        if query["result"] != "ok":
            return None
        try:
            self.manga_aggregate_cache[cache_key] = {"timestamp": time.time(), "data": query}
            return query  # TODO: Add scheme if needed
        except AttributeError:
            return None

    @perf_test
    def get_cover_art(self, identifier: str, size=COVER_ART_MAX_SIZE) -> Optional[bytes]:
        # as defined in https://api.mangadex.org/docs/03-manga/covers/
        size_suffix = ""
        if size == COVER_ART_256_SIZE:
            size_suffix = ".256.jpg"
        elif size == COVER_ART_512_SIZE:
            size_suffix = ".512.jpg"

        if identifier in self.cover_file_name_cache:
            if is_expired(self.cover_file_name_cache[identifier]["timestamp"], CACHE_COVER_ART_TTL):
                del self.cover_file_name_cache[identifier]
            else:
                cover_url = f"https://uploads.mangadex.org/covers/{identifier}/" + \
                            self.cover_file_name_cache[identifier]["data"] + size_suffix
                cover_art = self.safe_request("GET", cover_url)
                self.logger.log(debug, "Getting coverart from cached filename!")
                return cover_art.content

        req = self.safe_request("GET", f"{self.API}/manga/{identifier}", params={"includes[]": ["cover_art"]})

        if req and req.status_code != 200:
            return None

        query = req.json()

        if query["result"] != "ok":
            return None

        manga = from_json(DirectSearchManga, query)
        if manga is None:
            return None

        for relationship in manga.data.relationships:
            if relationship.type == "cover_art":
                coverurl = f"https://uploads.mangadex.org/covers/{identifier}/" + relationship.attributes[
                    "fileName"] + size_suffix
                return self.safe_request("GET", coverurl).content

    def get_chapter_list(self, identifier: MangaIdentifier, lang: str = "en") -> Optional[ChapterList]:
        params = {
            "manga": identifier,
            "limit": 100,
            "offset": 0,
            "translatedLanguage[]": lang,
            "excludedGroups[]": self.exclude_groups,
            "includes[]": ["scanlation_group", "user"],
            "includeEmptyPages": 0,
            "includeExternalUrl": 0
        }
        req = self.safe_request("GET", url=f"{self.API}/chapter", params=params)

        if req and req.status_code != 200:
            return None

        query = req.json()

        if query["result"] != "ok":
            return None

        chapter_list: ChapterList = from_json(ChapterList, req.json())
        while chapter_list.total > len(chapter_list.data):
            params["offset"] += 100
            req = self.safe_request("GET", url=f"{self.API}/chapter", params=params)

            if req.status_code != 200:
                self.logger.log(error, f"API returned {req.status_code} while getting rest of the chapters")
                return chapter_list
            query = req.json()
            if query["result"] != "ok":
                self.logger.log(error, f"API returned result {query['result']} while getting rest of the chapters")
                return None
            if not query["data"]:
                return chapter_list
            new_chapter_list: ChapterList = from_json(ChapterList, query)

            chapter_list.data.extend(new_chapter_list.data)

        return chapter_list

    def get_manga_feed(self, identifier: MangaIdentifier, lang: Union[str, List[str]] = "en") -> Optional[ChapterList]:
        params = {
            "limit": 100,
            "offset": 0,
            "translatedLanguage[]": lang,
            "excludedGroups[]": self.exclude_groups,
            "includes[]": ["scanlation_group", "user"],
            "includeEmptyPages": 0,
            "includeExternalUrl": 0,
            "includeUnavailable": 0,
            "order[volume]": "desc",
            "order[chapter]": "desc"
        }
        req = self.safe_request("GET", url=f"{self.API}/manga/{identifier}/feed", params=params)

        if req and req.status_code != 200:
            return None

        query = req.json()

        if query["result"] != "ok":
            return None

        chapter_list: ChapterList = from_json(ChapterList, req.json())
        while chapter_list.total > len(chapter_list.data):
            params["offset"] += 100
            req = self.safe_request("GET", url=f"{self.API}/manga/{identifier}/feed", params=params)

            if req.status_code != 200:
                self.logger.log(error, f"API returned {req.status_code} while getting rest of the chapters")
                return chapter_list
            query = req.json()
            if query["result"] != "ok":
                self.logger.log(error, f"API returned result {query['result']} while getting rest of the chapters")
                return None
            if not query["data"]:
                return chapter_list
            new_chapter_list: ChapterList = from_json(ChapterList, query)

            chapter_list.data.extend(new_chapter_list.data)

        return chapter_list

    def get_chapter(self, identifier: ChapterIdentifier) -> Optional[Chapter]:
        req = self.safe_request("GET", url=f"{self.API}/chapter/{identifier}")
        if req and req.status_code != 200:
            return None
        chapter_info = req.json()
        if chapter_info["result"] != "ok":
            return None
        dsc = from_json(DirectSearchChapter, chapter_info)
        return dsc.data

    def get_manga_uuid_from_chapter(self, identifier: ChapterIdentifier) -> Optional[str]:
        chapter = self.get_chapter(identifier)
        for relationship in chapter.relationships:
            if relationship.type == "manga":
                return relationship.id
        return None

    def get_popular_new_titles(self) -> Optional[MangaList]:
        now = datetime.now()
        month_back = now - relativedelta(months=1)
        set_time = month_back.replace(hour=23, minute=0, second=0, microsecond=0)
        data = self.safe_request("GET", url=f"{self.API}/manga", params={
            "includes[]": ["artist", "cover_art", "author"],
            "order[followedCount]": "desc",
            "hasAvailableChapters": "true",
            "createdAtSince": set_time.isoformat()
        }, default_parameter_exclude=["translatedLanguage[]"])
        try:
            manga_list: MangaList = from_json(MangaList, data.json())
            self.cache_cover_art_filename(manga_list)
        except AttributeError as e:
            self.logger.log(error, str(e))
            return None
        if not manga_list:
            return None
        return manga_list

    def get_custom_list_feed(self, custom_list_id, limit, offset) -> Union[None, ChapterList, int]:
        """
        int is returned when credentials are not set right
        None is returned when there is error while getting the feed
        ChapterList when api returns successfully
        """
        if not self.credentials_manager:
            self.logger.log(warning, "Credentials manager not configured")
            return -1
        req = self.safe_request("GET", f"{self.API}/list/{custom_list_id}/feed",
                                headers=self.credentials_manager.get_header_token(),
                                params={"limit": limit, "offset": offset, "order[updatedAt]": "desc"})
        if req and req.status_code != 200:
            self.logger.log(error, f"API returned result {req.status_code} while getting feed")
            return None
        chapter_info = req.json()
        if chapter_info["result"] != "ok":
            self.logger.log(error, json.dumps(chapter_info))
            return None
        return from_json(ChapterList, req.json())

    def get_user_custom_lists(self) -> CustomListResponse:
        data = self.safe_request("GET", url=f"{self.API}/user/list",
                                 headers=self.credentials_manager.get_header_token(), default_parameters=False,
                                 params={"limit": 100})
        return from_json(CustomListResponse, data.json())

    def create_custom_list(self, mangas: List[str]) -> bool:
        response = self.safe_request("POST",
                                     url=f"{self.API}/list",
                                     headers=self.credentials_manager.get_header_token(),
                                     default_parameters=False,
                                     _json={
                                         "name": "local-mangadex-sync",
                                         "visibility": "private",
                                         "manga": mangas,
                                         "version": 1
                                     }
                                     )
        return response.status_code == 200

    def get_sync_list(self) -> Tuple[str, CustomListResponse]:
        user_custom_lists = self.get_user_custom_lists()
        sync_list_uuid = None
        for custom_list in user_custom_lists.data:
            if custom_list.attributes.name == "local-mangadex-sync":
                if sync_list_uuid:
                    self.logger.log(warning, "Multiple MDlists with name local-mangadex-sync")
                else:
                    sync_list_uuid = custom_list.id
        return sync_list_uuid, user_custom_lists

    @perf_test
    def sync_custom_list(self, manga_uuids: List[MangaIdentifier]) -> bool:
        sync_list_uuid, sync_lists = self.get_sync_list()
        if sync_list_uuid is None:
            return self.create_custom_list(manga_uuids)
        sync_list: CustomList = next((x for x in sync_lists.data if x.id == sync_list_uuid), None)
        sync_list_uuids = []
        version = sync_list.attributes.version
        for relationship in sync_list.relationships:
            if relationship.type == "manga":
                sync_list_uuids.append(relationship.id)
        if sorted(sync_list_uuids) == sorted(manga_uuids):
            self.logger.log(debug, "Libraries are already synced")
            return True
        response = self.safe_request("PUT",
                                     url=f"{self.API}/list/{sync_list_uuid}",
                                     headers=self.credentials_manager.get_header_token(),
                                     default_parameters=False,
                                     _json={
                                         "name": "local-mangadex-sync",
                                         "visibility": "private",
                                         "manga": manga_uuids,
                                         "version": version
                                     }
                                     )
        if response.status_code == 409:
            self.logger.log(warning, "Conflict! If you have opened the MDlist in your browser please close it")
        return response.status_code == 200

    def get_followed_manga(self) -> Optional[MangaList]:
        params = {"limit": 100, "offset": 0, "includes[]": ["cover_art"]}
        req = self.safe_request("GET",
                                url=f"{self.API}/user/follows/manga",
                                headers=self.credentials_manager.get_header_token(),
                                default_parameters=False,
                                params=params
                                )

        if req and req.status_code != 200:
            return None

        query = req.json()

        if query["result"] != "ok":
            return None

        manga_list: MangaList = from_json(MangaList, req.json())
        while manga_list.total > len(manga_list.data):
            params["offset"] += 100
            req = self.safe_request("GET",
                                    url=f"{self.API}/user/follows/manga",
                                    headers=self.credentials_manager.get_header_token(),
                                    default_parameters=False,
                                    params=params
                                    )

            if req and req.status_code != 200:
                self.logger.log(error, f"API returned {req.status_code} while getting rest of the chapters")
                return manga_list

            query = req.json()

            if query["result"] != "ok":
                self.logger.log(error, f"API returned result {query['result']} while getting rest of the chapters")
                return None

            if not query["data"]:
                return manga_list

            new_manga_list: MangaList = from_json(MangaList, query)
            manga_list.data.extend(new_manga_list.data)
        self.cache_cover_art_filename(manga_list)
        return manga_list

    def get_latest_updated_chapters(self) -> ChapterList | None:
        params = {"limit": 100, "includes[]": ["scanlation_group", "manga"], "order[readableAt]": "desc"}
        req = self.safe_request("GET",
                                url=f"{self.API}/chapter",
                                default_parameters=True,
                                params=params
                                )
        if req and req.status_code != 200:
            self.logger.log(error, f"API returned code {req.status_code} when getting latest updates")
            return None

        query = req.json()

        if query["result"] != "ok":
            return None

        return from_json(ChapterList, req.json())

# simple download to FS
# def on_download(muuid: MangaIdentifier, cuuid: ChapterIdentifier, downloader: MangaDownloader):
#     if not os.path.isdir(f"{os.getcwd()}/download/{muuid}"):
#         os.mkdir(f"{os.getcwd()}/download/{muuid}")
#     if not os.path.isdir(f"{os.getcwd()}/download/{muuid}/{cuuid}"):
#         os.mkdir(f"{os.getcwd()}/download/{muuid}/{cuuid}")
#     for index, page in enumerate(downloader.finished[muuid][cuuid]):
#         with open(f"{os.getcwd()}/download/{muuid}/{cuuid}/{index}.png", "wb") as file:
#             file.write(page)
#     del downloader.finished[muuid][cuuid]
#
