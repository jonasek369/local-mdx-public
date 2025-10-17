import asyncio
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple, Callable, Dict, Union, List, Any

import aiohttp
import eventlet
import requests
from dateutil.relativedelta import relativedelta
from flask_socketio import SocketIO

from rewrite.backend.schemas import MangaList, from_json, Manga, MangaIdentifier, ChapterIdentifier, ChapterList, \
    Chapter, \
    DirectSearchManga, MangaAttributes, DirectSearchChapter, CustomList, CustomListResponse, COVER_ART_256_SIZE, \
    COVER_ART_512_SIZE, COVER_ART_MAX_SIZE, Relationship
from enum import Enum, auto
from rewrite.backend.settings import Settings, MangadexCredentials
import concurrent.futures
from datetime import datetime

from rewrite.backend.utils import Logger, error, info, warning, perf_test, get_correct_language, run_async


class DownloaderState(Enum):
    Off = auto()
    Starting = auto()
    Awaiting = auto()
    Downloading = auto()
    Cancelling = auto()


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
        case DownloaderState.Cancelling:
            return "CANCELLING"
        case _:
            return "UNKNOWN_STATE"

@dataclass
class MangaDownloadJobInDatabase:
    # Stores the pages in database and record (how many pages does the chapter have)
    pages_in_db: Dict  # {"cuuid": [1,2, 3, 4, 5], ...}
    records: Dict  # {"cuuid": 12, ...}


class MangaDownloadJob:
    # pages_in_db = {"cuuid": [1,2, 3, 4, 5]}
    def __init__(self, identifier: MangaIdentifier, manga_attribute: MangaAttributes, chapter_list: ChapterList,
                 database_info: MangaDownloadJobInDatabase, settings: Settings):
        self.identifier = identifier
        self.downloaded = False
        self.chapter_info = chapter_list
        self.database_info = database_info
        if self.chapter_info.data is not None:
            self.title = get_correct_language(manga_attribute.title, settings)
        else:
            self.title = None
        self.settings = settings
        self.settings.logger.log(info, "Created job sucesfully")

    def __del__(self):
        self.settings.logger.log(warning, "Deleting job")

    def __eq__(self, other):
        if isinstance(other, MangaDownloadJob):
            return self.identifier == other.identifier
        return False

    # to currently working on (json struct with info about state of download)
    def to_cwo(self):
        return {
            "id": self.identifier,
            "title": self.title,
            "chapter_status": [0, int(len(self.chapter_info.data))],
            "page_status": ["?", "?"]
        }


class MangaQueue:
    def __init__(self):
        self.__queue: [MangaDownloadJob] = []

    @property
    def first(self) -> Optional[MangaDownloadJob]:
        if len(self.__queue) == 0:
            return None
        return self.__queue[0]

    def next(self) -> Optional[MangaDownloadJob]:
        if self.__queue:
            return self.__queue.pop(0)
        else:
            return None

    def add_job(self, job: MangaDownloadJob) -> bool:
        if job not in self.__queue:
            self.__queue.append(job)
            return True
        return False

    def remove_job(self, _id: str):
        for index, job in enumerate(self.__queue):
            if job.downloaded or job.identifier == _id:
                self.__queue.pop(index)

    def get_job_index(self, _id: str) -> Optional[int]:
        for index, job in enumerate(self.__queue):
            if job.identifier == _id:
                return index

    def pop_job_index(self, index: int) -> Optional[MangaDownloadJob]:
        if self.__queue:
            return self.__queue.pop(index)
        return None

    def in_queue(self, _id: str):
        return _id in self.__queue

    def push_to_top(self, index):
        self.__queue.insert(0, self.__queue.pop(index))

    def __iter__(self):
        return self.__queue.__iter__()

    def __len__(self):
        return self.__queue.__len__()

    def add_to_top(self, job: MangaDownloadJob):
        self.__queue.insert(0, job)


@dataclass
class MangaDownload:
    pages: int
    data: [bytes]


