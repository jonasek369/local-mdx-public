import asyncio
import base64
import io
import json
import os
import random
import sqlite3
import string
import sys
import threading
import time
from collections.abc import MutableSequence, Set, Generator
from gzip import compress, decompress
from pstats import SortKey
from typing import Callable, Tuple, Optional, Sequence, Union
# ALERT SYSTEM
from uuid import UUID

import aiohttp
import discord
import tqdm
from discord import File
from discord import Webhook, Member
from discord.abc import MISSING

from custom_logger import Logger, info, warn, erro
from pydub import AudioSegment
from pydub.playback import play as PlaySound
from hashlib import sha256
from PIL import Image
import io

# for testing the offline mode
DISABLE_INTERNET_CONNECTION = 0
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


class Settings:
    def __init__(self):
        default: dict = {"LogLevel": 1, "OnStart": {"cacheMangas": True},
                         "DownloadProcessor": {"defaultSpeedMode": "FAST", "settings": {"silent_download": False}},
                         "MangadexConnection": {
                             "excludedGroups": ["4f1de6a2-f0c5-4ac5-bce5-02c7dbb67deb"],
                             "cacheChapterInfoToDatabase": True},
                         "AlertSystem": {"discordWebhook": "", "discordRecipient": -1, "soundAlertOnRelease": False,
                                         "downloadStartOnRelease": True, "watchedMangas": [], "cooldown": 600}}
        try:
            with open("settings.json") as file:
                self.__data: dict = json.load(file)
        except Exception as e:
            self.__data: dict = {}

        self.__data = self.fill_default_settings(self.__data, default)

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
    def Global(self):
        return self.__data


settings = Settings()

if settings.Global.get("LogLeveL"):
    Plogger = Logger(int(settings.Global.get("LogLeveL")))
else:
    Plogger = Logger(1)

COMPILED = False
LOGGING = False


def try_to_get(json, path):
    try:
        for element in path:
            json = json[element]
        return json
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


def get_file_content_chrome(driver, uri):
    result = driver.execute_async_script("""
    var uri = arguments[0];
    var callback = arguments[1];
    var toBase64 = function(buffer){for(var r,n=new Uint8Array(buffer),t=n.length,a=new Uint8Array(4*Math.ceil(t/3)),i=new Uint8Array(64),o=0,c=0;64>c;++c)i[c]="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/".charCodeAt(c);for(c=0;t-t%3>c;c+=3,o+=4)r=n[c]<<16|n[c+1]<<8|n[c+2],a[o]=i[r>>18],a[o+1]=i[r>>12&63],a[o+2]=i[r>>6&63],a[o+3]=i[63&r];return t%3===1?(r=n[t-1],a[o]=i[r>>2],a[o+1]=i[r<<4&63],a[o+2]=61,a[o+3]=61):t%3===2&&(r=(n[t-2]<<8)+n[t-1],a[o]=i[r>>10],a[o+1]=i[r>>4&63],a[o+2]=i[r<<2&63],a[o+3]=61),new TextDecoder("ascii").decode(a)};
    var xhr = new XMLHttpRequest();
    xhr.responseType = 'arraybuffer';
    xhr.onload = function(){ callback(toBase64(xhr.response)) };
    xhr.onerror = function(){ callback(xhr.status) };
    xhr.open('GET', uri);
    xhr.send();
    """, uri)
    if type(result) == int:
        raise Exception("Request failed with status %s" % result)
    return base64.b64decode(result)


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
            """CREATE TABLE IF NOT EXISTS "info" ("identifier" TEXT NOT NULL, "name" TEXT NOT NULL, "description" 
            TEXT, cover BLOB, small_cover BLOB)""")
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS "updates" ("identifier"	TEXT NOT NULL,"old_chapter"	REAL NOT NULL,
            "new_chapter"	REAL NOT NULL,"timestamp"	REAL NOT NULL);""")
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS "chapter_info" ("identifier"	TEXT NOT NULL, "json_data"	BLOB NOT NULL);""")
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS "users" ("name"	TEXT NOT NULL UNIQUE,"password"	BLOB NOT NULL,"data" BLOB);""")
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS "sessions" ("sessionid"	TEXT UNIQUE,"expire"	REAL,"owner"	TEXT)""")
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

    def manga_in_db(self, allow_empty_entry=False):
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT i.identifier, i.name, i.description FROM info i WHERE i.ROWID IN (SELECT info_id FROM mangas);")
        fetch = cursor.fetchall()
        return fetch

    def set_info(self, identifier: str, name: str, description: str, cover: bytes, small_cover: bytes) -> None:
        cursor = self.conn.cursor()
        cursor.execute("SELECT identifier FROM info WHERE identifier=:ide", {"ide": identifier})
        fetch = cursor.fetchone()
        if fetch:
            return
        cursor.execute("INSERT INTO info VALUES (:ide, :name, :desc, :cover, :sc)",
                       {"ide": identifier, "name": name, "desc": description, "cover": cover, "sc": small_cover})
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
                if not _id in db_chapters:
                    continue

                pages = self.get_record(_id)

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


