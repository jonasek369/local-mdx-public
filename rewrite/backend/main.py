import json
import time
from dataclasses import asdict

from connection import MangaDownloader, MangaDownloadJob, MangadexConnection
from repository import MangaRepository
from settings import Settings, load_settings