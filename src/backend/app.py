import asyncio
import json
import os
import random
import sqlite3
import string
import threading
import time
from collections.abc import MutableSequence, Set, Generator
from datetime import datetime
from gzip import compress, decompress
from typing import Callable, Tuple, Optional, Sequence, Union, List
# ALERT SYSTEM
from uuid import UUID

import aiohttp
import discord
import pypresence.exceptions
from discord import File
from discord import Webhook, Member
from discord.abc import MISSING
from pypresence import Presence

from custom_logger import Logger, info, warn, erro
from hashlib import sha256
from PIL import Image
import io
import concurrent.futures

# for testing the offline mode
DISABLE_INTERNET_CONNECTION = 0
REDIS_CACHING = 1

if DISABLE_INTERNET_CONNECTION:
    class empty_requests:
        def get(self, *args, **kwargs):
            return None

        def post(self, *args, **kwargs):
            return None

        def Session(self, *args, **kwargs):
            return empty_requests()


    requests = empty_requests()
else:
    import requests

Plogger = Logger(1)


class Settings:
    def __init__(self):
        self.default: dict = {
            "LogLevel": 1,
            "redisCaching": False,
            "OnStart": {
                "cacheMangas": False
            },
            "DownloadProcessor": {
                "defaultSpeedMode": "NO_LIMIT",
                "useThreading": True,
                "silentDownload": True,
                "saveQueueOnExit": True,
                "runOnStart": True,
                "haltOnRateLimitReach": True
            },
            "MangadexConnection": {
                "excludedGroups": [
                    "4f1de6a2-f0c5-4ac5-bce5-02c7dbb67deb"
                ],
                "cacheChapterInfoToDatabase": True,
                "contentRating": []
            },
            "AlertSystem": {
                "discordWebhook": "",
                "discordRecipient": -1,
                "soundAlertOnRelease": False,
                "downloadStartOnRelease": True,
                "watchedMangas": [

                ],
                "cooldown": 300
            },
            "DiscordIntegration": {
                "showPresence": True,
                "filter": [

                ]
            }
        }
        try:
            with open("settings.json", "r") as file:
                self.__data: dict = json.load(file)
        except Exception as e:
            Plogger.log(erro, f"Could not load settings because of '{e}' using defaults")
            if isinstance(e, FileNotFoundError):
                with open("settings.json", "w") as file:
                    json.dump(self.default, file)
                Plogger.log(info, "File dose not exist creating settings.json with default settings")
            self.__data: dict = {}

        self.__data = self.fill_default_settings(self.__data, self.default)

    def set_settings(self, _dict):
        self.__data = self.fill_default_settings(_dict, self.default)
        with open("settings.json", "w") as file:
            json.dump(self.__data, file)
        Plogger.log(info, f"Forcing restart on all modules wait please")
        initialize(True)

    def fill_missing_info(self, input_dict, default_dict):
        for key, value in default_dict.items():
            if isinstance(value, dict):
                input_dict[key] = self.fill_missing_info(input_dict.get(key, {}), value)
            else:
                input_dict[key] = input_dict.get(key, value)
        return input_dict

    def fill_default_settings(self, input_dict, default_dict):
        filled_dict = default_dict.copy()
        filled_dict = self.fill_missing_info(input_dict, filled_dict)
        return filled_dict

    @property
    def OnStart(self):
        return self.__data["OnStart"]

    @property
    def DownloadProcessor(self):
        return self.__data["DownloadProcessor"]

    @property
    def MangadexConnection(self):
        return self.__data["MangadexConnection"]

    @property
    def AlertSystem(self):
        return self.__data["AlertSystem"]

    @property
    def DiscordIntegration(self):
        return self.__data["DiscordIntegration"]

    @property
    def Global(self):
        return self.__data


settings = Settings()

if settings.Global.get("LogLeveL"):
    Plogger.log_level = int(settings.Global.get("LogLeveL"))

using_redis = False

if settings.Global.get("redisCaching"):
    import redis

    try:
        import redis

        cache: redis.Redis = redis.Redis()
        cache.setex("testvalue", 10, 1)
        assert int(cache.get("testvalue")) == 1
        using_redis = True
        Plogger.log(info, "Using redis for caching")
    except Exception as e:
        Plogger.log(erro, f"Unable to initialize redis {e}")
if not using_redis:
    Plogger.log(info, "Using self implemented caching instead of redis (might use more ram)")


    class Cache:
        def __init__(self):
            self.cache: dict = {}
            self.__stop_event = threading.Event()
            t = threading.Thread(target=self.__check_expiration)
            t.start()
            self.__thread_finished = False

        def __check_expiration(self):
            while not self.__stop_event.is_set():
                for key, value in self.cache.items():
                    if time.time() >= value["expiration"]:
                        del self.cache[key]
                time.sleep(0.1)
            self.__thread_finished = True

        def close(self):
            self.__stop_event.set()
            while not self.__thread_finished:
                pass
            del self.cache

        def set(self, key, value):
            self.cache[key] = {"value": value, "expiration": None}

        def setex(self, key, _time, value):
            self.cache[key] = {"value": value, "expiration": time.time() + _time}

        def get(self, key):
            try:
                return self.cache[key]["value"]
            except KeyError:
                return None

        def exists(self, key):
            return key in self.cache


    cache = Cache()


def try_to_get(_json, path):
    try:
        for element in path:
            _json = _json[element]
        return _json
    except (KeyError, TypeError):
        return None


def is_uuid(value):
    try:
        UUID(str(value))
        return True
    except ValueError:
        return False


def try_and_conv(value):
    try:
        return float(value)
    except (ValueError, TypeError):
        return -1