class MangadexConnection:
    def __init__(self):
        self.session = requests.Session()
        self.API = "https://api.mangadex.org"
        self.ROUTE = "https://mangadex.org"

        self.exclude_groups = settings.MangadexConnection.get("excludedGroups")
        self.cached_chapter_info = settings.MangadexConnection.get("cacheChapterInfoToDatabase")

    def search_manga(self, name: str, limit: int = 1) -> Optional[MutableSequence[dict]]:
        if name in cache["search"]:
            return cache["search"][name]["result"]
        try:
            req = self.session.get(url=f"{self.API}/manga", params={"title": name, "limit": limit})
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
        if name not in cache:
            cache["search"][name] = {}
            cache["search"][name]["result"] = return_array
        return return_array

    def coverart_update(self, identifier):
        _, _, _, old_image, _ = database.get_info(identifier)

        manga_info_nonagr = self.session.get(
            f"{self.API}/manga/{identifier}?includes%5B%5D=cover_art").json()
        if identifier not in cache:
            cache[identifier] = {}
        if cache[identifier].get("coverurl"):
            pass
        else:
            for index, relationship in enumerate(manga_info_nonagr["data"]["relationships"]):
                if relationship["type"] == "cover_art":
                    cache[identifier]["coverurl"] = f"https://mangadex.org/covers/{identifier}/" + try_to_get(
                        manga_info_nonagr, ["data", "relationships", index, "attributes", "fileName"])
        cover_url = cache[identifier].get("coverurl")

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
                manga_info_nonagr = self.session.get(
                    f"{self.API}/manga/{identifier}?includes%5B%5D=cover_art").json()
            except AttributeError:
                return None
            if identifier not in cache:
                cache[identifier] = {}
            for index, relationship in enumerate(manga_info_nonagr["data"]["relationships"]):
                if relationship["type"] == "cover_art":
                    cache[identifier]["coverurl"] = f"https://mangadex.org/covers/{identifier}/" + try_to_get(
                        manga_info_nonagr, ["data", "relationships", index, "attributes", "fileName"])

            if cache[identifier]["coverurl"] is None:
                Plogger.log(warn, "coverurl is none. Parsing failed or manga dose not have any cover")
            image_data = requests.get(cache[identifier]["coverurl"]).content
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
                              description=try_to_get(manga_info_nonagr, ["data", "attributes", "description", "en"]),
                              cover=image_webp_bytes,
                              small_cover=small_cover_webp_bytes
                              )

    def get_last_chapter_num(self, identifier: str) -> dict:
        chapters_dict = {}
        if identifier is None:
            return {}
        try:
            if identifier not in cache:
                manga_info_nonagr = self.session.get(
                    f"{self.API}/manga/{identifier}?includes%5B%5D=cover_art").json()
                cache[identifier] = {}
                cache[identifier]["titles"] = try_to_get(manga_info_nonagr, ["data", "attributes", "title"])
                for index, relationship in enumerate(manga_info_nonagr["data"]["relationships"]):
                    if relationship["type"] == "cover_art":
                        cache[identifier]["coverurl"] = f"https://mangadex.org/covers/{identifier}/" + try_to_get(
                            manga_info_nonagr, ["data", "relationships", index, "attributes", "fileName"])
                if not database.get_info(identifier):
                    # download cover and put into db cover url in cache
                    if cache[identifier]["coverurl"] is None:
                        Plogger.log(warn, "coverurl is none. Parsing failed or manga dose not have any cover")
                    image_data = requests.get(cache[identifier]["coverurl"]).content
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
                                      small_cover=small_cover_webp_bytes
                                      )
        except Exception as e:

            Plogger.log(warn, str(e))
        if identifier in cache and "chapter_expire" in cache[identifier].keys() and cache[identifier][
            "chapter_expire"] >= time.time():
            chapters_dict[identifier] = cache[identifier]["current_chapter"]
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
                cache[identifier]["current_chapter"] = highest_chapter
                cache[identifier]["chapter_expire"] = time.time() + 600
            except Exception as e:
                Plogger.log(warn, str(e))
        return chapters_dict

    def get_chapter_list(self, identifier: str, lang: str = "en", cache_only: bool = False) -> Optional[Sequence[dict]]:
        if identifier in cache["get_chapter_list"]:
            if time.time() >= cache["get_chapter_list"][identifier]["expire"]:
                del cache[identifier]
            else:
                return cache["get_chapter_list"][identifier]["list"]
        if cache_only:
            return None
        chapters = self.get_last_chapter_num(identifier)
        if not chapters.get(identifier):
            return None
        chapter_value = chapters.get(identifier)
        chapters = []
        offset = 0
        while offset <= chapter_value:
            chapter = self.session.get(f"{self.API}/chapter",
                                       params={"manga": identifier, "limit": 100, "offset": offset,
                                               "translatedLanguage[]": lang,
                                               "excludedGroups[]": self.exclude_groups}).json()
            chapters.extend(chapter["data"])
            offset += 100
        sorted_chapters = sorted(chapters, key=lambda x: float(x["attributes"]["chapter"]))
        cache["get_chapter_list"][identifier] = {}
        cache["get_chapter_list"][identifier]["expire"] = time.time() + 3600
        cache["get_chapter_list"][identifier]["list"] = sorted_chapters
        if self.cached_chapter_info:
            data = compress(json.dumps(sorted_chapters).encode())
            database.add_chapter_info(identifier, data)
        return sorted_chapters

    def get_chapter_pages(self, identifier: str, db_pages: Sequence, silent=False, generate: Callable = None,
                          page_download_cb: Callable = None, muuid: str = None) -> Sequence[Tuple[int, bytes]]:
        metadata = requests.get(f"https://api.mangadex.org/at-home/server/{identifier}").json()
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

    def get_manga_uuid_from_chapter(self, chapter_uuid):
        muuid = chapter_to_manga_from_cache(chapter_uuid)
        if muuid:
            return muuid
        req = self.session.get(self.API + f"/chapter/{chapter_uuid}")
        if not req:
            return None
        if req.status_code != 200:
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
    "FAST_CHAPTER_FINISH": 2.5,
    "NORMAL_CHAPTER_FINISH": 5.0,
    "SLOW_CHAPTER_FINISH": 10.0,

    "FAST_ON_ERROR": 5,
    "NORMAL_ON_ERROR": 15,
    "SLOW_ON_ERROR": 30,

    "FAST_PAGE_TIMEOUT": 0,
    "NORMAL_PAGE_TIMEOUT": 0.125,
    "SLOW_PAGE_TIMEOUT": 0.25
}


