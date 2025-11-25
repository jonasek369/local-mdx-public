import json
import os
import shutil
import struct
import threading
import time
from typing import Optional, Tuple, Dict, Union, List, Any

import requests
from dateutil.relativedelta import relativedelta
from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer

from rewrite.backend.database import Database
from rewrite.backend.schemas import MangaList, from_json, Manga, MangaIdentifier, ChapterIdentifier, ChapterList, \
    Chapter, \
    DirectSearchManga, DirectSearchChapter, CustomList, CustomListResponse, COVER_ART_256_SIZE, \
    COVER_ART_512_SIZE, COVER_ART_MAX_SIZE, Relationship
from rewrite.backend.settings import Settings, MangadexCredentials
from datetime import datetime

from rewrite.backend.utils import error, info, warning, perf_test, critical

import subprocess


class DownloaderHandler(FileSystemEventHandler):
    def __init__(self, database: Database):
        self.database = database

    def save_chapter(self, finished_src):
        finished_full_path = os.path.join(os.getcwd(), finished_src)
        directory_path = os.path.dirname(finished_full_path)
        chapterid = os.path.basename(directory_path)
        assert chapterid != "downloads", "FINISHED was created in main directory"
        pages = []

        for file in os.listdir(directory_path):
            if file == "FINISHED":
                continue
            try:
                n_str = "".join([c for c in file.split("-")[0] if c.isdigit()])
                if not n_str:
                    raise ValueError("No digits in filename")
                n = int(n_str)
                with open(os.path.join(directory_path, file), "rb") as f:
                    pages.append((n, f.read(), chapterid))
            except Exception as e:
                print(f"Could not process {file} in {directory_path}: {e}")

        pages.sort(key=lambda x: x[0])

        try:
            self.database.set_chapter_pages(pages, is_webp=True)
            shutil.rmtree(directory_path)
        except Exception as e:
            print(f"Could not save {chapterid} to database: {e}")

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type == "created" or event.event_type == "modified":
            if event.src_path.endswith("FINISHED"):
                self.save_chapter(event.src_path)


class DownloaderProcessHandler:
    def __init__(self, settings: Settings, database: Database):
        self.logger = settings.logger
        if not os.path.exists("main.exe"):
            self.logger.log(critical,
                            f"Could not find downloader program. Compile it and put it in {os.getcwd()}. https://github.com/jonasek369/C-manga-downloader")

        self.downloader_proc = subprocess.Popen(
            ["main.exe"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=False
        )

        timeout = 4
        start = time.time()

        while time.time() - start < timeout:
            if os.path.exists("downloads"):
                self.logger.log(info, "Folder 'downloads' detected")
                break
            time.sleep(0.1)
        else:
            self.logger.log(critical, "Timed out! Folder 'downloads' was not created.")

        self.response_buffer = []

        self.logger.log(info, f"started downloader proc! {self.downloader_proc}")
        self.exit_event = threading.Event()

        event_handler = DownloaderHandler(database)
        self.observer = Observer()
        self.observer.schedule(event_handler, "downloads", recursive=True)
        self.observer.start()

        threading.Thread(target=self.read_messages, args=(self.downloader_proc,)).start()

    def exit(self):
        self.exit_event.set()
        self.send_message({"command": "exit"})
        self.logger.log(info, "waiting for process to stop")
        self.downloader_proc.wait()
        time.sleep(1)  # sleeping because after process finishes it will most probably finish one more chapter so
        # so we make sure to save it
        self.logger.log(info, "process stopped")
        self.observer.stop()
        self.observer.join()

    def clear_responses(self):
        self.response_buffer.clear()

    def wait_for_response(self, timeout=5.0) -> dict | None:
        end_time = time.time() + timeout

        while time.time() < end_time:
            if self.response_buffer:
                return self.response_buffer.pop(0)
        return None

    def read_exact(self, stream, n):
        buf = b''
        while len(buf) < n:
            chunk = stream.read(n - len(buf))
            buf += chunk
        return buf

    def read_messages(self, proc):
        while not self.exit_event.is_set():
            raw_len = self.read_exact(proc.stdout, 4)
            if not raw_len:
                print("Process ended or pipe closed.")
                break

            msg_len = struct.unpack("<I", raw_len)[0]
            data = self.read_exact(proc.stdout, msg_len)
            if not data:
                print("Incomplete JSON payload. Process may have terminated.")
                break

            try:
                msg = json.loads(data.decode("utf-8"))
                self.response_buffer.append(msg)
            except Exception as e:
                print("JSON decode error:", e)
                print("Raw data:", data)
                break

    def send_message(self, message: Dict) -> bool:
        if "command" not in message:
            self.logger.log(error, f"Message to proc must have command field!")
            return False
        json_str = json.dumps(message)
        json_bytes = json_str.encode('utf-8')
        length = len(json_bytes)

        self.downloader_proc.stdin.write(struct.pack('<I', length))

        self.downloader_proc.stdin.write(json_bytes)
        self.downloader_proc.stdin.flush()


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

    def cache_cover_art_from_relationships(self, identifier: MangaIdentifier, relationships: [Relationship]):
        for relationship in relationships:
            if relationship.type == "cover_art" and relationship.attributes is not None:
                if identifier not in self.cover_file_name_cache:
                    self.logger.log(info, f"Caching cover art filename for {identifier}")
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

        cache_hit: Optional[str] = self.cover_file_name_cache.get(identifier, None)
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
                coverurl = f"https://uploads.mangadex.org/covers/{identifier}/" + relationship.attributes[
                    "fileName"] + size_suffix
                self.cover_file_name_cache[identifier] = relationship.attributes["fileName"]
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