def threaded_get_chapter_page(
        identifier: ChapterIdentifier,
        db_pages: Sequence,
        logger: Logger,
        rate_limit_callback: Optional[Callable] = None,
        can_continue_download: Optional[Callable] = None,
        page_download_cb: Optional[Callable] = None) -> Optional[MangaDownload]:
    """
    if rate_limit_callback returns False it will retry
    """
    metadata = requests.get(f"https://api.mangadex.org/at-home/server/{identifier}")
    remaining = metadata.headers.get("X-RateLimit-Remaining")
    retry_after = metadata.headers.get("X-RateLimit-Retry-After")
    logger.log(info, f"RateLimit Rem. {remaining}. RateLimit after {retry_after}")
    if int(remaining) <= 0:
        if rate_limit_callback(float(retry_after) - time.time()):
            return None
        else:
            logger.log(warning, "Recursing!")
            return threaded_get_chapter_page(identifier, db_pages, logger, rate_limit_callback, can_continue_download,
                                             page_download_cb)
    metadata = metadata.json()
    _hash = metadata["chapter"]["hash"]
    baseUrl = metadata['baseUrl']
    pages = len(metadata["chapter"]["data"])
    manga_download = MangaDownload(int(pages), [])
    session = requests.Session()

    def download_page(page_count, page_digest):
        page = session.get(f"{baseUrl}/data/{_hash}/{page_digest}")
        logger.log(info, f"Getting {page_digest}")
        return page_count + 1, page.content

    downloaded_pages = []
    # this is for mangas that have 1-4 pages per chapter because im not sure if ThreadPoolExecutor handles that
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(max(pages, 4), os.cpu_count())) as executor:
        futures = [executor.submit(download_page, page_count, page_digest)
                   for page_count, page_digest in enumerate(metadata["chapter"]["data"])
                   if page_count + 1 not in db_pages and (can_continue_download() if can_continue_download else True)]
        for future in concurrent.futures.as_completed(futures):
            try:
                downloaded_pages.append(future.result())
                if page_download_cb:
                    page_download_cb(identifier, downloaded_pages[-1][0], pages)
            except Exception as e:
                logger.log(error, f"An error occurred: {e}")

    for i in sorted(downloaded_pages, key=lambda x: x[0]):
        manga_download.data.append(i[1])

    return manga_download


async def async_get_chapter_page(
        identifier,
        db_pages: Sequence,
        logger: Logger,
        rate_limit_callback: Optional[Callable[[float], bool]] = None,
        can_continue_download: Optional[Callable[[], bool]] = None,
        page_download_cb: Optional[Callable] = None
) -> Optional[MangaDownload]:
    """
    Async version of threaded_get_chapter_page.
    If rate_limit_callback returns False, retries after waiting.
    """

    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://api.mangadex.org/at-home/server/{identifier}") as metadata:
            remaining = metadata.headers.get("X-RateLimit-Remaining", "1")
            retry_after = metadata.headers.get("X-RateLimit-Retry-After", "1")
            logger.log(info, f"RateLimit Rem. {remaining}. RateLimit after {retry_after}")

            if int(remaining) <= 0:
                wait_time = float(retry_after) - time.time()
                if rate_limit_callback and rate_limit_callback(wait_time):
                    return None
                else:
                    logger.log(warning, f"Rate limited, retrying after {wait_time}s...")
                    await asyncio.sleep(wait_time)
                    return await async_get_chapter_page(identifier, db_pages, logger,
                                                        rate_limit_callback, can_continue_download, page_download_cb)

            metadata_json = await metadata.json()
        try:
            _hash = metadata_json["chapter"]["hash"]
            base_url = metadata_json['baseUrl']
            pages = len(metadata_json["chapter"]["data"])
        except KeyError as e:
            logger.log(error, f"KeyError {e}: {metadata_json}")
        manga_download = MangaDownload(int(pages), [])


        sem = asyncio.Semaphore(min(max(pages, 4), os.cpu_count() or 4))
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
                        logger.log(info, f"Downloaded {page_digest}")

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

        return manga_download


timeouts = {
    "NO_LIMIT_CHAPTER_FINISH": 0,
    "FAST_CHAPTER_FINISH": 1,
    "NORMAL_CHAPTER_FINISH": 2.5,
    "SLOW_CHAPTER_FINISH": 5,
}


