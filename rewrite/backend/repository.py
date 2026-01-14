import asyncio
import json
import time
from dataclasses import asdict
from typing import Optional, List, Set, Tuple

import aiohttp

from rewrite.backend.connection import MangadexConnection, CredentialManager, MangaDownloader
from rewrite.backend.database import Database
from rewrite.backend.schemas import MangaIdentifier, ChapterIdentifier, ChapterList, MangaAttributes, \
    ChapterAttributes, LatestChapter, MangaList, COVER_ART_MAX_SIZE, COVER_ART_512_SIZE, from_json, Chapter, Manga
from rewrite.backend.settings import load_settings, load_credentials
from rewrite.backend.utils import perf_test, info, error, get_relationships, warning, debug


# Taking inspiration from how android works utilizing repositories which take connection nad database
# so it interfaces finding offline and online data
class MangaRepository:
    def __init__(self):
        self.settings = load_settings()
        self.settings.onMangaDownloadFinishHandler = self.store_downloaded_pages

        self.database = Database()
        self.credential_manager = CredentialManager(self.settings, load_credentials())
        self.connection = MangadexConnection(self.settings, self.credential_manager)
        self.downloader = MangaDownloader(self.settings)

    def store_downloaded_pages(self, muuid: MangaIdentifier, cuuid: ChapterIdentifier, downloader: MangaDownloader):
        chapter = self.database.get_chapter_attribute(cuuid)
        if not chapter:
            chapter_list = self.connection.get_chapter_list(muuid)
            self.database.set_chapter_attributes(muuid, chapter_list.data)
        batch = []
        for page, content in enumerate(downloader.finished[muuid][cuuid]):
            batch.append((page + 1, content, cuuid))
        self.database.set_chapter_pages(batch)
        self.settings.logger.log(debug, f"Saved {len(batch)} pages")
        del downloader.finished[muuid][cuuid]

    def get_manga_attributes(self, identifier: MangaIdentifier, local_only: bool = False) -> Optional[MangaAttributes]:
        if not local_only:
            manga = self.connection.get_manga(identifier)
            if manga is not None:
                self.database.set_manga_attributes(manga)
                return manga.attributes
        db_manga = self.database.get_manga_attributes(identifier)
        if db_manga:
            return db_manga
        return None

    def get_chapter_list(self, identifier: MangaIdentifier) -> ChapterList:
        chapter_list = self.connection.get_chapter_list(identifier)
        if chapter_list:
            self.database.set_chapter_attributes(identifier, chapter_list.data)
            return chapter_list
        raise NotImplemented("Error. Offline usage of get_chapter_list is not Implemented!")

    def get_cover_art(self, identifier: MangaIdentifier, size: int, size_any=False) -> Optional[bytes]:
        cover_art = self.database.get_cover_art(identifier, size=size, size_any=size_any)
        if not cover_art:
            fetch_cover_art = self.connection.get_cover_art(identifier, size=size)
            if fetch_cover_art is not None:
                self.database.set_cover_art(identifier, size, fetch_cover_art)
            return fetch_cover_art
        return cover_art

    def get_chapter_attributes(self, identifier: MangaIdentifier, ids: Optional[dict[str, str]] = None) -> Optional[
        ChapterAttributes]:
        """
        Retrieves chapter attributes for the given identifier.

        If `ids` is provided, it is a dictionary where the `muuid` and `cuuid`
        are added as keys with their values.
        """
        chapters = self.database.get_chapter_attribute_raw(identifier)
        if chapters:
            cuuid, muuid, attributes = *chapters[:2], ChapterAttributes(*chapters[2:])
            if ids is not None:
                ids["muuid"] = muuid
                ids["cuuid"] = cuuid
            return attributes
        return None

    def get_downloaded_pages(self, identifier: MangaIdentifier):
        pages = self.database.get_downloaded_chapters(identifier)
        return pages

    def get_page(self, identifier: MangaIdentifier, page: int):
        page = self.database.get_page(identifier, page)
        return page

    def get_pages(self, identifier: MangaIdentifier):
        pages = self.database.get_pages(identifier)
        return pages

    @staticmethod
    def resolve_chapter_id(chapter):
        if chapter["isUnavailable"] or not chapter["id"]:
            return chapter["others"][0] if chapter["others"] else None
        return chapter["id"]

    @staticmethod
    def get_adjacent_chapter_id(
            *,
            volumes,
            volume,
            current_volume_chapters,
            current_chapter_index,
            direction
    ):
        """
        MangaDex aggregate chapters are sorted in descending order.
        Index 0 = highest chapter in volume.
        direction:
            -1 -> next chapter
             1 -> previous chapter
        """
        if direction not in (-1, 1) or not isinstance(direction, int):
            raise Exception("Only 1 and -1 is supported as an direction. -1 -> next. 1 -> prev")

        keys = list(current_volume_chapters.keys())
        target_index = current_chapter_index + direction

        if 0 <= target_index < len(keys):
            chapter = current_volume_chapters[keys[target_index]]
            return MangaRepository.resolve_chapter_id(chapter)

        volume_keys = list(volumes.keys())
        volume_index = volume_keys.index(volume) + direction

        if volume_index < 0 or volume_index >= len(volume_keys):
            return None

        adjacent_volume_chapters = volumes[volume_keys[volume_index]]["chapters"]
        if not adjacent_volume_chapters:
            return None

        chapter_keys = list(adjacent_volume_chapters.keys())
        chapter = (
            adjacent_volume_chapters[chapter_keys[-1]]
            if direction == -1
            else adjacent_volume_chapters[chapter_keys[0]]
        )

        return MangaRepository.resolve_chapter_id(chapter)

    def get_manga_aggregate(self, muuid, params) -> Optional[dict]:
        connection_aggregate = self.connection.get_manga_aggregate(muuid, params=params)
        str_params = json.dumps(params)
        if connection_aggregate is None:
            database_aggregate = self.database.get_manga_aggragate(muuid, str_params)
            if database_aggregate:
                return database_aggregate
            if database_aggregate is None:
                self.settings.logger.log(warning,
                                         "Could not fetch aggregate and there is no local saved. When reading it will result to redirection to the manga instead of next chapter. Connect to internet to resolve.")
                return None
        else:
            self.database.set_manga_aggregate(muuid, str_params, json.dumps(connection_aggregate))
            return connection_aggregate

    def get_next_prev(self, identifier: ChapterIdentifier) -> Tuple[Optional[str], Optional[str]]:
        """
        This fucntion walks the aggregate provided from mangadex and gets next and prev chapter thanks to this
        of manga has multiple chapters it will go to next one with the bonus of keeping the same scanlation group
        if possible if not it will choose first one in the list `others`

        if my implementation is right it should be exact same function as mangadex
        """
        _next, prev = None, None
        muuid = self.database.chapter_to_manga_identifier(identifier)
        feed = self.get_manga_feed(muuid, force_latest=False)
        current_chapter_filtered = list(filter(lambda chap: chap.id == identifier, feed.data))
        if not current_chapter_filtered:
            return _next, prev
        current_chapter: Chapter = current_chapter_filtered[0]
        current_chapter_groups = get_relationships(current_chapter.relationships, "scanlation_group")

        params = {
            "translatedLanguage[]": self.settings.translatedLanguage
        }

        if current_chapter_groups is not None:
            params["groups[]"] = [group.id for group in current_chapter_groups]

        aggregate = self.get_manga_aggregate(muuid, params)
        if aggregate is None:
            return None, None
        volume, chapter = current_chapter.attributes.volume, current_chapter.attributes.chapter
        volumes = aggregate["volumes"]
        # if no volume is set mangadex expects string 'none' not json null
        if volume is None:
            volume = "none"
        if volume not in volumes:
            raise KeyError(f"Volume {volume} not found")
        current_volume_chapters = volumes[volume]["chapters"]

        if chapter not in current_volume_chapters:
            raise KeyError(f"Chapter {chapter} not found in volume {volume}")
        current_chapter_index = list(current_volume_chapters.keys()).index(chapter)

        _next = self.get_adjacent_chapter_id(
            volumes=volumes,
            volume=volume,
            current_volume_chapters=current_volume_chapters,
            current_chapter_index=current_chapter_index,
            direction=-1
        )

        prev = self.get_adjacent_chapter_id(
            volumes=volumes,
            volume=volume,
            current_volume_chapters=current_volume_chapters,
            current_chapter_index=current_chapter_index,
            direction=1
        )

        if not self.database.is_chapter_downloaded(_next):
            _next = None
        if not self.database.is_chapter_downloaded(prev):
            prev = None

        return _next, prev

    def get_manga_feed(self, identifier: MangaIdentifier, force_latest=False) -> ChapterList:
        if not force_latest:
            feed = self.database.get_manga_feed(identifier)
            if feed is not None:
                return feed
        feed = self.connection.get_manga_feed(identifier, self.settings.translatedLanguage)
        if feed is not None:
            self.database.set_manga_feed(identifier, feed)
        return feed

    async def _fetch_and_store_cover(self, session, identifier: MangaIdentifier, coverurl: str) -> None:
        async with session.get(coverurl) as resp:
            resp.raise_for_status()
            content = await resp.read()
            self.database.set_cover_art(identifier, COVER_ART_512_SIZE, content)

    async def popular_new_titles(self):
        popular = self.connection.get_popular_new_titles()
        if popular is None:
            return None

        async with aiohttp.ClientSession() as session:
            tasks = []
            for manga in popular.data:
                for relationship in manga.relationships:
                    if relationship.type == "cover_art":
                        coverurl = f"https://mangadex.org/covers/{manga.id}/{relationship.attributes['fileName']}.512.jpg"
                        if not self.database.get_cover_art(manga.id, COVER_ART_512_SIZE):
                            # Create async download task
                            tasks.append(self._fetch_and_store_cover(session, manga.id, coverurl))

            if tasks:
                await asyncio.gather(*tasks)

        return popular

    @perf_test
    def __get_updates(self):
        # TODO: attributes have any language not just our selected
        # This is pretty intresting approach but its not complete
        # And it would cost multiple api calls. Ive decided it will be better
        # To use mangadexes feed which comes with cost of requiring users credentials
        return None
        downloaded_mangas = self.database.all_manga_in_db()
        latest_chapters = self.database.get_latest_chapters()
        latest_chapters_map = {}
        if latest_chapters is not None:
            for latest_chapter in latest_chapters:
                latest_chapters_map[latest_chapter.muuid] = latest_chapters
        else:
            latest_chapters_map = {}
        muuid_list = [manga[0] for manga in downloaded_mangas]
        attribute_map = {}
        for muuid in muuid_list:
            manga = self.connection.get_manga(muuid)
            if manga is not None:
                attribute_map[muuid] = manga.attributes
                self.database.set_manga_attributes(manga)
        for muuid in latest_chapters_map.keys():
            if muuid not in attribute_map:
                continue
            if attribute_map[muuid].latestUploadedChapter != latest_chapters_map[muuid]:  # new != old
                self.settings.logger.log(info, f"Update happened!")
                self.settings.logger.log(info, f"{attribute_map[muuid].latestUploadedChapter} released")

        for muuid in attribute_map.keys():
            chapter = self.connection.get_chapter(attribute_map[muuid].latestUploadedChapter)
            if chapter is not None:
                self.database.set_latest_chapter(
                    LatestChapter(
                        muuid,
                        attribute_map[muuid].latestUploadedChapter,
                        chapter.attributes.updatedAt,
                        chapter.attributes.createdAt,
                        chapter.attributes.volume,
                        chapter.attributes.chapter
                    )
                )

    def sync_libraries(self, manga_uuids: List[str] = None):
        if not self.credential_manager.validate_token(self.credential_manager.token):
            return None
        if not manga_uuids:
            self.connection.sync_custom_list([manga[0] for manga in self.database.all_manga_in_db()])
        else:
            self.connection.sync_custom_list(manga_uuids)

    @perf_test
    def get_updates(self, limit, offset):
        # TODO: Store in db
        sync_list_uuid, _ = self.connection.get_sync_list()
        feed = self.connection.get_custom_list_feed(sync_list_uuid, limit, offset)
        if feed is None:
            self.settings.logger.log(warning, "Could not get the feed")
            return None
        if isinstance(feed, int):
            self.settings.logger.log(info, "Credentials are not set")
            return None
        return feed

    def get_latest_updated_chapters(self) -> ChapterList | None:
        updates = self.connection.get_latest_updated_chapters()
        return updates
