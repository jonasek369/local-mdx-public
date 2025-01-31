from dataclasses import asdict
from typing import Optional

import requests

from rewrite.backend.connection import MangadexConnection, MangaDownloader
from rewrite.backend.database import Database
from rewrite.backend.schemas import MangaIdentifier, ChapterIdentifier, ChapterList, Manga, MangaAttributes, \
    ChapterAttributes
from rewrite.backend.settings import load_settings
from rewrite.backend.utils import resize_image, perf_test


# Taking inspiration from how android works utilizing repositories which take connection nad database
# so it interfaces finding offline and online data


class MangaRepository:
    def __init__(self):
        self.settings = load_settings()
        self.settings.onMangaDownloadFinishHandler = self.store_downloaded_pages

        self.database = Database()
        self.connection = MangadexConnection(self.settings)
        self.downloader = MangaDownloader(self.settings)

    def store_downloaded_pages(self, muuid: MangaIdentifier, cuuid: ChapterIdentifier, downloader: MangaDownloader):
        chapter = self.database.get_chapter_attribute(cuuid)
        if not chapter:
            chapter_list = self.connection.get_chapter_list(muuid)
            self.database.set_chapter_attributes(muuid, chapter_list.data)
        for page, content in enumerate(downloader.finished[muuid][cuuid]):
            self.database.set_chapter_page(page + 1, content, cuuid)
        del downloader.finished[muuid][cuuid]

    def get_manga_attributes(self, identifier: MangaIdentifier) -> Optional[MangaAttributes]:
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
        raise NotImplemented("Error")

    def get_cover_art(self, identifier: MangaIdentifier, small=False) -> Optional[bytes]:
        if small:
            small_cover_art = self.database.get_small_cover_art(identifier)
            if not small_cover_art:
                fetch_small_cover_art = self.connection.get_cover_art(identifier)
                if fetch_small_cover_art is not None:
                    self.database.set_small_cover_art(identifier, fetch_small_cover_art)
                return fetch_small_cover_art
            return small_cover_art
        else:
            cover_art = self.database.get_cover_art(identifier)
            if not cover_art:
                fetch_cover_art = self.connection.get_cover_art(identifier)
                if fetch_cover_art is not None:
                    self.database.set_cover_art(identifier, fetch_cover_art)
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
        pages = self.database.get_downloaded_pages(identifier)
        return pages

    def get_page(self, identifier: MangaIdentifier, page: int):
        page = self.database.get_page(identifier, page)
        return page

    def get_pages(self, identifier: MangaIdentifier):
        pages = self.database.get_pages(identifier)
        return pages

    def get_next_prev(self, identifier: ChapterIdentifier):
        next_prev = self.database.get_next_prev(identifier)
        return next_prev

    @perf_test
    def popular_new_titles(self):
        popular = self.connection.get_popular_new_titles()
        for manga in popular.data:
            for relationship in manga.relationships:
                if relationship.type == "cover_art":
                    coverurl = f"https://mangadex.org/covers/{manga.id}/" + relationship.attributes["fileName"]
                    if not self.database.get_cover_art(manga.id):
                        self.database.set_cover_art(manga.id, self.connection.safe_get_request(coverurl).content)
        if popular is None:
            return popular
        return asdict(popular)["data"]