class MangaDownloader:
    def __init__(self, settings: Settings, sio: SocketIO):
        self.state: DownloaderState = DownloaderState.Off

        self.stop_event = threading.Event()
        # Should be only called on the end
        self.exit_event = threading.Event()
        self.queue = MangaQueue()
        self.finished = {}
        self.currently_working_on: Optional[dict] = None
        self.speed = "NORMAL"

        assert settings.onMangaDownloadFinishHandler is not None, "onMangaDownloadFinishHandler cannot be None"

        self.on_finish_callback = settings.onMangaDownloadFinishHandler
        self.logger = settings.logger
        assert sio is not None, "socketIO cannot be None"
        self.socketio = sio
        self.socketio.start_background_task(target=self.__loop)

    def start(self):
        if self.state == DownloaderState.Off:
            self.state = DownloaderState.Starting

    def stop(self):
        self.stop_event.set()

    def exit(self):
        self.exit_event.set()

    def add_chapter(self):
        self.currently_working_on["chapter_status"][0] += 1
        self.currently_working_on["page_status"] = ["?", "?"]

    def rate_limit_callback(self, retry_after) -> bool:
        self.logger.log(warning, f"Halting execution of downloader. Sleeping for {retry_after + 30}s")
        # pause execution until we can try again
        time.sleep((retry_after + 30))
        return False

    def can_continue_downloading(self):
        return self.state != DownloaderState.Off

    def page_download_callback(self, identifier, at_page, page_total):
        if self.currently_working_on["page_status"] == ["?", "?"]:
            self.currently_working_on["page_status"] = [0, page_total]
        self.currently_working_on["page_status"][0] += 1

    def __loop(self):
        while not self.exit_event.is_set():  # Outer loop checks exit_event
            if self.state == DownloaderState.Off:
                eventlet.sleep(0.1)
                continue

            if self.stop_event.is_set():
                self.state = DownloaderState.Off
                self.stop_event.clear()
                continue

            job = self.queue.next()
            if not job:
                self.state = DownloaderState.Awaiting
                eventlet.sleep(0.01)
                continue

            self.state = DownloaderState.Downloading
            self.currently_working_on = job.to_cwo()

            if not job.chapter_info.data:
                self.logger.log(warning, f"{job.identifier} data is empty!")

            self.socketio.emit("update", self.get_downloader_state())

            for chapter in job.chapter_info.data:
                if self.stop_event.is_set() or self.exit_event.is_set():  # Check both events
                    self.queue.add_to_top(job)
                    self.currently_working_on = None
                    self.socketio.emit("update", self.get_downloader_state())
                    break  # Break out of the chapter processing loop

                try:
                    if len(job.database_info.pages_in_db[chapter.id]) == job.database_info.records[chapter.id]:
                        self.add_chapter()
                        self.logger.log(info,
                                        f"already in database {chapter.attributes.volume} Volume {chapter.attributes.chapter} Chapter")
                        self.socketio.emit("update", self.get_downloader_state())
                        continue
                except KeyError:
                    pass

                pages_in_db = job.database_info.pages_in_db.get(chapter.id, [])
                downloaded_data = run_async(async_get_chapter_page,
                    chapter.id,
                    pages_in_db,
                    self.logger,
                    None,
                    self.can_continue_downloading,
                    self.page_download_callback
                )

                if job.identifier not in self.finished:
                    self.finished[job.identifier] = {}

                self.finished[job.identifier][chapter.id] = downloaded_data.data
                self.on_finish_callback(job.identifier, chapter.id, self)
                self.logger.log(info,
                                f"Downloaded {chapter.attributes.volume} Volume {chapter.attributes.chapter} Chapter")
                self.add_chapter()

                self.socketio.emit("update", self.get_downloader_state())
                eventlet.sleep(timeouts[self.speed + "_CHAPTER_FINISH"])
            self.logger.log(info, f"Finished downloading {job}!")
            self.currently_working_on = None
            self.socketio.emit("update", self.get_downloader_state())

    # state for socket
    def get_downloader_state(self) -> dict:
        return {
            "speed": self.speed,
            "state": downloader_state_to_string(self.state),
            "currently_working_on": self.currently_working_on,
            "queue": {job.identifier: job.title for job in self.queue}
        }