class DownloadProcessor:
    def __init__(self):
        self.mode = settings.DownloadProcessor.get("defaultSpeedMode")
        self.silent_download = settings.DownloadProcessor.get("silentDownload")
        self.save_queue = settings.DownloadProcessor.get("saveQueueOnExit")
        if not os.path.isdir("data"):
            os.mkdir("data")

        if self.save_queue and os.path.isfile(f"{os.getcwd()}\data\queue.json"):
            with open(f"{os.getcwd()}\data\queue.json", "r") as file:
                self.queue = json.load(file)
            with open(f"{os.getcwd()}\data\queue.json", "w") as file:
                json.dump([], file)
        else:
            self.queue = []
        self.currently_working_on = []
        self.in_progress = False
        self.thread_finished = True
        self.waiting_for_job = False
        if settings.DownloadProcessor.get("runOnStart"):
            self.start()

    def start(self):
        self.in_progress = True
        t = threading.Thread(target=self.__download, args=())
        t.start()

    def stop(self):
        if self.save_queue:
            if self.currently_working_on:
                for cwo in self.currently_working_on:
                    found = False
                    for q in self.queue:
                        if q["id"] == cwo["id"]:
                            found = True
                            break
                    if not found:
                        self.queue.append({"id": cwo["id"]})
            with open(f"{os.getcwd()}\data\queue.json", "w") as file:
                json.dump(self.queue, file)
        self.in_progress = False

    def change_mode(self, mode):
        if mode not in ["FAST", "NORMAL", "SLOW"]:
            return
        self.mode = mode

    def running(self):
        return self.in_progress

    def page_download_callback(self, identifier, at_page, page_total):
        for cwo_index, task in enumerate(self.currently_working_on):
            if task["id"] == identifier:
                self.currently_working_on[cwo_index]["status"][1][0] = at_page
                self.currently_working_on[cwo_index]["status"][1][1] = page_total

    def add_chapter(self, job):
        for cwo_index, task in enumerate(self.currently_working_on):
            if task["id"] == job["id"]:
                self.currently_working_on[cwo_index]["status"][0][0] += 1

    def try_conv(self, v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 1

    def __download(self):
        while not self.thread_finished:
            Plogger.log(info, "waiting for thread finish")
            time.sleep(0.25)
            continue
        self.thread_finished = False
        while self.in_progress:
            if self.queue:
                try:

                    job = self.queue.pop(0)
                except IndexError:
                    self.waiting_for_job = True
                    # pause execution no jobs in queue
                    time.sleep(0.25)
                    continue
            else:
                self.waiting_for_job = True
                time.sleep(0.25)
                continue
            if not job:
                continue
            else:
                self.waiting_for_job = False
            try:
                chapter_list = connection.get_chapter_list(job["id"])
                job_finished = False
                try:
                    # best case scenario the volume is set and ordered
                    chapter_list = sorted(chapter_list,
                                          key=lambda x: (
                                              float(x["attributes"]['volume']),
                                              float(x["attributes"]['chapter'])))
                except TypeError:
                    # normal case it will go from 1-N chapters as the usual
                    chapter_list = sorted(chapter_list,
                                          key=lambda x: float(x["attributes"]['chapter']))

                self.currently_working_on.append({"id": job["id"], "status": [[0, len(chapter_list)], ["?", "?"]]})
                for chapter in chapter_list:
                    if job.get("chapter_start"):
                        last_manga_chapter = try_to_get(chapter_list[-1], ["attributes", "chapter"])
                        if float(last_manga_chapter) < job.get("chapter_start"):
                            Plogger.log(warn,
                                        f"cannot download any manga because no scanlation was found. Start {job.get('chapter_start')}. Found {last_manga_chapter}")
                            break
                        chapter_value = try_to_get(chapter, ["attributes", "chapter"])
                        if chapter_value is not None and float(chapter_value) < job.get("chapter_start"):
                            self.add_chapter(job)
                            continue
                    if job.get("chapter_end"):
                        if float(try_to_get(chapter, ["attributes", "chapter"])) >= job.get("chapter_end"):
                            job_finished = True
                            break
                    _id = chapter["id"]
                    Plogger.log(info,
                                f"downloading {chapter['attributes']['volume']} Volume Chapter {chapter['attributes']['chapter']}")
                    pages_in_db = database.get_chapter_pages(_id)
                    # chapter already in database. No need to download
                    if len(pages_in_db) == database.get_record(_id):
                        self.add_chapter(job)
                        Plogger.log(info,
                                    f"already in database {chapter['attributes']['volume']} Volume {chapter['attributes']['chapter']} Chapter")
                        continue
                    # generate is for instant stop of manga downloading until now it just stops
                    for page_value, page in connection.get_chapter_pages(_id, pages_in_db,
                                                                         silent=self.silent_download,
                                                                         generate=self.running,
                                                                         page_download_cb=self.page_download_callback,
                                                                         muuid=job["id"]):
                        database.save_page(_id, page_value, job["id"], page)
                    if not self.running():
                        Plogger.log(info, "stopping thread " + str(self.in_progress))
                        if not job_finished:
                            _, task = find_by_id_in_list(self.currently_working_on, job["id"])
                            if task["status"][0][0] < task["status"][0][1]:
                                self.queue.append(job)
                        return

                    # finished downloading chapter add
                    self.add_chapter(job)
                    Plogger.log(info, "saved to database")
                    time.sleep(timeouts[self.mode + "_CHAPTER_FINISH"])
                # finished downloading remove from currently_working_on
                _, task = find_by_id_in_list(self.currently_working_on, job["id"])
                self.currently_working_on.remove(task)
            except Exception as e:
                Plogger.log(erro, "Dlerror" + str(e))
                # could not find element or timedout
                time.sleep(timeouts[self.mode + "_ON_ERROR"])
                # set job back to front
                self.queue.insert(0, job)
        Plogger.log(info, "stopping download from worker and exiting thread")
        self.thread_finished = True

    def test(self):
        self.__download()


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
                        "AlertSystem cooldown is set lower that 60! anything lower that atlest 300-600 could get your ip restriced. Please keep that in mind")
            return
        if self.cooldown <= 300:
            Plogger.log(warn,
                        "AlertSystem cooldown. anything lower that atlest 300-600 could get your ip restriced. Please keep that in mind")
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
            if time.time() > start + self.cooldown:
                self.track()
                start = time.time()
            time.sleep(0.5)

    def send_alert(self, data):
        if self.sound_alert:
            try:
                PlaySound(self.audio)
            except Exception as e:
                Plogger.log(erro, f"Could not play sound due to: {e}")
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

                DlProcessor.queue.append({"id": manga, "chapter_start": chapter_before[manga]})
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


