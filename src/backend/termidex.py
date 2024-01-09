import curses
import json
import re
import string
import sys
import threading
import time
from dataclasses import dataclass
from typing import List, Callable, Optional
import os
from gzip import decompress
from uuid import UUID

import matplotlib.pyplot as plt
from io import BytesIO

from matplotlib import image as mpimg

os.environ["stop_cache_start"] = "0"

from app import MangaDownloadJob, Database, MangaDownloader, MangadexConnection, cache

database = Database()
connection = MangadexConnection(database)
downloader = MangaDownloader(connection, database)


def is_valid_uuid(uuid_to_test, version=4):
    try:
        uuid_obj = UUID(uuid_to_test, version=version)
    except ValueError:
        return False
    return str(uuid_obj) == uuid_to_test


class vec2:
    def __init__(self, x, y):
        self.x = x
        self.y = y

    def __eq__(self, other):
        return self.x == other.x and self.y == other.y

    def __lt__(self, other):
        return self.x < other.x and self.y < other.y

    def __le__(self, other):
        return self.x <= other.x and self.y <= other.y

    def __repr__(self):
        return f"{self.x}x {self.y}y"


class ChoiceCursor:
    def __init__(self, max_x: vec2, max_y: vec2, on_choice: Callable = None):
        # ranges
        self.xmax: vec2 = max_x
        self.ymax: vec2 = max_y

        self.current = vec2(self.xmax.x, self.ymax.x)
        self.on_choice = on_choice

    def set_callback(self, cb: Callable):
        self.on_choice = cb

    def reset(self):
        self.current = vec2(self.xmax.x, self.ymax.x)

    def handle_key(self, key, termidex_instance):
        termidex_instance.stdscr.clear()

        if (key == ord("\n")) and self.on_choice:
            self.on_choice(self.current, termidex_instance)

        if (key == ord("s") or key == curses.KEY_DOWN) and self.current.y + 1 <= self.ymax.y:
            self.current.y += 1

        if (key == ord("w") or key == curses.KEY_UP) and self.current.y - 1 >= self.ymax.x:
            self.current.y -= 1

        if (key == ord("d") or key == curses.KEY_RIGHT) and self.current.x + 1 <= self.xmax.y:
            self.current.x += 1

        if (key == ord("a") or key == curses.KEY_LEFT) and self.current.x - 1 >= self.xmax.x:
            self.current.x -= 1


@dataclass
class Page:
    id: str
    name: str
    data: dict


MENU_PAGE = Page("1", "Menu", {})
LIBRARY_PAGE = Page("2", "Library", {
    "choice_cursor": ChoiceCursor(vec2(0, 0), vec2(0, max(len(database.manga_in_db()) - 1, 0))),
})
DOWNLOADER_PAGE = Page("3", "Downloader", {})

LIBRARY_OFFSET = 2
MANGA_OFFSET = 2
MANGA_LABEL_OFFSET = 0
DOWNLOADER_OFFSET = 0
DOWNLOADER_BAR_NUM = 20
SEARCH_LABEL_OFFSET = 0
SEARCH_OFFSET = 2

KEY_ESC = 27
KEY_TAB = 9
KEY_BACKSPACE = 8

MAX_QUERY_CHARACTERS = 128

COLOR_FGRAY_BWHITE = 1
COLOR_FRED_BWHITE = 2
COLOR_FLGREEN_BWHITE = 3
COLOR_FBLUE_BWHITE = 4
COLOR_FGRAY_BBLACK = 5

SEARCH_MANGA_OPTIONS = 1

commands = {
    "menu": {
        "name": "menu",
        "params": []
    },
    "library": {
        "name": "library",
        "params": []
    },
    "downloader": {
        "name": "downloader",
        "params": []
    },
    "search": {
        "name": "search",
        "params": [
            {
                "name": "query",
                "abbreviates": [
                    "q"
                ],
                "value_type": "string",
                "default": None,
                "validator": lambda x: isinstance(x, str) and len(x) <= MAX_QUERY_CHARACTERS
            },
            {
                "name": "limit",
                "abbreviates": [
                    "lim",
                    "l"
                ],
                "value_type": "integer",
                "default": 5,
                "validator": lambda x: 0 < x <= 100,
            }
        ]
    },
    "back": {
        "name": "back",
        "params": []
    },
    "download": {
        "name": "download",
        "params": [
            {
                "name": "id",
                "abbreviates": [
                    "muuid", "uuid"
                ],
                "value_type": "string",
                "default": None,
                "validator": lambda x: is_valid_uuid(x)
            }
        ]
    }
}


