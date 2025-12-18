import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import List, Optional, Tuple, Dict

from rewrite.backend.utils import perf_test, convert_to_webp
from rewrite.backend.schemas import MangaIdentifier, ChapterIdentifier, ChapterAttributes, Chapter, Manga, \
    MangaAttributes, LatestChapter, COVER_ART_MAX_SIZE, COVER_ART_256_SIZE, COVER_ART_512_SIZE, from_json, \
    from_database_row, ChapterList


@dataclass
class MangaDownloadJobInDatabase:
    # Stores the pages in database and record (how many pages does the chapter have)
    pages_in_db: Dict  # {"cuuid": [1, 2, 3, 4, 5], ...}
    records: Dict  # {"cuuid": 12, ...}

class Database:
    def __init__(self):
        self.conn = sqlite3.connect("database.db", check_same_thread=False, timeout=10)
        cursor = self.conn.cursor()
        self.conn.execute("PRAGMA foreign_keys = ON")
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS chapters (
            cuuid CHAR(36) PRIMARY KEY,
            type TEXT,
            relationships TEXT
        );
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS chapter_attributes (
            cuuid CHAR(36) PRIMARY KEY,
            muuid CHAR(36) NOT NULL,
            title TEXT,
            volume TEXT,
            chapter TEXT,
            pages INTEGER NOT NULL,
            translatedLanguage TEXT NOT NULL,
            uploader TEXT,
            externalUrl TEXT,
            version INTEGER NOT NULL,
            createdAt TEXT NOT NULL,
            updatedAt TEXT NOT NULL,
            publishAt TEXT NOT NULL,
            readableAt TEXT NOT NULL,
            FOREIGN KEY (cuuid) REFERENCES chapters(cuuid)
        );
        """)
        cursor.execute("""CREATE TABLE IF NOT EXISTS manga_attributes (
            muuid CHAR(36) NOT NULL PRIMARY KEY,
            title TEXT NOT NULL,
            altTitles TEXT NOT NULL, -- List[LocalizedString] as JSON
            description TEXT NOT NULL,
            isLocked BOOLEAN NOT NULL,
            links TEXT NOT NULL, -- Links as JSON
            originalLanguage TEXT NOT NULL,
            lastVolume TEXT, -- Optional
            lastChapter TEXT, -- Optional
            publicationDemographic TEXT, -- Optional
            status TEXT, -- Optional
            year INTEGER, -- Optional
            contentRating TEXT NOT NULL,
            chapterNumbersResetOnNewVolume BOOLEAN NOT NULL,
            availableTranslatedLanguages TEXT NOT NULL, -- List[str] as JSON
            latestUploadedChapter TEXT NOT NULL,
            tags TEXT NOT NULL, -- List[Tag] as JSON
            state TEXT NOT NULL,
            version INTEGER NOT NULL,
            createdAt TEXT NOT NULL, -- ISO 8601 Date-Time as TEXT
            updatedAt TEXT NOT NULL -- ISO 8601 Date-Time as TEXT
        );""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS chapter_page (
    page_number INT NOT NULL,
    page_content BLOB NOT NULL,
    cuuid CHAR(36) NOT NULL,
    FOREIGN KEY (cuuid) REFERENCES chapter_attributes(cuuid),
    UNIQUE (cuuid, page_number)
);""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS cover_art(
            muuid CHAR(36) NOT NULL,
            size INTEGER NOT NULL,
            data BLOB NOT NULL,
            PRIMARY KEY(muuid, size)
        )""")

        cursor.execute("""CREATE TABLE IF NOT EXISTS latest_chapter (
            muuid CHAR(36) NOT NULL primary key,
            latest_chapter CHAR(36) NOT NULL,
            createdAt TEXT NOT NULL, -- ISO 8601 Date-Time as TEXT
            updatedAt TEXT NOT NULL, -- ISO 8601 Date-Time as TEXT
            volume TEXT,
            chapter TEXT
        )""")

        cursor.execute("""CREATE TABLE IF NOT EXISTS manga_feed (
            muuid CHAR(36) NOT NULL primary key,
            feed BLOB NOT NULL
        )""")

        cursor.execute("""CREATE TABLE IF NOT EXISTS chapters_read (
            muuid CHAR(36) NOT NULL,
            cuuid CHAR(36) NOT NULL,
            PRIMARY KEY(muuid, cuuid)
        )""")

        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_chapter_attributes_cuuid ON chapter_attributes(cuuid);
        """)
        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_chapter_page_chapter_id ON chapter_page(cuuid);
        """)
        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_muuid ON chapter_attributes (muuid);
        """)

        # TODO: Make index from chapter to chapter attributes if performance begins to be a problem

        self.conn.commit()
        cursor.close()
        # Sqlite3 dose not like access from multiple threads so we use lock to make sure to only access one at time
        self.lock = threading.Lock()

    def add_read_record(self, muuid, cuuid):
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("INSERT INTO chapters_read VALUES (:muuid, :cuuid)", {"muuid": muuid, "cuuid": cuuid})
            self.conn.commit()

    def remove_read_record(self, muuid, cuuid):
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("DELETE FROM chapters_read WHERE muuid=:muuid AND cuuid=:cuuid", {"muuid": muuid, "cuuid": cuuid})
            self.conn.commit()
            cursor.close()

    def get_manga_read_chapters(self, muuid):
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT cuuid FROM chapters_read WHERE muuid=:muuid", {"muuid": muuid})
            read = cursor.fetchall()
            cursor.close()
            if read:
                return [i[0] for i in read]
            return None

    def get_chapter_read_status(self, cuuid):
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT cuuid FROM chapters_read WHERE cuuid=:cuuid", {"cuuid": cuuid})
            read = cursor.fetchone()
            cursor.close()
            if read:
                return read
            return None

    @perf_test
    def get_chapter_attribute(self, identifier: ChapterIdentifier) -> Optional[ChapterAttributes]:
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM chapter_attributes WHERE cuuid=:id", {"id": identifier})
            fetch = cursor.fetchone()
            cursor.close()
            if fetch:
                return ChapterAttributes(*fetch[2:])
            return None

    @perf_test
    def get_chapter_attribute_raw(self, identifier: ChapterIdentifier) -> Optional[ChapterAttributes]:
        # Returns the raw fetch. Because the raw fetch returns muuid and cuuid which can be usefull
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM chapter_attributes WHERE cuuid=:id", {"id": identifier})
            fetch = cursor.fetchone()
            cursor.close()
            if fetch:
                return fetch
            return None

    @perf_test
    def set_chapter_attributes(self, identifier: MangaIdentifier, chapters: List[Chapter]) -> None:
        with self.lock:
            cursor = self.conn.cursor()
            cursor.executemany("REPLACE INTO chapters VALUES (?, ?, ?)", [
                [chapter.id, chapter.type, json.dumps([asdict(relationship) for relationship in chapter.relationships])] for
                chapter in chapters
            ])
            cursor.executemany("REPLACE INTO chapter_attributes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               [[i.id, identifier] + list(asdict(i.attributes).values()) for i in chapters])
            self.conn.commit()
            cursor.close()

    @perf_test
    def get_user_and_groups(self, cuuids: List[str]) -> Optional[List[dict]]:
        with self.lock:
            if not cuuids:
                return None

            cursor = self.conn.cursor()

            placeholders = ",".join(["?"] * len(cuuids))
            query = f"""
                SELECT cuuid, relationships
                FROM chapters
                WHERE cuuid IN ({placeholders})
            """

            cursor.execute(query, cuuids)
            rows = cursor.fetchall()

            if not rows:
                return None

            result = {}

            for row in rows:
                user = None
                group = None
                relationships = json.loads(row[1])
                for relationship in relationships:
                    if relationship["type"] == "scanlation_group" and relationship["attributes"] is not None:
                        group = relationship["attributes"] | {"id": relationship["id"]}
                    if relationship["type"] == "user" and relationship["attributes"] is not None:
                        user = relationship["attributes"] | {"id": relationship["id"]}

                result[row[0]] = {"user": user, "scanlation_group": group}

            return result

    @perf_test
    def set_manga_attributes(self, manga: Manga):
        with self.lock:
            cursor = self.conn.cursor()
            data = [manga.id]
            for i in asdict(manga.attributes).values():
                if isinstance(i, (dict, list)):
                    data.append(json.dumps(i))
                else:
                    data.append(i)
            cursor.execute("REPLACE INTO manga_attributes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                           "?, ?, ?)", data)
            self.conn.commit()
            cursor.close()

    @perf_test
    def get_manga_attributes(self, identifier: MangaIdentifier) -> Optional[MangaAttributes]:
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM manga_attributes WHERE muuid=:identifier", {"identifier": identifier})
            fetch = cursor.fetchone()
            cursor.close()
            if fetch:
                return from_database_row(MangaAttributes, fetch[1:])
            return None

    @perf_test
    def get_chapter_list(self, identifier: MangaIdentifier) -> Optional[List[ChapterAttributes]]:
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM chapter_attributes WHERE muuid=:identifier", {"identifier": identifier})
            chapters = [ChapterAttributes(*chapter[2:]) for chapter in cursor.fetchall()]
            cursor.close()
            if chapters:
                return chapters
            return None

    @perf_test
    def set_chapter_page(self, page, content, cuuid) -> None:
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("INSERT INTO chapter_page VALUES (:p_count, :p_content, :cuuid)",
                           {"p_count": page, "p_content": content, "cuuid": cuuid})
            self.conn.commit()
            cursor.close()

    @perf_test
    def set_chapter_pages(self, batch: List[Tuple[int, bytes, str]], is_webp=False):
        with self.lock:
            cursor = self.conn.cursor()
            if not is_webp:
                png_images_batch = [i[1] for i in batch]

                with ThreadPoolExecutor() as executor:
                    results = list(executor.map(convert_to_webp, png_images_batch))

                webp_batch = []
                for idx, i in enumerate(batch):
                    webp_batch.append((i[0], results[idx], i[2]))

                cursor.executemany("INSERT OR REPLACE INTO chapter_page VALUES (?, ?, ?)", webp_batch)
            else:
                cursor.executemany("INSERT OR REPLACE INTO chapter_page VALUES (?, ?, ?)", batch)
            self.conn.commit()
            cursor.close()

    @perf_test
    def get_manga_job(self, identifier: MangaIdentifier) -> MangaDownloadJobInDatabase:
        with self.lock:
            cursor = self.conn.cursor()
            mdj = MangaDownloadJobInDatabase({}, {})

            cursor.execute("SELECT cuuid, pages FROM chapter_attributes WHERE muuid=:muuid", {"muuid": identifier})
            chapter_records = cursor.fetchall()

            for cuuid, record in chapter_records:
                mdj.records[cuuid] = record

            chapter_ids = [chapter[0] for chapter in chapter_records]
            if not chapter_ids:
                return mdj

            if chapter_ids:
                placeholders = ', '.join(['?'] * len(chapter_ids))
                sql_query = f"""
                SELECT cuuid, page_number
                FROM chapter_page
                WHERE cuuid IN ({placeholders});
                """
                cursor.execute(sql_query, chapter_ids)
                pages_in_db = cursor.fetchall()

                for cuuid, page in pages_in_db:
                    if cuuid not in mdj.pages_in_db:
                        mdj.pages_in_db[cuuid] = []
                    mdj.pages_in_db[cuuid].append(page)
            cursor.close()
            return mdj

    def get_cover_art(self, identifier: MangaIdentifier, size=COVER_ART_MAX_SIZE, size_any=False) -> Optional[bytes]:
        with self.lock:
            cursor = self.conn.cursor()
            if size_any:
                cursor.execute("SELECT data FROM cover_art WHERE muuid=:id", {"id": identifier})
            else:
                cursor.execute("SELECT data FROM cover_art WHERE muuid=:id and size=:size",
                               {"id": identifier, "size": size})
            fetch = cursor.fetchone()
            cursor.close()
            if fetch:
                return fetch[0]
            return fetch

    @perf_test
    def set_cover_art(self, identifier: MangaIdentifier, size: int, content: bytes) -> None:
        with self.lock:
            cursor = self.conn.cursor()
            assert size in {COVER_ART_MAX_SIZE, COVER_ART_256_SIZE, COVER_ART_512_SIZE}, "Unsupported cover art size"
            webp_image = convert_to_webp(content)
            cursor.execute(
                """INSERT INTO cover_art (muuid, size, data) VALUES (?, ?, ?) ON CONFLICT(muuid, size) DO UPDATE SET data = excluded.data""",
                (identifier, size, webp_image))
            self.conn.commit()
            cursor.close()

    @perf_test
    def get_page(self, identifier: str, page: int) -> Optional[bytes]:
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT page_content FROM chapter_page WHERE cuuid=:identifier AND page_number=:page",
                           {"identifier": identifier, "page": page})
            fetch = cursor.fetchone()
            cursor.close()
            return None if fetch is None else fetch[0]

    @perf_test
    def get_pages(self, identifier: ChapterIdentifier):
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT page_number, page_content FROM chapter_page WHERE cuuid=:identifier",
                           {"identifier": identifier})
            fetch = cursor.fetchall()
            cursor.close()
            if fetch:
                return fetch
            return None

    @perf_test
    def all_manga_in_db(self) -> Optional[List[str]]:
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT muuid, title, altTitles, description FROM manga_attributes")
            fetch = cursor.fetchall()
            cursor.close()
            if fetch:
                return fetch
            return None

    @perf_test
    def get_downloaded_pages(self, identifier: MangaIdentifier):
        with self.lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    """
                    SELECT 
                            ca.cuuid, 
                            ca.title, 
                            ca.volume, 
                            ca.chapter
                        FROM chapter_attributes ca
                        WHERE ca.muuid = ?
                          AND ca.pages = (
                              SELECT COUNT(cuuid)
                              FROM chapter_page cp
                              WHERE cp.cuuid = ca.cuuid
                          )
                    """,
                    (identifier,)
                )
                return cursor.fetchall()
            finally:
                cursor.close()

    def chapter_to_manga_identifier(self, identifier: ChapterIdentifier) -> Optional[MangaIdentifier]:
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("""SELECT muuid FROM chapter_attributes WHERE cuuid=:identifier""", {"identifier": identifier})
            muuid = cursor.fetchone()
            cursor.close()
            if not muuid:
                return None
            return muuid[0]

    @perf_test
    def get_next_prev(self, muuid: MangaIdentifier, target_cuuid: ChapterIdentifier, feed: ChapterList) -> Optional[tuple]:
        downloaded_chapters = [i[0] for i in self.get_downloaded_pages(muuid)]
        with self.lock:
            if not downloaded_chapters:
                return None, None

            in_order_all_chapters = [chapter.id for chapter in feed.data]

            in_order_downloaded = []
            for cuuid in in_order_all_chapters:
                if cuuid in downloaded_chapters:
                    in_order_downloaded.append(cuuid)

            index = in_order_downloaded.index(target_cuuid)
            prev = in_order_downloaded[index - 1] if index > 0 else None
            _next = in_order_downloaded[index + 1] if index < len(in_order_downloaded) - 1 else None

            return prev, _next

    def get_latest_chapters(self) -> Optional[List[LatestChapter]]:
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM latest_chapter")
            chapters = cursor.fetchall()
            cursor.close()
            if chapters:
                return [LatestChapter(*chapter) for chapter in chapters]
            return None

    def get_latest_chapter(self, muuid: str) -> Optional[LatestChapter]:
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM latest_chapter WHERE muuid=:muuid", {"muuid": muuid})
            chapter = cursor.fetchone()
            cursor.close()
            if chapter:
                return LatestChapter(*chapter)
            return None

    def set_latest_chapter(self, chapter: LatestChapter) -> None:
        with self.lock:
            cursor = self.conn.cursor()
            data = [value for value in asdict(chapter).values()]
            cursor.execute("REPLACE INTO latest_chapter VALUES (?, ?, ?, ?, ?, ?)", data)
            self.conn.commit()
            cursor.close()

    def delete_manga(self, muuid: MangaIdentifier) -> bool:
        with self.lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute("SELECT cuuid FROM chapter_attributes WHERE muuid=:muuid", {"muuid": muuid})
                cuuids = [i[0] for i in cursor.fetchall()]

                if cuuids:
                    placeholders = ",".join("?" for _ in cuuids)
                    sql = f"DELETE FROM chapter_page WHERE cuuid IN ({placeholders})"
                    cursor.execute(sql, cuuids)

                self.conn.commit()
                cursor.close()
                return True
            except Exception as e:
                print(f"error: {e}")
                cursor.close()
                return False

    def set_manga_feed(self, identifier: MangaIdentifier, feed: ChapterList) -> None:
        with self.lock:
            try:
                feed_blob = json.dumps([asdict(chapter) for chapter in feed.data])
            except Exception as e:
                print(f"error: Could not serialize feed into string {e}")
                return

            cursor = self.conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO manga_feed VALUES (?, ?)", (identifier, feed_blob))
            self.conn.commit()
            cursor.close()

    def get_manga_feed(self, identifier) -> Optional[ChapterList]:
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT feed FROM manga_feed WHERE muuid=:identifier", {"identifier": identifier})
            feed = cursor.fetchone()
            if feed:
                feed = feed[0]
            else:
                return None
            cursor.close()
            return from_database_row(ChapterList, ("", "", feed, 0, 0, 0))