database: Database = None
connection: MangadexConnection = None
DlProcessor: DownloadProcessor = None
alert_sys: AlertSystem = None
session_manager: SessionManager = None
cache: dict = {"search": {}, "chapter_to_manga": {}, "get_chapter_list": {}}


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


def initialize():
    global database, connection, DlProcessor, alert_sys, session_manager
    if database is None or connection is None or DlProcessor is None or alert_sys is None or session_manager is None:
        database = Database()
        connection = MangadexConnection()
        DlProcessor = DownloadProcessor()
        alert_sys = AlertSystem()
        session_manager = SessionManager()
        create_chapter_to_manga_cache()
        if settings.OnStart.get("cacheMangas"):
            # start on diffrent thread so it dose not block it takes quite a lot of time
            t = threading.Thread(target=preload_mangas)
            t.start()
    return True


def search_manga(name):
    if database is None or connection is None or DlProcessor is None:
        initialize()
    return connection.search_manga(name, limit=5)


def download_manga(manga):
    # raise DeprecationWarning("Dont use pls")
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

        # chapter already in database. No need to download
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
        cache["chapter_to_manga"][muuid] = [i["id"] for i in json.loads(decompress(chapter_info_str).decode())]


def chapter_to_manga_from_cache(cuuid):
    for muuid in cache["chapter_to_manga"]:
        for db_cuuid in cache["chapter_to_manga"][muuid]:
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


def threaded_downloader():
    """
    concept (because not much time and my memory is bad):
    list that all threads can access (to install from start to end not random from 1-N chapter)

    list that will store downloaded content and save it to database so the threadeds don't race each other
    or in the case of sqlite throw exception (not handling that or SystemError that escapes try/except for some reason

    make sure threads dont save record so it won't intefere with main thread (save on main thread)

    some fail safeties:
        stop all threads and warn user when rate limit is reached (maybe some couter so it dosent actually hit it)
        limit threads to some reasonal number that i will find while testing

    dont get too silly and overload/get banned from mangadex servers
    """


if __name__ == "__main__":
    database = Database()
    connection = MangadexConnection()
    DlProcessor = DownloadProcessor()
    # update cover arts
    # database.c.execute("SELECT identifier FROM info")
    # for i in database.c.fetchall():
    #     _id = i[0]
    #     connection.coverart_update(_id)
    #     time.sleep(1)