def parse_value(value, token_info):
    if token_info['value_type'] == 'string':
        return value.strip('"')
    elif token_info['value_type'] == 'integer':
        return int(value)


def parse_buffer(buff, config):
    # Define regular expression for matching key-value pairs
    key_value_pattern = re.compile(r'(\w+)\s*=\s*(".*?"|\w+)')

    # Split buffer into key-value pairs
    key_value_pairs = re.findall(key_value_pattern, buff)

    # Create a dictionary to store parsed tokens
    parsed_tokens = {}

    # Iterate through key-value pairs and extract tokens
    for key, value in key_value_pairs:
        for param in config.get("params", []):
            if key == param["name"] or key in param.get("abbreviates", []):
                parsed_value = parse_value(value, param)
                parsed_tokens[param['name']] = (
                    parsed_value, param["value_type"], param["validator"](parsed_value), key)

    return parsed_tokens


def get_param_by_name(command_name, param_name):
    for param in commands[command_name]["params"]:
        if param["name"] == param_name:
            return param


class Termidex:
    def __init__(self):
        self.stdscr = None
        self.page: Page = MENU_PAGE
        self.last_page: Optional[Page] = None

    def set_page(self, new_page: Page):
        self.last_page = self.page
        self.page = new_page

    def open_cmd_overlay(self):
        buffer = []
        while True:
            finish = None
            command_in_use = None
            param_in_use = None
            parsed_params = {}

            # handle key event
            key = self.stdscr.getch()

            if key == KEY_ESC:
                return
            if key == KEY_BACKSPACE:
                buffer = buffer[:-1]

            if key != -1:
                key = chr(key)
            else:
                key = "\0"
            if key == "\n":
                return buffer
            if key in list(string.ascii_letters + string.digits + string.punctuation + " "):
                buffer.append(key)

            str_buffer = "".join(buffer)

            command_value = str_buffer.split(" ")[0] if " " in str_buffer else str_buffer

            for command in commands.keys():
                if str(command).startswith(str_buffer.lower()) and 0 < len(buffer) < len(str(command)):
                    finish = command[len(buffer):]
                if command_value == str(command):
                    command_in_use = str(command)

            if command_in_use:
                parsed_params = parse_buffer(str_buffer, commands[command_in_use])

            if not finish and command_in_use is not None:
                for param in commands[command_in_use]["params"]:
                    if param["name"] in parsed_params.keys():
                        continue
                    param_name = str(param["name"])
                    param_buffer = str_buffer.split(" ")[-1].lower()
                    if param_name.startswith(param_buffer):
                        finish = param_name[len(param_buffer):]
                        param_in_use = param_name

            if param_in_use and commands[command_in_use]["params"]:
                param = get_param_by_name(command_in_use, param_in_use)
                if param["default"] is not None:
                    finish = finish + f"={param['default']}"
                else:
                    if param["value_type"] == "string":
                        finish = finish + '=""'

            if key == chr(KEY_TAB) and finish:
                buffer.extend(list(finish))

            rows, cols = self.stdscr.getmaxyx()
            cmd_promp = str_buffer + " " * (cols - len(buffer) - 1)

            self.stdscr.addstr(rows - 1, 0, cmd_promp, curses.A_REVERSE)

            if finish:
                written_in_cmd_overlay = 0
                self.stdscr.addstr(rows - 1, written_in_cmd_overlay, str_buffer, curses.A_REVERSE)
                written_in_cmd_overlay += len(buffer)
                self.stdscr.addstr(rows - 1, written_in_cmd_overlay, finish, curses.color_pair(COLOR_FGRAY_BWHITE))
                written_in_cmd_overlay += len(finish)
            else:
                if command_in_use:
                    for key, value in parsed_params.items():
                        param_value, param_type, valid, key_used = value
                        start = str_buffer.find(key_used + "=")
                        param_value_start = str_buffer.find(str(param_value), start)
                        if not valid:
                            self.stdscr.addstr(rows - 1, param_value_start, str(param_value),
                                               curses.color_pair(COLOR_FRED_BWHITE))
                        elif param_type == "string":
                            qouted_buff = str_buffer[
                                          str_buffer.find(param_value) - 1: str_buffer.find(param_value) + len(
                                              param_value) + 1]
                            self.stdscr.addstr(rows - 1, param_value_start - 1, qouted_buff,
                                               curses.color_pair(COLOR_FLGREEN_BWHITE))
                        elif param_type == "integer":
                            self.stdscr.addstr(rows - 1, param_value_start, str(param_value),
                                               curses.color_pair(COLOR_FBLUE_BWHITE))
            # Set cursor to where the user is typing
            self.stdscr.addstr(rows - 1, len(buffer), "")

    def main(self, stdscr):
        if not self.stdscr and stdscr:
            self.stdscr = stdscr
        self.stdscr.nodelay(True)

        curses.start_color()
        curses.use_default_colors()

        curses.init_pair(COLOR_FGRAY_BWHITE, 8, 7)
        curses.init_pair(COLOR_FRED_BWHITE, 4, 7)
        curses.init_pair(COLOR_FLGREEN_BWHITE, 10, 7)
        curses.init_pair(COLOR_FBLUE_BWHITE, 1, 7)
        curses.init_pair(COLOR_FGRAY_BBLACK, 8, 0)

        last = vec2(0, 0)
        last_page = self.page
        while True:
            try:
                key = self.stdscr.getch()
                y, x = self.stdscr.getmaxyx()
                if vec2(y, x) != last:
                    self.stdscr.clear()
                    last = vec2(y, x)
                if self.page != last_page:
                    self.stdscr.clear()
                    last_page = self.page
                cmd_buffer = None

                if key == KEY_ESC:
                    cmd_buffer = self.open_cmd_overlay()
                    if cmd_buffer:
                        do_command(cmd_buffer, self)
                    self.stdscr.clear()

                if self.page == MENU_PAGE:
                    self.stdscr.addstr(0, 0, "Welcome to ")
                    self.stdscr.addstr(0, 11, "Termidex", curses.A_BOLD)

                if self.page == DOWNLOADER_PAGE:
                    self.stdscr.clear()
                    self.stdscr.addstr(DOWNLOADER_OFFSET, 0, "Downloader")
                    self.stdscr.addstr(DOWNLOADER_OFFSET + 2, 0, "currently downloading:")
                    if downloader.currently_working_on is not None:
                        self.stdscr.addstr(DOWNLOADER_OFFSET + 3, 2, downloader.currently_working_on["name"] + "> ")
                        # percentage of download
                        percentage = round(downloader.currently_working_on["chapter_status"][0] / (
                                downloader.currently_working_on["chapter_status"][1] / 100))
                        bars_num = int((percentage / 100) * DOWNLOADER_BAR_NUM)
                        payload = f"{percentage}% |{('#' * bars_num).ljust(20)}| {downloader.currently_working_on['chapter_status'][0]}/{downloader.currently_working_on['chapter_status'][1]}"

                        self.stdscr.addstr(DOWNLOADER_OFFSET + 3, len(downloader.currently_working_on["name"]) + 4,
                                           payload)
                    else:
                        print("empty ?", downloader.currently_working_on)
                        self.stdscr.addstr(DOWNLOADER_OFFSET + 3, 2, "Empty")
                    self.stdscr.addstr(DOWNLOADER_OFFSET + 4, 0, "Queue:")
                    if downloader.queue.first is None or (
                            downloader.currently_working_on is not None and
                            downloader.queue.first.id == downloader.currently_working_on["id"] and len(
                        downloader.queue) == 1):
                        self.stdscr.addstr(DOWNLOADER_OFFSET + 5, 2, "Empty")
                    else:
                        written = 1
                        for job in downloader.queue:
                            if downloader.currently_working_on and downloader.currently_working_on["id"] == job.id:
                                continue
                            self.stdscr.addstr(DOWNLOADER_OFFSET + 4 + written, 2, job.name)
                            written += 1

                if self.page == LIBRARY_PAGE:
                    # handle key can change termidex page, so we check it if it changed
                    self.page.data["choice_cursor"].handle_key(key, self)
                    if self.page != LIBRARY_PAGE:
                        continue

                    self.stdscr.addstr(0, 0, "Library")

                    at_char = 0
                    for index, manga in enumerate(database.manga_in_db()):
                        attr = 0
                        if self.page.data["choice_cursor"].current.y == index:
                            attr = curses.A_BOLD
                        else:
                            attr = curses.color_pair(COLOR_FGRAY_BBLACK)
                        identifier, name, description = manga
                        corrected_name = name
                        if len(corrected_name) > x:
                            corrected_name = corrected_name[:x - 4] + "..."

                        if attr == curses.A_BOLD:
                            at_char = len(corrected_name) if x > len(corrected_name) else len(corrected_name) - 1

                        self.stdscr.addstr(index + LIBRARY_OFFSET, 0, corrected_name, attr)
                    self.stdscr.addstr(self.page.data["choice_cursor"].current.y + LIBRARY_OFFSET, at_char, "")

                if self.page.name == "Manga":
                    self.page.data["choice_cursor"].handle_key(key, self)
                    if self.page.name != "Manga":
                        continue
                    self.stdscr.addstr(MANGA_LABEL_OFFSET, 0, self.page.data["manga_data"][1])

                    on_screen_written_y = MANGA_OFFSET

                    self.page.data["scroll"] = self.page.data["choice_cursor"].current.y - (y - MANGA_OFFSET)
                    at_char = [0, 0]
                    for index, chapter in enumerate(self.page.data["chapters"].items()):
                        cuuid, data = chapter
                        if not on_screen_written_y >= y and self.page.data["scroll"] < index:
                            if self.page.data["choice_cursor"].current.y == index:
                                attr = curses.A_BOLD
                            else:
                                attr = curses.color_pair(COLOR_FGRAY_BBLACK)

                            title = f"{data['volume']} Volume {data['chapter']} Chapter"

                            if attr == curses.A_BOLD:
                                at_char = [len(title) if x > len(title) else len(title) - 1, on_screen_written_y]

                            self.stdscr.addstr(on_screen_written_y, 0, title, attr)
                            on_screen_written_y += 1
                        self.stdscr.addstr(at_char[1], at_char[0], "")

                if self.page.name == "Search":
                    self.page.data["choice_cursor"].handle_key(key, self)
                    if self.page.name != "Search":
                        continue
                    self.stdscr.addstr(SEARCH_LABEL_OFFSET, 0,
                                       f"Search result for: '{self.page.data['query']}' with limit={self.page.data['limit']}")

                    on_screen_written_y = SEARCH_OFFSET

                    self.page.data["scroll"] = self.page.data["choice_cursor"].current.y - (y - SEARCH_OFFSET)
                    at_char = [0, 0]
                    for index, manga in enumerate(self.page.data["result"]):
                        if not on_screen_written_y >= y and self.page.data["scroll"] < index:
                            if self.page.data["choice_cursor"].current.y == index:
                                attr = curses.A_BOLD
                            else:
                                attr = curses.color_pair(COLOR_FGRAY_BBLACK)

                            title = manga["title"]

                            if attr == curses.A_BOLD:
                                at_char = [len(title) if x > len(title) else len(title) - 1, on_screen_written_y]

                            self.stdscr.addstr(on_screen_written_y, 0, title, attr)
                            on_screen_written_y += 1
                        self.stdscr.addstr(at_char[1], at_char[0], "")

                if self.page.name == "OpenSearchManga":
                    self.stdscr.clear()
                    self.page.data["choice_cursor"].handle_key(key, self)

                    if self.page.name != "OpenSearchManga":
                        continue

                    self.stdscr.addstr(0, 0, self.page.data["title"])

                    for index, tag in enumerate(["Download", "Back"]):
                        attr = 0
                        if self.page.data["choice_cursor"].current.y == index:
                            attr = curses.A_BOLD
                        else:
                            attr = curses.color_pair(COLOR_FGRAY_BBLACK)
                        self.stdscr.addstr(index + 2, 0, tag, attr)

                if self.page.name == "MangaReader":
                    #TODO: Make the image viewer decent
                    self.stdscr.clear()
                    if not self.page.data["is_displayed"]:
                        page_img = None
                        for page, image in self.page.data["chapter_pages"]:
                            if page == self.page.data["at_page"]:
                                page_img = image
                        assert page_img is not None, f"Could not find image with page {self.page.data['at_page']}"
                        fig, ax = plt.subplots()
                        ax.axis('off')  # Turn off the axes

                        # Prototype of showing the image maybe experiment with different libraries
                        image = mpimg.imread(BytesIO(page_img), format="jpeg")

                        # Display the image
                        plt.imshow(image)
                        plt.get_current_fig_manager().toolbar_visible = False


                        plt.show()

                        self.page.data["is_displayed"] = True

            except KeyboardInterrupt:
                break