def save_credentials(credentials: MangadexCredentials):
    with open("mangadex_account.json", "w") as file:
        json.dump({
            "username": credentials.username,
            "password": credentials.password,
            "client_id": credentials.clientId,
            "client_secret": credentials.clientSecret
        }, file)


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
        self.settings.logger.log(info, "refreshing token")
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
            self.logger.log(info, F"sending={method}: {url}")
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
                self.cover_file_name_cache[identifier] = relationship.attributes["fileName"]

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
        req = self.safe_request("GET", f"{self.API}/manga/{identifier}", params={"includes[]": ["cover_art"]})

        if req and req.status_code != 200:
            return None

        query = req.json()

        if query["result"] != "ok":
            return None
        try:
            direct_manga = from_json(DirectSearchManga, query).data
            self.cache_cover_art_filename(direct_manga)
            return direct_manga
        except AttributeError:
            return None

    @perf_test
    def get_cover_art(self, identifier: MangaIdentifier, size=COVER_ART_MAX_SIZE) -> Optional[bytes]:
        # as defined in https://api.mangadex.org/docs/03-manga/covers/
        size_suffix = ""
        if size == COVER_ART_256_SIZE:
            size_suffix = ".256.jpg"
        elif size == COVER_ART_512_SIZE:
            size_suffix = ".512.jpg"

        cache_hit: str | None = self.cover_file_name_cache.get(identifier, None)
        if cache_hit is not None:
            cover_url = f"https://uploads.mangadex.org/covers/{identifier}/" + cache_hit + size_suffix
            cover_art = self.safe_request("GET", cover_url)
            self.logger.log(info, "Getting coverart from cached filename!")
            return cover_art.content

        req = self.safe_request("GET", f"{self.API}/manga/{identifier}", params={"includes[]": ["cover_art"]})

        if req and req.status_code != 200:
            return None

        query = req.json()

        if query["result"] != "ok":
            return None

        for relationship in from_json(DirectSearchManga, query).data.relationships:
            if relationship.type == "cover_art":
                coverurl = f"https://uploads.mangadex.org/covers/{identifier}/" + relationship.attributes["fileName"] + size_suffix
                self.cover_file_name_cache[identifier] = relationship.attributes["fileName"]
                return self.safe_request("GET", coverurl).content

    def get_chapter_list(self, identifier: ChapterIdentifier, lang: str = "en") -> Optional[ChapterList]:
        params = {
            "manga": identifier,
            "limit": 100,
            "offset": 0,
            "translatedLanguage[]": lang,
            "excludedGroups[]": self.exclude_groups,
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
            "includes[]": ["artists", "cover_art", "author"],
            "order[followedCount]": "desc",
            "hasAvailableChapters": "true",
            "createdAtSince": set_time.isoformat()
        }, default_parameter_exclude=["translatedLanguage[]"])
        # TODO: parse out data that is usefull like a cover art
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
            self.logger.log(info, "Libraries are already synced")
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
            self.logger.log(info, "Conflict! If you have opened the MDlist in your browser please close it")
        return response.status_code == 200

    def get_followed_manga(self) -> MangaList | None:
        params = {"limit": 100, "offset": 0, "inclues[]": ["cover_art"]}
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


# simple download to FS
def on_download(muuid: MangaIdentifier, cuuid: ChapterIdentifier, downloader: MangaDownloader):
    if not os.path.isdir(f"{os.getcwd()}/download/{muuid}"):
        os.mkdir(f"{os.getcwd()}/download/{muuid}")
    if not os.path.isdir(f"{os.getcwd()}/download/{muuid}/{cuuid}"):
        os.mkdir(f"{os.getcwd()}/download/{muuid}/{cuuid}")
    for index, page in enumerate(downloader.finished[muuid][cuuid]):
        with open(f"{os.getcwd()}/download/{muuid}/{cuuid}/{index}.png", "wb") as file:
            file.write(page)
    del downloader.finished[muuid][cuuid]
