from dataclasses import dataclass
from typing import Optional, Callable


@dataclass
class Settings:
    onMangaDownloadFinishHandler: Optional[Callable]