def callback_read_manga(position: vec2, termidex_instance: Termidex):
    for index, chapter in enumerate(termidex_instance.page.data["chapters"].items()):
        if index != position.y:
            continue
        cuid, chapter_data = chapter
        muid = termidex_instance.page.id
        termidex_instance.set_page(Page(cuid, "MangaReader", {
            "manga_uuid": muid,
            "chapter_uuid": cuid,
            "chapter_data": chapter_data,
            "at_page": 1,
            "is_displayed": False,
            "chapter_pages": database.get_chapter_images(cuid)
        }))


def callback_open_manga(position: vec2, termidex_instance: Termidex):
    db_mangas = database.manga_in_db()
    if not db_mangas:
        return None
    selected_manga = db_mangas[position.y]
    assert selected_manga is not None, "Could not get manga from database"
    if termidex_instance.page.name == "Manga":
        termidex_instance.page.data = {}
    termidex_instance.set_page(Page(selected_manga[0], "Manga", {}))

    if termidex_instance.page.data.get("chapters") is None:
        termidex_instance.page.data["chapters"] = \
            database.get_chapters(json.loads(decompress(database.get_chapter_info(termidex_instance.page.id)[1])))[0]

    termidex_instance.page.data["choice_cursor"] = ChoiceCursor(vec2(0, 0),
                                                                vec2(0,
                                                                     max(len(
                                                                         termidex_instance.page.data["chapters"]) - 1,
                                                                         0)))
    termidex_instance.page.data["scroll"] = 0
    termidex_instance.page.data["manga_data"] = database.get_info(termidex_instance.page.id)
    termidex_instance.page.data["choice_cursor"].set_callback(callback_read_manga)
    termidex_instance.stdscr.clear()


