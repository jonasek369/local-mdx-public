import base64
import json
import os
from dataclasses import asdict

from werkzeug.exceptions import UnsupportedMediaType

from rewrite.backend.connection import MangaDownloadJob, DownloaderState, save_credentials
from rewrite.backend.repository import MangaRepository
from rewrite.backend.settings import credentials_from_json
from rewrite.backend.utils import is_valid_uuid, info, error

try:
    import webview
except ImportError:
    class wv:
        def __init__(self):
            self.windows = []
            self.token = -1


    webview = wv()

from flask import Flask, jsonify, render_template, request, make_response

from gzip import compress

gui_dir = os.path.join(os.getcwd(), 'gui')

repository = MangaRepository()

repository.settings.logger.log(info, gui_dir + " is static and template dir!")

server = Flask(__name__, static_folder=gui_dir, template_folder=gui_dir)


@server.route("/")
def landing():
    return render_template("index.html")


@server.route('/search/manga', methods=['POST'])
def search():
    data = request.json
    limit = 5
    if request.args.get("limit") and request.args.get("limit").isdigit():
        try:
            limit = int(request.args.get("limit"))
        except ValueError:
            pass

    if limit > 50:
        return "cannot search that much"

    result = repository.connection.search_manga(data["searchTerm"], limit=limit)

    return jsonify([asdict(i) for i in result.data])


@server.route("/manga/cover/<identifier>")
def get_cover_art(identifier):
    small = request.args.get('small', 0)
    if small and small.isdigit():
        small = int(small)
    image_binary = repository.get_cover_art(identifier, bool(small))
    if image_binary is not None:
        response = make_response(compress(image_binary))
        response.headers.set('Content-Type', 'image/jpeg')
        response.headers.set('Content-Disposition', 'inline', filename=f'{identifier}.jpg')
        response.headers.set("Content-Encoding", "gzip")
        return response
    return "Error: no image found"


@server.route("/manga/<mangauuid>", methods=["GET"])
def server_manga(mangauuid):
    back = request.args.get('from', "/")

    manga = repository.get_manga_attributes(mangauuid)
    return render_template("manga.html",
                           muuid=mangauuid,
                           name=manga.title["en"],
                           description=manga.description["en"],
                           back_redirect=back)


@server.route("/manga/<mangauuid>/info", methods=["GET"])
def get_manga_info(mangauuid):
    downloaded_pages = repository.get_downloaded_pages(mangauuid)
    chapters = {}
    for cuuid, title, volume, chapter in downloaded_pages:
        chapters[cuuid] = {"title": title, "volume": volume, "chapter": chapter}
    return jsonify(chapters)


@server.route("/page-image/<identifier>/<page>")
def get_chapter_image(identifier, page):
    if not page.isdigit():
        return "Error: page is not an number"
    image_binary = repository.database.get_page(identifier, int(page))
    if image_binary is not None:
        response = make_response(compress(image_binary))
        response.headers.set('Content-Type', 'image/jpeg')
        response.headers.set('Content-Disposition', 'inline', filename=f'{identifier}-{page}.png')
        response.headers.set("Content-Encoding", "gzip")
        return response
    return "Error: no image found"


@server.route("/page-images/<identifier>/")
def get_chapter_images(identifier):
    image_binary = repository.database.get_pages(identifier)
    if image_binary is not None:
        data = {}
        for page, image in image_binary:
            encoded_image = base64.b64encode(image).decode('utf-8')
            data[page] = encoded_image
        return jsonify(data)
    return "Error: no image found", 404


@server.route("/read/next-prev/<chapteruuid>")
def chapter_next_previous(chapteruuid):
    next_prev_tuple = repository.get_next_prev(chapteruuid)
    if next_prev_tuple:
        next_prev = {"next": next_prev_tuple[0], "prev": next_prev_tuple[1]}
    else:
        next_prev = {"next": None, "prev": None}
    return jsonify(next_prev)


@server.route("/manga/download/push-job", methods=["POST"])
def push_job():
    data = request.json
    if "id" not in data:
        return {"status": "error"}
    if not is_valid_uuid(data.get("id")):
        return {"status": "error"}
    repository.downloader.queue.add_job(
        MangaDownloadJob(
            data.get("id"),
            repository.get_manga_attributes(data.get("id")),
            repository.get_chapter_list(data.get("id")),
            repository.database.get_manga_job(data.get("id"))
        )
    )
    return {"status": "success"}


