from dataclasses import dataclass, is_dataclass, asdict
from pprint import pprint
from typing import Optional, get_type_hints, Union, Type, get_origin, get_args, Dict, List

# Schema for https://api.mangadex.org/docs/swagger.html
# Version 5.10.2

# LocalizedString {
#   < * >:	string
#   pattern: ^[a-z]{2,8}$
# }
LocalizedString = dict
# {
#   < * >:	string
# }
Links = dict

# This means literally the same, but I want to be visible what the connection wants
ChapterIdentifier = str
MangaIdentifier = str


def from_json(dataclass_type, json_data):
    """
    Populate a dataclass instance from a JSON object, handling nested dataclasses and collections.

    :param dataclass_type: The dataclass type to instantiate.
    :param json_data: The JSON data to populate the dataclass.
    :return: An instance of the dataclass populated with the JSON data.
    """
    if not is_dataclass(dataclass_type):
        raise TypeError(f"{dataclass_type} must be a dataclass type.")

    args = {}
    hints = get_type_hints(dataclass_type)

    for key, _type in hints.items():
        if key in json_data:
            value = json_data[key]
            if is_dataclass(_type):
                # Recursively handle nested dataclass
                args[key] = from_json(_type, value)
            elif hasattr(_type, "__origin__") and _type.__origin__ in {list, List}:
                # Handle list of nested dataclasses or primitive types
                item_type = _type.__args__[0]
                if is_dataclass(item_type):
                    # Handle a list of dataclasses
                    args[key] = [from_json(item_type, item) for item in value]
                else:
                    # Handle a list of primitives (e.g., strings, ints)
                    args[key] = value
            elif hasattr(_type, "__origin__") and _type.__origin__ in {dict, Dict}:
                # Handle dictionaries (basic or nested types)
                key_type, value_type = _type.__args__
                if is_dataclass(value_type):
                    args[key] = {k: from_json(value_type, v) for k, v in value.items()}
                else:
                    args[key] = value
            else:
                # Handle primitive types or other fields
                args[key] = value
        elif hasattr(_type, "__origin__") and _type.__origin__ is Union and type(None) in _type.__args__:
            # If the field is Optional, allow None
            args[key] = None
        else:
            # Raise an error for missing required fields
            pprint(json_data)
            raise AttributeError(f"'{key}' is required but not in the input JSON.")

    return dataclass_type(**args)


@dataclass
class TagAttributes:
    name: LocalizedString
    description: LocalizedString
    group: str
    version: int


@dataclass
class Relationship:
    id: str
    type: str
    related: Optional[str]
    attributes: Optional[Dict]


@dataclass
class Tag:
    id: str
    type: str
    attributes: TagAttributes
    relationships: List[Relationship]


@dataclass
class ChapterAttributes:
    title: Optional[str]
    volume: Optional[str]
    chapter: Optional[str]
    pages: int
    translatedLanguage: str
    uploader: Optional[str]  # FIXME: API Mismatch this shouldn't be Optional but some chapter dont have uploader
    externalUrl: Optional[str]
    version: int
    createdAt: str
    updatedAt: str
    publishAt: str
    readableAt: str


@dataclass
class Chapter:
    id: str
    type: str
    attributes: ChapterAttributes
    relationships: List[Relationship]


@dataclass
class DirectSearchChapter:
    result: str
    response: str
    data: Chapter


@dataclass
class ChapterList:
    result: str
    response: str
    data: List[Chapter]
    limit: int
    offset: int
    total: int


@dataclass
class MangaAttributes:
    title: LocalizedString
    altTitles: List[LocalizedString]
    description: LocalizedString
    isLocked: bool
    links: Links
    originalLanguage: LocalizedString
    lastVolume: Optional[str]
    lastChapter: Optional[str]
    publicationDemographic: Optional[str]
    status: Optional[str]
    year: Optional[int]
    contentRating: str
    chapterNumbersResetOnNewVolume: bool
    availableTranslatedLanguages: List[str]
    latestUploadedChapter: str
    tags: List[Tag]
    state: str
    version: int
    createdAt: str
    updatedAt: str


@dataclass
class Manga:
    id: str
    type: str
    attributes: MangaAttributes
    relationships: List[Relationship]


@dataclass
class DirectSearchManga:
    result: str
    response: str
    data: Manga


@dataclass
class MangaList:
    result: str
    response: str
    data: List[Manga]
    limit: int
    offset: int
    total: int


@dataclass
class CustomListAttributes:
    name: str
    visibility: Union[str]
    version: int


@dataclass
class CustomList:
    id: str
    type: str
    attributes: CustomListAttributes
    relationships: List[Relationship]


@dataclass
class CustomListResponse:
    result: str
    response: str
    data: List[CustomList]


# Custom schema


# Latest chapter per downloaded manga
# all mangas are checked against this to see if there was new release
@dataclass
class LatestChapter:
    muuid: str
    latestChapter: str
    createdAt: str
    updatedAt: str
    # Maybe dont need this
    volume: Optional[str]
    chapter: Optional[str]