def download_manga(position: vec2, termidex_instance: Termidex):
    if position.y == 0:
        do_command(list(f'download id="{termidex_instance.page.id}"'), termidex_instance)
    do_command(list("back"), termidex_instance)


def callback_open_manga_search(position: vec2, termidex_instance: Termidex):
    opened_manga = termidex_instance.page.data["result"][position.y]
    termidex_instance.set_page(Page(id=opened_manga["id"], name="OpenSearchManga", data={
        "title": opened_manga["title"],
        "choice_cursor": ChoiceCursor(vec2(0, 0), vec2(0, 1))
    }))
    termidex_instance.page.data["choice_cursor"].set_callback(download_manga)


LIBRARY_PAGE.data["choice_cursor"].set_callback(callback_open_manga)


def do_command(buffer: List[str], termidex_instance: Termidex) -> dict:
    str_buffer = "".join(buffer)
    command_in_use = None
    command_value = str_buffer.split(" ")[0] if " " in str_buffer else str_buffer

    for command in commands.keys():
        if command_value == str(command):
            command_in_use = str(command)

    if command_in_use:
        command_parameters = parse_buffer(str_buffer, commands[command_in_use])
    else:
        command_parameters = {}

    if command_in_use:
        command_in_use = command_in_use.lower()

    if command_in_use == "search" and "query" in command_parameters and command_parameters["query"][2]:
        if "limit" in command_parameters and command_parameters["limit"][2]:
            limit = command_parameters["limit"][0]
        else:
            limit = commands["search"]["params"][1]["default"]

        result = connection.search_manga(command_parameters["query"][0], limit=limit)
        termidex_instance.set_page(Page(command_parameters["query"][0], "Search", {
            "result": result,
            "query": command_parameters["query"][0],
            "limit": limit,
            "choice_cursor": ChoiceCursor(vec2(0, 0), vec2(0, max(len(result) - 1, 0))),
        }))
        termidex_instance.page.data["choice_cursor"].set_callback(callback_open_manga_search)

    if command_in_use == "back":
        # swap pages
        termidex_instance.set_page(termidex_instance.last_page)
        # termidex_instance.page, termidex_instance.last_page = termidex_instance.last_page, termidex_instance.page
    if command_in_use == "library":
        LIBRARY_PAGE.data["choice_cursor"].reset()
        termidex_instance.set_page(LIBRARY_PAGE)
    if command_in_use == "menu":
        termidex_instance.set_page(MENU_PAGE)
    if command_in_use == "download" and "id" in command_parameters and command_parameters["id"][2]:
        print("download", command_parameters["id"][0])
        info = database.get_info(command_parameters["id"][0])
        if info is None:
            connection.set_manga_info(command_parameters["id"][0])

        downloader.queue.add_job(
            MangaDownloadJob(_id=command_parameters["id"][0], connection=connection, database=database))
    if command_in_use == "downloader":
        termidex_instance.set_page(DOWNLOADER_PAGE)


def curses_keycode_test(stdscr):
    key = stdscr.getch()
    print(key)


# print(parse_buffer('search limit=32 query="this is test"', commands["search"]))

if __name__ == '__main__':
    termidex = Termidex()
    curses.wrapper(termidex.main)

    downloader.stop()
    cache.close()
    sys.exit(0)