@server.route("/read/<chapteruuid>", methods=["GET"], defaults={"page": 1})
@server.route("/read/<chapteruuid>/<page>", methods=["GET"])
def read_manga(chapteruuid, page):
    from_end = request.args.get('end', None)
    ids = {}
    # ids are passed and filled with data in the functions
    attributes = repository.get_chapter_attributes(chapteruuid, ids)
    if not attributes:
        return {"error": "Data not in database"}

    if from_end:
        page = attributes.pages

    return render_template("read.html",
                           cuuid=chapteruuid,
                           pages=attributes.pages,
                           muuid=ids["muuid"],
                           page=page,
                           page_render="NORMAL"  # TODO: Add logic for long strips when reader supports it
                           )


@server.route("/manga/download/manager", methods=["GET"])
def download_manager():
    return render_template("download-manager.html")


@server.route("/manga/download/start", methods=["GET"])
def start_download():
    repository.downloader.start()
    return {"status": "success"}


@server.route("/manga/download/stop", methods=["GET"])
def stop_download():
    repository.downloader.stop()
    return {"status": "success"}


@server.route("/manga/download/queue", methods=["GET"])
def get_queue():
    queue = {}

    for idx, job in enumerate(repository.downloader.queue):
        if (repository.downloader.currently_working_on is not None
                and job.identifier == repository.downloader.currently_working_on["id"]
                and repository.downloader.state != DownloaderState.Off):
            continue
        queue[idx] = {
            "name": job.title
        }

    return {"queue": queue}


@server.route("/manga/download/status")
def download_status():
    cwo = repository.downloader.currently_working_on
    if cwo is None:
        return {"status": {},
                "in_progress": repository.downloader.state != DownloaderState.Off,
                "speed_mode": repository.downloader.speed}

    return {"status": {
        cwo["id"]: {"name": cwo["title"], "page_status": cwo["page_status"], "chapter_status": cwo["chapter_status"]}},
        "in_progress": repository.downloader.state != DownloaderState.Off,
        "speed_mode": repository.downloader.speed}


@server.route("/manga/download/push-to-top", methods=["POST"])
def push_to_top():
    data = request.json
    if "index" not in data:
        return "Error: no index in json"

    repository.downloader.queue.push_to_top(int(data["index"]))

    return {"status": "success"}


@server.route("/manga/download/pop-job", methods=["POST"])
def pop_job():
    data = request.json
    if "index" not in data:
        return "Error: no index in json"

    repository.downloader.queue.remove_job(
        repository.downloader.queue.pop_job_index(int(data["index"])).identifier
    )
    return {"status": "success"}


@server.route("/manga/download/speed", methods=["POST"])
def set_speed():
    data = request.json
    if "speed" not in data or data["speed"] not in ["SLOW", "NORMAL", "FAST", "NO_LIMIT"]:
        return {"status": "error"}
    repository.downloader.speed = data["speed"]

    return {"status": "success"}


@server.route("/manga/library/data", methods=["GET"])
def library_data():
    to_send = {}
    mangas = repository.database.all_manga_in_db()
    for manga in mangas:
        pages = repository.database.get_downloaded_pages(manga[0])
        if pages:
            to_send[manga[0]] = [json.loads(manga[1])["en"], json.loads(manga[2])["en"]]

    repository.sync_libraries(list(to_send.keys()))
    return {"status": "ok", "response": to_send}


@server.route("/manga/library", methods=["GET"])
def library():
    return render_template("library.html")


@server.route("/popular-new-titles", methods=["GET"])
def popular_new_titles():
    return repository.popular_new_titles()


@server.route("/auth/check")
def check_auth():
    token = repository.credential_manager.token
    if repository.credential_manager.validate_token(token):
        status = "ok"
    else:
        status = "error"
    return {"auth_status": status}


@server.route("/auth")
def authorize():
    return render_template("auth.html")


@server.route("/auth/set-credentials", methods=["POST"])
def set_credentials():
    try:
        data = request.json
    except UnsupportedMediaType:
        return {"status": "error", "response": "Endpoint requires json"}
    credentials = credentials_from_json(data)
    repository.credential_manager.set_credentials(credentials)
    if credentials.is_valid():
        save_credentials(credentials)
        return {"status": "ok", "response": "credentials set"}
    return {"status": "error", "response": "invalid credentials"}


@server.route("/updates")
def updates():
    auth = check_auth()
    if auth["auth_status"] != "ok":
        return {"status": "error", "response": "Credentials are not set properly. Updates require them"}
    return render_template("updates.html")


@server.route("/updates/data")
def updates_data():
    limit = request.args.get('limit', 32)
    offset = request.args.get('offset', 0)
    _updates = repository.get_updates(limit, offset)
    if not _updates:
        repository.settings.logger.log(error, "coudnt get updates")
        return {"status": "error"}
    return asdict(_updates)

if __name__ == "__main__":
    USE_SERVER = 0
    if not USE_SERVER:
        server.run(host="127.0.0.1", port=5000, threaded=False)
    else:
        print("starting server")
        # testing performance on other devices
        from waitress import serve

        serve(server, listen="127.0.0.1:5000")
