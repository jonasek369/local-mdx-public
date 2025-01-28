import time

from connection import MangaDownloader, MangaDownloadJob, MangadexConnection
from repository import MangaRepository
from settings import Settings

repository = MangaRepository()

md = MangaDownloader(Settings(repository.store_downloaded_pages))


while True:
    print(md.currently_working_on)
    time.sleep(1)