class Database:
    def __init__(self):
        self.conn = sqlite3.connect("manga.db", check_same_thread=False, )
        cursor = self.conn.cursor()
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS "mangas" (identifier TEXT NOT NULL,page INTEGER NOT NULL,data BLOB,
            info_id INTEGER,FOREIGN KEY (info_id) REFERENCES info(rowid));""")
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS "records" ("identifier" TEXT NOT NULL UNIQUE, "pages" INTEGER NOT NULL)""")
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS"info" ("identifier"	TEXT NOT NULL,"name"	TEXT NOT NULL,"description"	TEXT,"cover" 
            BLOB,"small_cover"	BLOB,"manga_format"	TEXT,"manga_genre"	TEXT,"content_rating"	TEXT);""")
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS "updates" ("identifier"	TEXT NOT NULL,"old_chapter"	REAL NOT NULL,
            "new_chapter"	REAL NOT NULL,"timestamp"	REAL NOT NULL);""")
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS "chapter_info" ("identifier"	TEXT NOT NULL, "json_data"	BLOB NOT NULL);""")
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS "users" ("name"	TEXT NOT NULL UNIQUE,"password"	BLOB NOT NULL,"data" BLOB);""")
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS "sessions" ("sessionid"	TEXT UNIQUE,"expire"	REAL,"owner"	TEXT)""")

        try:
            # needed for huge performance boost
            # this little code actually speedup /manga/library/data from 11000ms to 3ms pretty neat
            cursor.execute("CREATE INDEX idx_info_id ON mangas(info_id)")
        except Exception as e:
            if "already exists" in e.__str__():
                Plogger.log(info, "mangas(info_id) index already exists")
            else:
                Plogger.log(warn, f"Exception when trying to create index for performance benefit: {e}")

        try:
            # can make downloading faster
            cursor.execute("CREATE INDEX idx_identifier_page ON mangas (identifier, page);")
        except Exception as e:
            if "already exists" in e.__str__():
                Plogger.log(info, "mangas (identifier, page) index already exists")
            else:
                Plogger.log(warn, f"Exception when trying to create index for performance benefit: {e}")

        self.conn.commit()

    def get_session(self, sessionid):
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM sessions WHERE sessionid=:si", {"si": sessionid})
        return cursor.fetchone()

    def set_session(self, sessionid, expire, owner):
        cursor = self.conn.cursor()
        cursor.execute("INSERT INTO sessions VALUES (:si, :exp, :ow)", {"si": sessionid, "exp": expire, "ow": owner})
        self.conn.commit()

    def remove_session(self, sessionid):
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM sessions WHERE sessionid=:si", {"si": sessionid})
        self.conn.commit()

    def get_sessions(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM sessions")
        return cursor.fetchall()

    def add_user(self, name: str, pwd_hash: str) -> bool:
        try:
            cursor = self.conn.cursor()
            cursor.execute("INSERT INTO users VALUES (:name, :pwd, '')", {"name": name, "pwd": pwd_hash})
            self.conn.commit()
            return True
        except Exception as e:
            Plogger.log(erro, f"Encountered exception while adding user: {e}")
            return False

    def get_user_uuid(self, name: str) -> Optional[str]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT name FROM users WHERE name=:nm", {"nm": name})
        if cursor.fetchall() is not None:
            return sha256(name.encode()).hexdigest()
        return None

    def get_user_data(self, name: str) -> dict:
        cursor = self.conn.cursor()
        cursor.execute("SELECT data FROM users  WHERE name=:name", {"name": name})
        fetch = cursor.fetchone()[0]
        if not fetch:
            return {}
        decompressed = decompress(fetch)
        if not decompressed:
            return {}
        else:
            return json.loads(decompressed)

    def set_user_data(self, name, user_data: Union[str, dict]) -> None:
        if isinstance(user_data, dict):
            user_data = json.dumps(user_data)
        compressed_data = compress(user_data.encode())
        cursor = self.conn.cursor()
        cursor.execute("UPDATE users SET data=:udata WHERE name=:name", {"udata": compressed_data, "name": name})
        self.conn.commit()

    def login_user(self, name: str, pwd_hash: str) -> bool:
        cursor = self.conn.cursor()
        cursor.execute("SELECT name FROM users WHERE name=:name AND password=:pwd", {"name": name, "pwd": pwd_hash})
        fetch = cursor.fetchone()
        if fetch is not None:
            return True
        return False

    def add_chapter_info(self, identifier: str, str_json: str | bytes) -> None:
        cursor = self.conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO chapter_info VALUES (:cuuid, :str_json)",
                       {"cuuid": identifier, "str_json": str_json})
        self.conn.commit()

    def get_chapter_info(self, identifier: str) -> Optional[Sequence]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM chapter_info WHERE identifier=:cuuid", {"cuuid": identifier})
        fetch = cursor.fetchone()
        return fetch if fetch is not None else (None, None)

    def add_update(self, identifier: str, chapter_old: int, chapter_new: int, timestamp: float = None) -> None:
        if not timestamp:
            timestamp = time.time()
        cursor = self.conn.cursor()
        cursor.execute("INSERT INTO updates VALUES (:ide, :cold, :cnew, :ts)",
                       {"ide": identifier, "cold": chapter_old, "cnew": chapter_new, "ts": timestamp})
        self.conn.commit()

    def set_record(self, identifier: str, pages: int) -> None:
        try:
            cursor = self.conn.cursor()
            cursor.execute("INSERT INTO records VALUES (:ide, :page)", {"ide": identifier, "page": pages})
            self.conn.commit()
        except sqlite3.IntegrityError:
            Plogger.log(warn, f"{identifier} already in database")

    def get_record(self, identifier: str) -> Optional[int]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT pages FROM records WHERE identifier=:ide", {"ide": identifier})
        fetch = cursor.fetchone()
        return None if fetch is None else fetch[0]

    def manga_in_db(self):
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT i.identifier, i.name, i.description FROM info i WHERE i.ROWID IN (SELECT info_id FROM mangas);")
        fetch = cursor.fetchall()
        return fetch

    def all_manga(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM info")
        return cursor.fetchall()

    def set_info(self, identifier: str, name: str, description: str, cover: bytes, small_cover: bytes,
                 manga_format: str, manga_genre: str, content_rating: str) -> None:
        cursor = self.conn.cursor()
        cursor.execute("SELECT identifier FROM info WHERE identifier=:ide", {"ide": identifier})
        fetch = cursor.fetchone()
        if fetch:
            return
        cursor.execute("INSERT INTO info VALUES (:ide, :name, :desc, :cover, :sc, :mf, :mg, :cr)",
                       {"ide": identifier, "name": name, "desc": description, "cover": cover, "sc": small_cover,
                        "mf": manga_format, "mg": manga_genre, "cr": content_rating})
        self.conn.commit()

    def get_info(self, identifier: str):
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM info WHERE identifier=:ide", {"ide": identifier})
        fetch = cursor.fetchone()
        return fetch if fetch is not None else None

    def get_title(self, identifier: str):
        cursor = self.conn.cursor()
        cursor.execute("SELECT name FROM info WHERE identifier=:ide", {"ide": identifier})
        fetch = cursor.fetchone()
        return fetch if fetch is not None else None

    def change_cover(self, identifier, cover):
        cursor = self.conn.cursor()
        cursor.execute("UPDATE info SET cover=:cover WHERE identifier=:ide", {"ide": identifier, "cover": cover})
        self.conn.commit()

    def change_small_cover(self, identifier, cover):
        cursor = self.conn.cursor()
        cursor.execute("UPDATE info SET small_cover=:cover WHERE identifier=:ide", {"ide": identifier, "cover": cover})
        self.conn.commit()

    def get_rowid(self, muuid):
        cursor = self.conn.cursor()
        cursor.execute("SELECT rowid FROM info WHERE identifier =:ide", {"ide": muuid})
        rowid = cursor.fetchone()
        return rowid[0] if rowid is not None else None

    def from_rowid(self, rowid):
        cursor = self.conn.cursor()
        cursor.execute("SELECT identifier FROM info WHERE ROWID=:rowid", {"rowid": rowid})
        fetch = cursor.fetchone()
        return fetch[0] if fetch is not None else None

    def save_page(self, identifier: str, page: int, muuid: str, data: bytes) -> None:
        cursor = self.conn.cursor()
        cursor.execute("SELECT identifier FROM mangas WHERE identifier=:ide AND page=:page",
                       {"ide": identifier, "page": page})
        fetch = cursor.fetchone()
        if not fetch:
            rowid = self.get_rowid(muuid)
            cursor.execute("INSERT INTO mangas VALUES (:ide, :page, :data, :info_id)",
                           {"ide": identifier, "page": page, "data": data, "info_id": rowid})
            self.conn.commit()

    def get_page(self, identifier: str, page: int) -> Optional[bytes]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT data FROM mangas WHERE identifier=:ide AND page=:page",
                       {"ide": identifier, "page": page})
        fetch = cursor.fetchone()
        return None if fetch is None else fetch[0]

    def get_chapter_images(self, identifier: str) -> Optional[MutableSequence[Set[int, bytes]]]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT page, data FROM mangas WHERE identifier=:ide", {"ide": identifier})
        fetch = cursor.fetchall()
        return fetch

    def contains_chapter(self, identifier: str) -> bool:
        cursor = self.conn.cursor()
        cursor.execute("SELECT identifier FROM mangas WHERE identifier=:ide", {"ide": identifier})
        fetch = cursor.fetchone()
        return False if fetch is None else True

    def get_chapter_pages(self, identifier: str) -> Optional[Sequence]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT page FROM mangas WHERE identifier=:ide ORDER BY page DESC", {"ide": identifier})
        fetch = cursor.fetchall()
        return [] if fetch is None else [page[0] for page in fetch]

    def get_chapters(self, chapters_ids: Sequence[dict], manga_row_id: int = None, muuid: str = None) -> Tuple[
        dict, Optional[str]]:
        downloaded_chapters = {}
        cursor = self.conn.cursor()
        cursor.execute("SELECT identifier FROM mangas")
        db_chapters = [i[0] for i in cursor.fetchall()]
        try:
            for chapter in chapters_ids:
                _id = chapter["id"]

                if _id not in db_chapters:
                    continue

                pages = self.get_record(_id)

                # record does not exist so it is not in database
                if not pages:
                    continue

                # assert pages == self.get_chapter_pages(
                #    _id), f"Not all pages have been downloaded {pages} {self.get_chapter_pages(_id)}"

                downloaded_chapters[_id] = {}

                downloaded_chapters[_id]["pages"] = pages
                downloaded_chapters[_id]["volume"] = try_to_get(chapter, ["attributes", "volume"])
                downloaded_chapters[_id]["chapter"] = try_to_get(chapter, ["attributes", "chapter"])
        except TypeError as e:
            if not muuid and not manga_row_id:
                raise Exception("Cannot figure out manga uuid you must set manga_row_id or muuid")
            elif muuid:
                identifier, data = database.get_chapter_info(muuid)
                data = decompress(data).decode()
            elif manga_row_id:
                identifier, data = database.get_chapter_info(self.from_rowid(manga_row_id))

                data = decompress(data).decode()
            else:
                raise Exception("??? how")
            if not data:
                if not manga_row_id and muuid:
                    self.get_rowid(muuid)
                return {uuid: {
                    "pages": self.get_record(uuid),
                    "volume": -1,
                    "chapter": idx + 1
                } for idx, uuid in enumerate(self.get_chapter_uuids_from_rowid(manga_row_id))}, "NO_CONNECTION"
            for chapter in json.loads(data):
                _id = chapter["id"]
                if not self.contains_chapter(_id):
                    continue

                pages = self.get_record(_id)

                # assert pages == self.get_chapter_pages(
                #    _id), f"Not all pages have been downloaded {pages} {self.get_chapter_pages(_id)}"

                downloaded_chapters[_id] = {}

                downloaded_chapters[_id]["pages"] = pages
                downloaded_chapters[_id]["volume"] = try_to_get(chapter, ["attributes", "volume"])
                downloaded_chapters[_id]["chapter"] = try_to_get(chapter, ["attributes", "chapter"])

            return downloaded_chapters, "NO_CONNECTION"
        return downloaded_chapters, None

    def get_cover(self, identifier, small=False):
        cursor = self.conn.cursor()
        if small:
            cursor.execute("SELECT small_cover FROM info WHERE identifier=:ide", {"ide": identifier})
        else:
            cursor.execute("SELECT cover FROM info WHERE identifier=:ide", {"ide": identifier})
        fetch = cursor.fetchone()
        return None if fetch is None else fetch[0]

    def get_updates(self, timestamp_lim=0):
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM updates WHERE timestamp > :tslim ORDER BY timestamp DESC",
                       {"tslim": timestamp_lim})
        fetch = cursor.fetchall()
        if fetch:
            fetch = [list(i) for i in fetch]
        return None if fetch is None else [[*update, self.get_title(update[0])[0]] for update in fetch]

    def get_chapter_uuids_from_rowid(self, rowid: int):
        cursor = self.conn.cursor()
        cursor.execute("SELECT DISTINCT identifier FROM mangas WHERE info_id = :rowid", {"rowid": rowid})
        fetch = cursor.fetchall()
        return [] if not fetch else [i[0] for i in fetch]

    # removes manga from a database, takes some time
    # TODO: Add frontend button for removing the manga
    def remove_manga(self, muuid):
        chapter_list = connection.get_chapter_list(muuid)
        cursor = self.conn.cursor()
        for chapter in chapter_list:
            cuuid = chapter["id"]
            pages_in_db = database.get_chapter_pages(cuuid)

            if not pages_in_db:
                continue

            cursor.execute("DELETE FROM mangas WHERE identifier=:cuid", {"cuid": cuuid})
            cursor.execute("DELETE FROM records WHERE identifier=:cuid", {"cuid": cuuid})
            cursor.execute("DELETE FROM chapter_info WHERE identifier=:cuid", {"cuid": cuuid})
        cursor.execute("DELETE FROM info WHERE identifier=:muid", {"muid": muuid})
        self.conn.commit()


class MangadexConnection:
    def __init__(self):
        self.session = requests.Session()
        self.API = "https://api.mangadex.org"
        self.ROUTE = "https://mangadex.org"

        self.exclude_groups = settings.MangadexConnection.get("excludedGroups")
        self.cached_chapter_info = settings.MangadexConnection.get("cacheChapterInfoToDatabase")

    def search_manga(self, name: str, limit: int = 1) -> Optional[MutableSequence[dict]]:
        if cache.exists(f"search:{name}"):
            return json.loads(cache.get(f"search:{name}"))
        try:
            params = {"title": name, "limit": limit}
            if settings.MangadexConnection.get("contentRating"):
                params["contentRating[]"] = settings.MangadexConnection.get("contentRating")

            req = self.session.get(url=f"{self.API}/manga", params=params)
        except requests.exceptions.ConnectionError:
            return None
        if req.status_code != 200:
            return None
        query = req.json()
        if query["result"] != "ok":
            return None
        return_array = []
        for i in query["data"]:
            # get only important data for us
            return_array.append(
                {"id": i["id"], "title": try_to_get(i, ["attributes", "title", "en"]),
                 "description": try_to_get(i, ["attributes", "description", "en"])})
        if not cache.exists(f"search:{name}"):
            cache.setex(f"redis:{name}", 3600, json.dumps(return_array))
        return return_array

    def coverart_update(self, identifier):
        _, _, _, old_image, _ = database.get_info(identifier)

        manga_info_nonagr = self.session.get(
            f"{self.API}/manga/{identifier}?includes%5B%5D=cover_art").json()
        if cache.exists(f"{identifier}:coverurl"):
            pass
        else:
            for index, relationship in enumerate(manga_info_nonagr["data"]["relationships"]):
                if relationship["type"] == "cover_art":
                    coverurl = f"https://mangadex.org/covers/{identifier}/" + try_to_get(manga_info_nonagr,
                                                                                         ["data", "relationships",
                                                                                          index, "attributes",
                                                                                          "fileName"])
                    cache.setex(f"{identifier}:coverurl", 3600, coverurl)
        cover_url = str(cache.get(f"{identifier}:coverurl"))

        if cover_url is None:
            Plogger.log(warn, "coverurl is none. Parsing failed or manga dose not have any cover")
        image_data = requests.get(cover_url).content
        image = Image.open(io.BytesIO(image_data))
        width = 51
        height = 80
        small_cover = image.resize((width, height))
        with io.BytesIO() as image_webp:
            image.save(image_webp, 'WEBP')
            image_webp_bytes = image_webp.getvalue()
        with io.BytesIO() as small_cover_webp:
            small_cover.save(small_cover_webp, 'WEBP')
            small_cover_webp_bytes = small_cover_webp.getvalue()
        database.change_cover(identifier, image_webp_bytes)
        database.change_small_cover(identifier, small_cover_webp_bytes)

    def set_manga_info(self, identifier: str) -> None:
        if not database.get_info(identifier):
            try:
                params = {}
                if settings.MangadexConnection.get("contentRating"):
                    params["contentRating[]"] = settings.MangadexConnection.get("contentRating")

                manga_info_nonagr = self.session.get(
                    f"{self.API}/manga/{identifier}?includes%5B%5D=cover_art", params=params).json()
            except AttributeError:
                return None
            manga_format = ""
            manga_genre = ""
            content_rating = try_to_get(manga_info_nonagr, ["data", "attributes", "contentRating"])
            for index, relationship in enumerate(manga_info_nonagr["data"]["relationships"]):
                if relationship["type"] == "cover_art":
                    coverurl = f"https://mangadex.org/covers/{identifier}/" + try_to_get(manga_info_nonagr,
                                                                                         ["data", "relationships",
                                                                                          index, "attributes",
                                                                                          "fileName"])
                    cache.setex(f"{identifier}:coverurl", 3600, coverurl)

            for tag in manga_info_nonagr["data"]["attributes"]["tags"]:
                if tag["attributes"]["group"] == "format":
                    manga_format = try_to_get(tag, ["attributes", "name", "en"]) + "|"
                if tag["attributes"]["group"] == "genre":
                    manga_genre += try_to_get(tag, ["attributes", "name", "en"]) + "|"

            if manga_format.endswith("|"):
                manga_format = manga_format[:-1]
            if manga_genre.endswith("|"):
                manga_genre = manga_genre[:-1]

            if cache.get(f"{identifier}:coverurl") is None:
                Plogger.log(warn, "coverurl is none. Parsing failed or manga dose not have any cover")
            image_data = requests.get(str(cache.get(f"{identifier}:coverurl"))).content
            image = Image.open(io.BytesIO(image_data))
            width = 51
            height = 80
            small_cover = image.resize((width, height))
            with io.BytesIO() as image_webp:
                image.save(image_webp, 'WEBP')
                image_webp_bytes = image_webp.getvalue()
            with io.BytesIO() as small_cover_webp:
                small_cover.save(small_cover_webp, 'WEBP')
                small_cover_webp_bytes = small_cover_webp.getvalue()

            if try_to_get(manga_info_nonagr, ["data", "attributes", "title", "en"]) is None:
                title = list(try_to_get(manga_info_nonagr, ["data", "attributes", "title"]).values())[0]
            else:
                title = try_to_get(manga_info_nonagr, ["data", "attributes", "title", "en"])
            database.set_info(identifier=identifier,
                              name=title,
                              description=try_to_get(manga_info_nonagr, ["data", "attributes", "description", "en"]),
                              cover=image_webp_bytes,
                              small_cover=small_cover_webp_bytes,
                              manga_format=manga_format,
                              manga_genre=manga_genre,
                              content_rating=content_rating
                              )

    def get_last_chapter_num(self, identifier: str) -> dict:
        chapters_dict = {}
        if identifier is None:
            return {}
        try:
            if cache.exists(f"{identifier}:current_chapter"):
                params = {}
                if settings.MangadexConnection.get("contentRating"):
                    params["contentRating[]"] = settings.MangadexConnection.get("contentRating")

                manga_info_nonagr = self.session.get(
                    f"{self.API}/manga/{identifier}?includes%5B%5D=cover_art", params=params).json()
                cache.setex(f"{identifier}:title", 3600,
                            str(try_to_get(manga_info_nonagr, ["data", "attributes", "title"])))
                manga_format = None
                for index, relationship in enumerate(manga_info_nonagr["data"]["relationships"]):
                    if relationship["type"] == "cover_art":
                        coverurl = f"https://mangadex.org/covers/{identifier}/" + try_to_get(manga_info_nonagr,
                                                                                             ["data", "relationships",
                                                                                              index, "attributes",
                                                                                              "fileName"])
                        cache.setex(f"{identifier}:coverurl", 3600, str(coverurl))

                manga_format = ""
                manga_genre = ""
                content_rating = try_to_get(manga_info_nonagr, ["data", "attributes", "contentRating"])
                for index, relationship in enumerate(manga_info_nonagr["data"]["relationships"]):
                    if relationship["type"] == "cover_art":
                        coverurl = f"https://mangadex.org/covers/{identifier}/" + try_to_get(manga_info_nonagr,
                                                                                             ["data", "relationships",
                                                                                              index, "attributes",
                                                                                              "fileName"])
                        cache.setex(f"{identifier}:coverurl", 3600, coverurl)

                for tag in manga_info_nonagr["data"]["attributes"]["tags"]:
                    if tag["attributes"]["group"] == "format":
                        manga_format = try_to_get(tag, ["attributes", "name", "en"]) + "|"
                    if tag["attributes"]["group"] == "genre":
                        manga_genre += try_to_get(tag, ["attributes", "name", "en"]) + "|"

                if manga_format.endswith("|"):
                    manga_format = manga_format[:-1]
                if manga_genre.endswith("|"):
                    manga_genre = manga_genre[:-1]

                if not database.get_info(identifier):
                    # download cover and put into db cover url in cache
                    if cache.get(f"{identifier}:coverurl") is None:
                        Plogger.log(warn, "coverurl is none. Parsing failed or manga dose not have any cover")
                    image_data = requests.get(str(cache.get(f"{identifier}:coverurl"))).content
                    image = Image.open(io.BytesIO(image_data))
                    width = 51
                    height = 80
                    small_cover = image.resize((width, height))
                    with io.BytesIO() as image_webp:
                        image.save(image_webp, 'WEBP')
                        image_webp_bytes = image_webp.getvalue()
                    with io.BytesIO() as small_cover_webp:
                        small_cover.save(small_cover_webp, 'WEBP')
                        small_cover_webp_bytes = small_cover_webp.getvalue()
                    database.set_info(identifier=identifier,
                                      name=try_to_get(manga_info_nonagr, ["data", "attributes", "title", "en"]),
                                      description=try_to_get(manga_info_nonagr,
                                                             ["data", "attributes", "description", "en"]),
                                      cover=image_webp_bytes,
                                      small_cover=small_cover_webp_bytes,
                                      manga_format=manga_format,
                                      manga_genre=manga_genre,
                                      content_rating=content_rating
                                      )
        except Exception as e:
            Plogger.log(warn, "ERR ?" + str(e))
        if cache.exists(f"{identifier}:current_chapter"):
            chapters_dict[identifier] = float(cache.get(f"{identifier}:current_chapter"))
        else:
            try:
                manga_info = self.session.get(f"{self.API}/manga/{identifier}/aggregate").json()
                highest_chapter = -1
                for volume in manga_info["volumes"].keys():
                    for chapter in manga_info["volumes"][volume]["chapters"].keys():
                        try:
                            if float(chapter) > highest_chapter:
                                highest_chapter = float(chapter)
                        except ValueError:
                            pass
                chapters_dict[identifier] = highest_chapter
                cache.setex(f"{identifier}:current_chapter", 600, highest_chapter)
            except Exception as e:
                Plogger.log(warn, str(e))
        return chapters_dict

    def get_chapter_list(self, identifier: str, lang: str = "en", cache_only: bool = False, remove_duplicates=True) -> \
            Optional[List[dict]]:
        if cache.exists(f"{identifier}:chapter_list"):
            return json.loads(cache.get(f"{identifier}:chapter_list"))
        if cache_only:
            return None
        chapters = []
        offset = 0
        total_chapters = None

        while True:
            if total_chapters and total_chapters <= offset:
                break
            params = {"manga": identifier, "limit": 100, "offset": offset,
                      "translatedLanguage[]": lang,
                      "excludedGroups[]": self.exclude_groups}
            if settings.MangadexConnection.get("contentRating"):
                params["contentRating[]"] = settings.MangadexConnection.get("contentRating")

            chapter = self.session.get(f"{self.API}/chapter",
                                       params=params).json()
            if not total_chapters:
                total_chapters = float(chapter.get("total"))
            chapters.extend(chapter["data"])
            offset += 100
        sorted_chapters = sorted(chapters, key=lambda x: float(x["attributes"]["chapter"]))
        cache.setex(f"{identifier}:chapter_list", 600, json.dumps(sorted_chapters))

        if self.cached_chapter_info:
            data = compress(json.dumps(sorted_chapters).encode())
            database.add_chapter_info(identifier, data)

        return remove_duplicate_chapters(sorted_chapters)

    def get_chapter_pages(self, identifier: str, db_pages: Sequence, silent=False, generate: Callable = None,
                          page_download_cb: Callable = None, muuid: str = None, rate_limit_callback: Callable = None) -> \
            Sequence[Tuple[int, bytes]]:
        metadata = requests.get(f"https://api.mangadex.org/at-home/server/{identifier}")
        remaining = metadata.headers.get("X-RateLimit-Remaining")
        retry_after = metadata.headers.get("X-RateLimit-Retry-After")
        if int(remaining) <= 0:
            Plogger.log(warn, "Reached Rate Limit while downloading")
            if rate_limit_callback(float(retry_after)):
                return []
        metadata = metadata.json()
        _hash = metadata["chapter"]["hash"]
        baseUrl = metadata['baseUrl']
        pages = len(metadata["chapter"]["data"])
        database.set_record(identifier, pages)
        if not silent:
            start = time.perf_counter()
        downloaded_pages = []
        for page_count, page_digest in enumerate(metadata["chapter"]["data"]):
            if generate is not None:
                if not generate():
                    break
            if page_count + 1 in db_pages:
                continue
            page = requests.get(f"{baseUrl}/data/{_hash}/{page_digest}")
            Plogger.log(info, f"Getting {page_digest}")
            downloaded_pages.append((page_count + 1, page.content))
            if page_download_cb:
                page_download_cb(muuid, page_count + 1, pages)
            time.sleep(timeouts[DlProcessor.mode + "_PAGE_TIMEOUT"])
        if not silent:
            Plogger.log(info, f"Finished downloading {identifier}. Took {(time.perf_counter() - start) * 1000}ms")
        return downloaded_pages

    def threaded_get_chapter_page(self, identifier: str, db_pages: Sequence, silent=False,
                                  generate: Callable = None,
                                  page_download_cb: Callable = None, muuid: str = None,
                                  rate_limit_callback: Callable = None) -> Sequence[Tuple[int, bytes]]:
        metadata = requests.get(f"https://api.mangadex.org/at-home/server/{identifier}")
        remaining = metadata.headers.get("X-RateLimit-Remaining")
        retry_after = metadata.headers.get("X-RateLimit-Retry-After")
        if int(remaining) <= 0:
            Plogger.log(warn, "Reached Rate Limit while downloading")
            if rate_limit_callback(float(retry_after)):
                return []
            else:
                metadata = requests.get(f"https://api.mangadex.org/at-home/server/{identifier}")
        metadata = metadata.json()
        _hash = metadata["chapter"]["hash"]
        baseUrl = metadata['baseUrl']
        pages = len(metadata["chapter"]["data"])
        database.set_record(identifier, pages)

        def download_page(page_count, page_digest):
            page = requests.get(f"{baseUrl}/data/{_hash}/{page_digest}")
            Plogger.log(info, f"Getting {page_digest}")
            return page_count + 1, page.content

        downloaded_pages = []
        # this is for mangas that have 1-4 pages per chapter because im not sure if ThreadPoolExecutor handles that
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(pages, (os.cpu_count() or 1) + 4)) as executor:
            futures = [executor.submit(download_page, page_count, page_digest)
                       for page_count, page_digest in enumerate(metadata["chapter"]["data"])
                       if page_count + 1 not in db_pages and (generate() if generate else True)]
            for future in concurrent.futures.as_completed(futures):
                try:
                    downloaded_pages.append(future.result())
                    if page_download_cb:
                        page_download_cb(muuid, downloaded_pages[-1][0], pages)
                except Exception as e:
                    Plogger.log(erro, f"An error occurred: {e}")

        if not silent:
            Plogger.log(info, f"Finished downloading {identifier}. Took {time.perf_counter() * 1000}ms")
        # so the data in database is stored sequentially
        return sorted(downloaded_pages, key=lambda x: x[0])

    def get_manga_uuid_from_chapter(self, chapter_uuid):
        muuid = chapter_to_manga_from_cache(chapter_uuid)
        if muuid:
            return muuid
        req = self.session.get(self.API + f"/chapter/{chapter_uuid}")
        if req.status_code != 200 or not req:
            return None
        chapter_info = req.json()
        if chapter_info["result"] != "ok":
            return None
        for relationship in chapter_info["data"]["relationships"]:
            if relationship["type"] == "manga":
                return relationship["id"]
        return None

    def get_next_and_prev(self, cuuid) -> dict:
        muuid = self.get_manga_uuid_from_chapter(cuuid)
        next_prev = {"next": None, "prev": None, "current": {}}
        chapter_list = self.get_chapter_list(muuid)
        if chapter_list is None:
            return next_prev
        chapter_list = sorted(chapter_list,
                              key=lambda x: (
                                  try_and_conv(x["attributes"]['volume']), try_and_conv(x["attributes"]['chapter'])))
        for chapter in chapter_list:
            if chapter["id"] == cuuid:
                chapter_list_index = chapter_list.index(chapter)
                next_prev["current"]["chapter"] = try_to_get(chapter_list[chapter_list_index],
                                                             ["attributes", "chapter"])
                next_prev["current"]["volume"] = try_to_get(chapter_list[chapter_list_index], ["attributes", "volume"])
                if chapter_list_index == 0 and chapter_list_index + 1 <= len(chapter_list) - 1:
                    next_prev["next"] = chapter_list[chapter_list_index + 1]
                if chapter_list_index >= 1 and chapter_list_index + 1 <= len(chapter_list) - 1:
                    next_prev["prev"] = chapter_list[chapter_list_index - 1]
                    next_prev["next"] = chapter_list[chapter_list_index + 1]
        if not database.contains_chapter(try_to_get(next_prev, ["next", "id"])):
            next_prev["next"] = None
        if not database.contains_chapter(try_to_get(next_prev, ["prev", "id"])):
            next_prev["prev"] = None
        return next_prev


# helper function
def find_by_id_in_list(lst, _id):
    for idx, _dict in enumerate(lst):
        if _dict["id"] == _id:
            return idx, _dict
    return None, None


timeouts = {
    "NO_LIMIT_CHAPTER_FINISH": 0,
    "FAST_CHAPTER_FINISH": 1.5,
    "NORMAL_CHAPTER_FINISH": 2.5,
    "SLOW_CHAPTER_FINISH": 5,

    "NO_LIMIT_ON_RATE_LIMIT": 0,
    "FAST_ON_RATE_LIMIT": 30,
    "NORMAL_ON_ERROR": 60,
    "SLOW_ON_ERROR": 120,
}


class MangaDownloadJob:
    def __init__(self, _id: str, chapter_start: Optional[float] = None, chapter_end: Optional[float] = None):
        self.id = _id
        self.chapter_start = chapter_start
        self.chapter_end = chapter_end
        self.downloaded = False

        self.__chapter_info = connection.get_chapter_list(self.id)
        # not a mdx api call so it should not fail
        self.name = database.get_info(self.id)[1]

    @property
    def chapter_list(self):
        if self.__chapter_info is None:
            self.__chapter_info = connection.get_chapter_list(self.id)
        return self.__chapter_info

    def __eq__(self, other):
        if isinstance(other, MangaDownloadJob):
            return self.id == other.id
        return False

    # to currently working on (json struct with info about state of download)
    def to_cwo(self):
        return {
            "id": self.id,
            "name": self.name,
            "chapter_status": [0, len(self.chapter_list)],
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

    def add_job(self, job: MangaDownloadJob) -> bool:
        if job not in self.__queue:
            self.__queue.append(job)
            return True
        return False

    def remove_job(self, _id: str):
        for index, job in enumerate(self.__queue):
            if job.downloaded or job.id == _id:
                self.__queue.pop(index)

    def in_queue(self, _id: str):
        return _id in self.__queue

    def push_to_top(self, index):
        self.__queue.insert(0, self.__queue.pop(index))

    def __iter__(self):
        return self.__queue.__iter__()

    def __len__(self):
        return self.__queue.__len__()


class MangaDownloader:
    def __init__(self):
        self.mode = settings.DownloadProcessor.get("defaultSpeedMode")
        self.silent_download = settings.DownloadProcessor.get("silentDownload")
        self.save_queue = settings.DownloadProcessor.get("saveQueueOnExit")
        self.use_threading = settings.DownloadProcessor.get("useThreading")
        self.halt_on_ratelimit = settings.DownloadProcessor.get("haltOnRateLimitReach")

        self.queue = MangaQueue()
        self.currently_working_on = None
        self.stop_event = threading.Event()

        if settings.DownloadProcessor.get("runOnStart"):
            self.start()

    def start(self):
        self.stop_event.clear()
        t = threading.Thread(target=self.__download, args=())
        t.start()

    def stop(self):
        if self.save_queue:
            if self.currently_working_on:
                self.queue.add_job(MangaDownloadJob(_id=self.currently_working_on["id"]))
            with open(f"{os.getcwd()}\data\queue.json", "w") as file:
                json.dump([i.to_cwo() for i in self.queue], file)
        self.stop_event.set()

    def is_running(self) -> bool:
        return not self.stop_event.is_set()

    def page_download_callback(self, identifier, at_page, page_total):
        if self.currently_working_on["page_status"] == ["?", "?"]:
            self.currently_working_on["page_status"] = [0, page_total]
        self.currently_working_on["page_status"][0] += 1

    def add_chapter(self):
        self.currently_working_on["chapter_status"][0] += 1
        self.currently_working_on["page_status"] = ["?", "?"]

    def change_mode(self, mode):
        self.mode = mode

    def rate_limit_callback(self, retry_after) -> bool:
        if self.halt_on_ratelimit:
            Plogger.log(warn, "Halting execution of downloader")
            # pause execution until we can try again
            time.sleep((retry_after + 30) - time.time())
            return False
        else:
            self.stop()
            return True

    def __download(self):
        exited = False
        while not self.stop_event.is_set():
            while not self.queue.first:
                if self.stop_event.is_set():
                    exited = True
                    break
                time.sleep(0.01)
            if exited:
                continue
            job = self.queue.first
            reached_end = False
            self.currently_working_on = job.to_cwo()

            if self.use_threading:
                download_func = connection.threaded_get_chapter_page
            else:
                download_func = connection.get_chapter_pages

            for chapter in job.chapter_list:
                if self.stop_event.is_set():
                    break
                if job.chapter_start:
                    last_manga_chapter = try_to_get(job.chapter_list[-1], ["attributes", "chapter"])
                    if float(last_manga_chapter) < job.chapter_start:
                        Plogger.log(warn,
                                    f"cannot download any manga because no scanlation was found. Start {job.chapter_start}. Found {last_manga_chapter}")
                        break
                    chapter_value = try_to_get(chapter, ["attributes", "chapter"])
                    if chapter_value is not None and float(chapter_value) < job.chapter_start:
                        self.add_chapter()
                        continue
                if job.chapter_end:
                    if float(try_to_get(chapter, ["attributes", "chapter"])) >= job.chapter_end:
                        reached_end = True
                        break
                if reached_end:
                    break
                cuuid = chapter["id"]
                pages_in_db = database.get_chapter_pages(cuuid)

                if len(pages_in_db) == database.get_record(cuuid):
                    self.add_chapter()
                    Plogger.log(info,
                                f"already in database {chapter['attributes']['volume']} Volume {chapter['attributes']['chapter']} Chapter")
                    continue

                for page_value, page in download_func(cuuid, pages_in_db,
                                                      silent=self.silent_download,
                                                      generate=self.is_running,
                                                      page_download_cb=self.page_download_callback,
                                                      muuid=job.id,
                                                      rate_limit_callback=self.rate_limit_callback):
                    if page_value is None or page is None:
                        continue
                    database.save_page(cuuid, page_value, job.id, page)
                self.add_chapter()
                Plogger.log(info,
                            f"saved to database {chapter['attributes']['volume']} Volume {chapter['attributes']['chapter']} Chapter")
                time.sleep(timeouts[self.mode + "_CHAPTER_FINISH"])
            if not self.stop_event.is_set():
                job.downloaded = True
                self.queue.remove_job(job.id)
                self.currently_working_on = None


FIRST_ITER = -1


class AlertSystem:
    def __init__(self):
        self.webhook = settings.AlertSystem.get("discordWebhook")
        self.recipient = settings.AlertSystem.get("discordRecipient")
        self.start_on_release = settings.AlertSystem.get("downloadStartOnRelease")
        self.watched_mangas = settings.AlertSystem.get("watchedMangas")
        self.cooldown = settings.AlertSystem.get("cooldown")
        self.sound_alert = settings.AlertSystem.get("soundAlertOnRelease")
        if self.sound_alert:
            # TODO: Implement sound alert
            pass

        if not os.path.isdir("data"):
            os.mkdir("data")

        for manga in self.watched_mangas:
            if not is_uuid(manga):
                Plogger.log(erro, f"AlertSystem could not recognise '{manga}' as valid uuid")
                self.watched_mangas.remove(manga)
        if not self.watched_mangas:
            Plogger.log(erro, "watchedMangas is empty or all inputted uuids weren't valid ones. Cannot start "
                              "watchedMangas")
            return
        if self.cooldown < 60:
            Plogger.log(erro,
                        "AlertSystem cooldown is set lower that 60! anything lower that at least 300-600 could get your"
                        "ip restricted. Please keep that in mind")
            return
        if self.cooldown <= 300:
            Plogger.log(warn,
                        "AlertSystem cooldown. anything lower that at least 300-600 could get your ip restricted. "
                        "Please keep that in mind")
        if self.webhook is None:
            Plogger.log(erro, "Discord webhook is None. Cannot start AlertSystem")
            return
        else:
            if self.recipient is not None:
                self.dcRecipient = f"<@{self.recipient}>"
            else:
                self.dcRecipient = None
            Plogger.log(info, "Alert server is starting")
            self.stop_event = threading.Event()
            t = threading.Thread(target=self.loop, args=(self.stop_event,))
            t.start()

    def __del__(self):
        self.stop()

    def stop(self):
        if not self.webhook:
            return
        self.stop_event.set()

    def loop(self, close_event: threading.Event):
        start = 0
        while True:
            if close_event.is_set():
                break
            if time.time() > (start + self.cooldown):
                self.track()
                start = time.time()
            time.sleep(0.5)

    def send_alert(self, data):
        asyncio.run(send_webhook(
            webhook=self.webhook,
            identifier=data.get("identifier"),
            title=data.get("title"),
            cover=data.get("cover_art"),
            old_chapter=data.get("old_chapter"),
            new_chapter=data.get("new_chapter"),
            desc=data.get("description"),
            mention=data.get("recipient")
        ))
        database.add_update(data.get("identifier"), data.get("old_chapter"), data.get("new_chapter"))

    def track(self):
        assert connection is not None, "Connection to mdx can not be established or the connector was terminated"
        assert database is not None, "Database connection must be running"
        if not os.path.isfile(os.getcwd() + "\\data\\watched-manga.json"):
            # create default
            with open(os.getcwd() + "\\data\\watched-manga.json", "w") as file:
                empty_chapters = {x: -1 for x in self.watched_mangas}
                json.dump(empty_chapters, file)
        with open(os.getcwd() + "\\data\\watched-manga.json", "r") as file:
            chapter_before = json.load(file)
        for key in chapter_before.keys():
            if key not in self.watched_mangas:
                del chapter_before[key]
        for manga in self.watched_mangas:
            if manga not in chapter_before:
                chapter_before[manga] = -1

        chapter_now = {}

        change = False

        for manga in self.watched_mangas:
            Plogger.log(info, f"Running on {manga}")
            current_chapter = connection.get_last_chapter_num(manga)
            if not current_chapter:
                Plogger.log(warn, f"Could not get current chapter for {manga}")
                continue
            chapter_now[manga] = current_chapter[manga]
            if chapter_now[manga] > chapter_before[manga] != FIRST_ITER:
                old_chapter = chapter_before[manga]
                new_chapter = chapter_now[manga]

                minfo = database.get_info(manga)

                if minfo is None:
                    connection.set_manga_info(manga)
                    minfo = database.get_info(manga)

                _, name, desc, cover = minfo

                self.send_alert({
                    "identifier": manga,
                    "old_chapter": old_chapter,
                    "new_chapter": new_chapter,
                    "cover_art": cover,
                    "title": name,
                    "description": desc,
                    "recipient": self.dcRecipient
                })

                DlProcessor.push_queue({"id": manga, "chapter_start": chapter_before[manga]})
                change = True

        with open(os.getcwd() + "\\data\\watched-manga.json", "w") as file:
            if chapter_now:
                json.dump(chapter_now, file)

        if self.start_on_release and not DlProcessor.in_progress and change:
            DlProcessor.start()


class SessionManager:
    def __init__(self):
        self.sessions = {}
        self.rand_string = list(string.ascii_letters + string.digits)
        random.seed(time.perf_counter_ns())
        self.remove_old_sessions()
        for sessionid, expire, owner in database.get_sessions():
            self.sessions[sessionid] = {"id": sessionid, "expire": expire, "owner": owner}

    def make_session(self, name):
        session = {
            "id": "".join(random.choices(self.rand_string, k=64)),
            "expire": time.time() + (3600 * 24)
        }
        # should not happen because its random 64 characters but you never know
        if session["id"] in self.sessions:
            return self.make_session(name)
        self.sessions[session["id"]] = session.copy()
        self.sessions[session["id"]]["owner"] = name
        database.set_session(session["id"], session["expire"], name)
        return session

    def remove_old_sessions(self):
        # memory sessions
        for session in self.sessions.values():
            if time.time() > session["expire"]:
                del self.sessions[session["id"]]

        # database sessions
        for sessionid, expire, owner in database.get_sessions():
            if time.time() > expire:
                database.remove_session(sessionid)

    def validate_session(self, session):
        if not session or session is None:
            return False
        if isinstance(session, str):
            session = json.loads(session)
        if session["id"] in self.sessions:
            if time.time() > self.sessions[session["id"]]["expire"]:
                del self.sessions[session["id"]]
                database.remove_session(session["id"])
                return False
            return True
        return False

    def get_session_name(self, session):
        if isinstance(session, str):
            session = json.loads(session)
        if session["id"] in self.sessions and self.sessions[session["id"]]["expire"] > time.time():
            return self.sessions[session["id"]]["owner"]
        return None


class DiscordIntegration:
    def __init__(self):
        self.RPC: Presence
        try:
            if settings.DiscordIntegration.get("showPresence"):
                self.RPC = Presence("1169363237725286540")
                self.RPC.connect()
                self.update("Browsing", "https://github.com/jonasek369/local-mdx-public")
            else:
                self.RPC = None
        except pypresence.exceptions.DiscordNotFound:
            self.RPC = None

        self.filter = settings.DiscordIntegration.get("filter")

    def stop(self):
        if self.RPC:
            self.RPC.close()

    def allowed_by_filtering(self, InOrEx, value, operation, data) -> bool:
        if isinstance(value, str):
            filter = eval(f"'{value}'{operation}{data}")
            if filter and InOrEx == "exclude":
                return False
            if filter and InOrEx == "include":
                return True
        if isinstance(value, list):
            for element in value:
                filter = eval(f"'{element}'.lower() {operation} '{data}'.lower()")
                if filter and InOrEx == "exclude":
                    return False
                if filter and InOrEx == "include":
                    return True

    def update(self, state, details, muuid=None):
        if not self.RPC:
            return
        if muuid:
            is_allowed = True
            for InOrEx, operation, uuids in self.filter:
                if len(operation) > 2 or operation not in ["==", "!=", "in"]:
                    raise Exception("Operation is bigger than 2 characters")
                if operation == "in" and uuids[0] in ["content_rating", "genres"]:
                    manga_info = database.get_info(muuid)
                    match uuids[0]:
                        case "genres":
                            content_rating = manga_info[-2]
                            is_allowed = self.allowed_by_filtering(InOrEx, uuids[1], operation, content_rating)
                            if not is_allowed:
                                break
                        case "content_rating":
                            genres = manga_info[-1]
                            is_allowed = self.allowed_by_filtering(InOrEx, uuids[1], operation, genres)
                            if not is_allowed:
                                break
                        case _:
                            raise Exception(f"Unsupported value '{uuids[0]}'")
                elif operation in ["==", "!="]:
                    is_allowed = self.allowed_by_filtering(InOrEx, muuid, operation, uuids)
                else:
                    raise Exception("invalid filtering")
            if not is_allowed:
                Plogger.log(info, "Change of reading was not allowed by filter")
                return

        self.RPC.update(state=state, details=details, large_image="chair")


database: Database = None
connection: MangadexConnection = None
DlProcessor: MangaDownloader = None
alert_sys: AlertSystem = None
session_manager: SessionManager = None
discord_integration: DiscordIntegration = None


async def send_webhook(webhook, identifier, title, cover, old_chapter, new_chapter, desc, mention: Member = None):
    async with aiohttp.ClientSession() as session:
        webhook = Webhook.from_url(webhook, session=session)
        embed = discord.Embed(title=f"{title}", url=f"https://mangadex.org/title/{identifier}")
        if not cover:
            pass
        else:
            embed.set_thumbnail(url=f"attachment://image.jpg")
        embed.add_field(name="Description", value=desc, inline=False)
        embed.add_field(name="Chapter",
                        value=f"chapter {old_chapter}->{new_chapter}",
                        inline=False)

        if mention is None:
            message = MISSING
        else:
            message = mention

        await webhook.send(content=message, embed=embed, file=File(io.BytesIO(cover), filename="image.jpg"))


def initialize(force=False):
    global database, connection, DlProcessor, alert_sys, session_manager, discord_integration
    if database is None or connection is None or DlProcessor is None or alert_sys is None or session_manager is None or discord_integration is None or force:
        if force and discord_integration:
            discord_integration.stop()
        database = Database()
        connection = MangadexConnection()
        DlProcessor = MangaDownloader()
        alert_sys = AlertSystem()
        session_manager = SessionManager()
        discord_integration = DiscordIntegration()
        create_chapter_to_manga_cache()
        if settings.OnStart.get("cacheMangas"):
            # start on diffrent thread so it dose not block it takes quite a lot of time
            t = threading.Thread(target=preload_mangas)
            t.start()
    return True


def search_manga(name, limit=5):
    if database is None or connection is None or DlProcessor is None:
        initialize()
    return connection.search_manga(name, limit=limit)


def download_manga(manga):
    # raise DeprecationWarning("Use Dlprocessor")
    print("Use DownloadProcessor")
    if isinstance(manga, str):
        manga_id = connection.search_manga(manga)[0]["id"]
    elif isinstance(manga, dict):
        assert "id" in manga.keys(), "Manga has to have the key 'id'"
        manga_id = manga["id"]
    else:
        raise Exception(f"manga is {type(manga)} it was expected to be str or dict")
    chapter_list = connection.get_chapter_list(manga_id)

    for index, chapter in enumerate(chapter_list):
        _id = chapter["id"]
        print(f"downloading {_id}")
        pages_in_db = database.get_chapter_pages(_id)

        # Chapter already in a database. No need to download
        if len(pages_in_db) == database.get_record(_id):
            print(f"{_id} already in database")
            continue

        for page_value, page in connection.get_chapter_pages(_id, pages_in_db, silent=False):
            database.save_page(_id, page_value, manga_id, page)
        print("saved to database")


def create_chapter_to_manga_cache():
    cursor = database.conn.cursor()
    cursor.execute("SELECT * FROM chapter_info")
    fetch = cursor.fetchall()
    for muuid, chapter_info_str in fetch:
        cache.setex(f"{muuid}:chapter_to_manga", 3600,
                    json.dumps([i["id"] for i in json.loads(decompress(chapter_info_str).decode())]))


def chapter_to_manga_from_cache(cuuid):
    if not cache.exists(f"{cuuid}:chapter_to_manga"):
        return None
    uuids = json.loads(cache.get(f"{cuuid}:chapter_to_manga"))
    for muuid in uuids:
        for db_cuuid in uuids[muuid]:
            if db_cuuid == cuuid:
                return muuid


def preload_mangas() -> None:
    mangas = database.manga_in_db()
    for mangauuid, _, _ in mangas:
        chapters_ids = connection.get_chapter_list(mangauuid)
        rowid = database.get_rowid(mangauuid)
        database.get_chapters(chapters_ids, rowid)
        Plogger.log(info, f"Finished caching {mangauuid}")
        time.sleep(5)


def convert_to_datetime(timestamp_str):
    return datetime.strptime(timestamp_str, '%Y-%m-%dT%H:%M:%S+00:00')


def remove_duplicate_chapters(chapters):
    chapter_dict = {}

    for chapter in chapters:
        key = (chapter['attributes']['volume'], chapter['attributes']['chapter'])
        publish_at = convert_to_datetime(chapter['attributes']['publishAt'])
        if key not in chapter_dict or publish_at > convert_to_datetime(chapter_dict[key]['attributes']['publishAt']):
            chapter_dict[key] = chapter

    return sorted(list(chapter_dict.values()),
                  key=lambda x: (try_and_conv(x['attributes']['volume']), try_and_conv(x['attributes']['chapter'])))


if __name__ == "__main__":
    database = Database()
    connection = MangadexConnection()
    DlProcessor = MangaDownloader()

    discord_integration = DiscordIntegration()

    print(connection.get_chapter_list("17a56d33-9443-433a-9e0d-70459893ed8f"))

    # for manga in database.all_manga():
    #    info = database.get_info(manga[0])
    #    params = {}
    #    if settings.MangadexConnection.get("contentRating"):
    #        params["contentRating[]"] = settings.MangadexConnection.get("contentRating")
#
#    manga_info_nonagr = connection.session.get(f"{connection.API}/manga/{info[0]}?includes%5B%5D=cover_art",
#                                               params=params).json()
#    manga_format = ""
#    manga_genre = ""
#    content_rating = try_to_get(manga_info_nonagr, ["data", "attributes", "contentRating"])
#
#    for tag in manga_info_nonagr["data"]["attributes"]["tags"]:
#        if tag["attributes"]["group"] == "format":
#            manga_format = try_to_get(tag, ["attributes", "name", "en"]) + "|"
#        if tag["attributes"]["group"] == "genre":
#            manga_genre += try_to_get(tag, ["attributes", "name", "en"]) + "|"
#
#    if manga_format.endswith("|"):
#        manga_format = manga_format[:-1]
#    if manga_genre.endswith("|"):
#        manga_genre = manga_genre[:-1]
#
#    cursor = database.conn.cursor()
#    cursor.execute("UPDATE info SET manga_format=:mf , manga_genre=:mg, content_rating=:cr WHERE identifier=:ide",
#                   {"ide": info[0], "mf": manga_format, "mg": manga_genre, "cr": content_rating})
#    database.conn.commit()
#    print(info[0])
# print("end")
