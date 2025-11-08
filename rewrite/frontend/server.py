import asyncio
import base64
import json
import os
from dataclasses import asdict

from flask_socketio import SocketIO
from sympy.assumptions import relation

from rewrite.backend.connection import MangaDownloadJob, save_credentials
from rewrite.backend.repository import MangaRepository
from rewrite.backend.schemas import COVER_ART_MAX_SIZE, COVER_ART_256_SIZE, COVER_ART_512_SIZE
from rewrite.backend.settings import credentials_from_json
from rewrite.backend.utils import info, error, get_correct_language, is_uuid4

try:
    import webview
except ImportError:
    class wv:
        def __init__(self):
            self.windows = []
            self.token = -1


    webview = wv()


from flask import Flask, jsonify, render_template, request, make_response

gui_dir = os.path.join(os.getcwd(), 'gui')

server = Flask(__name__, static_folder=gui_dir, template_folder=gui_dir)

socketio = SocketIO(server, async_mode='eventlet')

# Passing socketio so we can communicate with socket mainly from the downloader
repository = MangaRepository(socketio)

repository.settings.logger.log(info, gui_dir + " is static and template dir!")


@server.route("/")
def landing():
    return render_template("index.html", darktheme=repository.settings.darkTheme)


@server.route('/search/manga', methods=['POST'])
def search():
    try:
        request_data = request.get_json(force=True)
    except Exception as e:
        repository.settings.logger.log(error, f"Caught exception while search! {e}")
        return jsonify({"status": "error", "message": "Invalid JSON body"}), 400

    search_term = request_data.get("searchTerm")
    if not search_term:
        return jsonify({"status": "error", "message": "Missing or empty 'searchTerm'"}), 400

    limit_arg = request.args.get("limit", 5)
    try:
        limit = int(limit_arg)
    except ValueError:
        return jsonify({"status": "error", "message": "Invalid limit"}), 400

    if limit > 50:
        return jsonify({"status": "error", "message": "Limit too high (max 50)"}), 400

    try:
        result = repository.connection.search_manga(search_term, limit=limit)
        data = [asdict(i) for i in result.data]
        for manga in data:
            try:
                manga["attributes"]["title"] = get_correct_language(
                    manga["attributes"]["title"],
                    manga["attributes"]["altTitles"],
                    repository.settings
                )
                for i, tag in enumerate(manga["attributes"]["tags"]):
                    manga["attributes"]["tags"][i]["attributes"]["name"] = get_correct_language(
                        manga["attributes"]["tags"][i]["attributes"]["name"],
                        None,
                        repository.settings
                    )
            except KeyError as e:
                repository.settings.log(error, f"Caught exception while choosing search translation! {e}")
        return jsonify(data), 200
    except Exception as e:
        repository.settings.logger.log(error, f"Search failed: {e}")
        return jsonify({"status": "error", "message": "Internal server error"}), 500


@server.route("/manga/cover/<identifier>")
def get_cover_art(identifier):
    if not is_uuid4(identifier):
        return jsonify({"status": "error", "message": "Invalid identifier"}), 400

    try:
        size = int(request.args.get("size", COVER_ART_MAX_SIZE))
    except (TypeError, ValueError):
        size = COVER_ART_MAX_SIZE

    allowed_sizes = {COVER_ART_MAX_SIZE, COVER_ART_256_SIZE, COVER_ART_512_SIZE}
    if size not in allowed_sizes:
        size = COVER_ART_MAX_SIZE

    image_binary = repository.get_cover_art(identifier, size)
    if not image_binary:
        return jsonify({"error": "No image found"}), 404

    response = make_response(image_binary)
    response.headers.set("Content-Type", "image/jpeg")
    response.headers.set("Content-Disposition", f"inline; filename={identifier}.jpg")
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response, 200


@server.route("/manga/<mangauuid>", methods=["GET"])
def server_manga(mangauuid):
    back = request.args.get('from', "/")

    manga = repository.get_manga_attributes(mangauuid)
    if not manga:
        return jsonify({"error": "No manga found"}), 404

    return render_template("manga.html",
                           muuid=mangauuid,
                           name=get_correct_language(manga.title, manga.altTitles, repository.settings),
                           description=get_correct_language(manga.description, None, repository.settings),
                           back_redirect=back,
                           darktheme=repository.settings.darkTheme), 200


@server.route("/manga/<mangauuid>/info", methods=["GET"])
def get_manga_info(mangauuid):
    downloaded_pages = repository.get_downloaded_pages(mangauuid)
    user_and_groups = repository.database.get_user_and_groups([dp[0] for dp in downloaded_pages])

    chapters = {}

    for cuuid, title, volume, chapter in downloaded_pages:
        chapters[cuuid] = {
            "title": title,
            "volume": volume,
            "chapter": chapter,
            "user": user_and_groups[cuuid]["user"],
            "scanlation_group": user_and_groups[cuuid]["scanlation_group"]
        }

    return jsonify(chapters), 200


@server.route("/manga/<mangauuid>/attributes")
def manga_attributes(mangauuid):
    attributes = repository.get_manga_attributes(mangauuid)
    if not attributes:
        return {"status": "error", "response": "Could not fetch attributes"}, 500
    return jsonify(asdict(attributes)), 200


@server.route("/page-image/<identifier>/<page>")
def get_chapter_image(identifier, page):
    if not page.isdigit():
        return {"status": "error", "message": "page is not an number"}, 400
    image_binary = repository.database.get_page(identifier, int(page))
    if image_binary is not None:
        response = make_response(image_binary)
        response.headers.set('Content-Type', 'image/jpeg')
        response.headers.set('Content-Disposition', 'inline', filename=f'{identifier}-{page}.png')
        return response, 200
    return {"status": "error", "response": "no image found"}, 404


@server.route("/page-images/<identifier>/")
def get_chapter_images(identifier):
    image_binary = repository.database.get_pages(identifier)
    if image_binary is not None:
        data = {}
        for page, image in image_binary:
            encoded_image = base64.b64encode(image).decode('utf-8')
            data[page] = encoded_image
        return jsonify(data), 200
    return {"status": "error", "response": "no image found"}, 404


@server.route("/read/next-prev/<chapteruuid>")
def chapter_next_previous(chapteruuid):
    next_prev_tuple = repository.get_next_prev(chapteruuid)
    if next_prev_tuple:
        next_prev = {"next": next_prev_tuple[0], "prev": next_prev_tuple[1]}
    else:
        next_prev = {"next": None, "prev": None}
    return jsonify(next_prev), 200


@server.route("/read/<chapteruuid>", methods=["GET"], defaults={"page": 1})
@server.route("/read/<chapteruuid>/<page>", methods=["GET"])
def read_manga(chapteruuid, page):
    try:
        from_end = bool(request.args.get('end', False))
    except Exception:
        return {"status": "error", "response": "Missing or empty 'end'"}, 400
    ids = {}
    # ids are passed and filled with data in the functions
    attributes = repository.get_chapter_attributes(chapteruuid, ids)
    if not attributes:
        return {"status": "error", "response": "Data not in database"}

    if from_end:
        page = attributes.pages

    return render_template("read.html",
                           cuuid=chapteruuid,
                           pages=attributes.pages,
                           muuid=ids["muuid"],
                           page=page,
                           page_render="NORMAL"  # TODO: Add logic for long strips when reader supports it
                           ), 200


@server.route("/manga/download/push-job", methods=["POST"])
def push_job():
    try:
        data = request.get_json(force=True)
    except Exception as e:
        repository.settings.logger.log(error, f"Caught exception while search! {e}")
        return {"status": "error", "response": "Invalid JSON body"}, 400

    if "id" not in data:
        return {"status": "error", "response": "id not in JSON body"}, 400
    if not is_uuid4(data.get("id")):
        return {"status": "error", "response": "invalid id"}, 400
    socketio.emit("update", repository.downloader.get_downloader_state())
    repository.downloader.queue.add_job(
        MangaDownloadJob(
            data.get("id"),
            repository.get_manga_attributes(data.get("id")),
            repository.get_chapter_list(data.get("id")),
            repository.database.get_manga_job(data.get("id")),
            repository.settings
        )
    )
    return {"status": "success"}

@server.route("/manga/download/contains", methods=["POST"])
def downloader_contains():
    try:
        data = request.get_json(force=True)
    except Exception as e:
        repository.settings.logger.log(error, f"Caught exception while search! {e}")
        return {"status": "error", "response": "Invalid JSON body"}, 400
    if "id" not in data:
        return {"status": "error", "response": "id not in JSON body"}, 400
    if not is_uuid4(data.get("id")):
        return {"status": "error", "response": "invalid id"}, 400

    muuid = data.get("id")

    dl_state = repository.downloader.get_downloader_state()
    if dl_state["currently_working_on"] is not None:
        contains = muuid in dl_state["queue"] or dl_state["currently_working_on"]["id"] == dl_state
    else:
        contains = muuid in dl_state["queue"]

    return {"status": "success", "data": {"contains": contains}}, 200


@server.route("/manga/download/manager", methods=["GET"])
def download_manager():
    return render_template("download-manager.html", darktheme=repository.settings.darkTheme), 200


@server.route("/manga/download/start", methods=["GET"])
def start_download():
    repository.downloader.start()
    socketio.emit("update", repository.downloader.get_downloader_state())
    return {"status": "success", "response": "started downloader"}, 200


@server.route("/manga/download/stop", methods=["GET"])
def stop_download():
    repository.downloader.stop()
    socketio.emit("update", repository.downloader.get_downloader_state())
    return {"status": "success", "response": "stopped downloader"}, 200


@server.route("/manga/download/push-to-top", methods=["POST"])
def push_to_top():
    try:
        data = request.get_json(force=True)
    except Exception:
        return {"status": "error", "response": "no JSON provided"}, 400
    if "index" not in data:
        return {"status": "error", "response": "Index not in JSON"}, 400
    repository.downloader.queue.push_to_top(int(data["index"]))
    socketio.emit("update", repository.downloader.get_downloader_state())
    return {"status": "success", "response": "pushed job to top"}, 200


@server.route("/manga/download/pop-job", methods=["POST"])
def pop_job():
    try:
        data = request.get_json(force=True)
    except Exception:
        return {"status": "error", "response": "no JSON provided"}, 400
    if "index" not in data:
        return {"status": "error", "response": "Index not in JSON"}, 400

    repository.downloader.queue.remove_job(
        repository.downloader.queue.pop_job_index(int(data["index"])).identifier
    )
    socketio.emit("update", repository.downloader.get_downloader_state())
    return {"status": "success", "response": "Removed job"}, 200


@server.route("/manga/download/speed", methods=["POST"])
def set_speed():
    try:
        data = request.get_json(force=True)
    except Exception:
        return {"status": "error", "response": "error no json provided"}, 400
    if "speed" not in data:
        return {"status": "error", "response": "Index not in json"}, 400
    if data["speed"] not in {"SLOW", "NORMAL", "FAST", "NO_LIMIT"}:
        return {"status": "error", "response": "Incorrect speed"}, 400
    repository.downloader.speed = data["speed"]
    socketio.emit("update", repository.downloader.get_downloader_state())
    return {"status": "success", "response": "ok"}, 200


@server.route("/manga/library/data", methods=["GET"])
def library_data():
    to_send = {}
    mangas = repository.database.all_manga_in_db()
    if not mangas:
        return {"status": "success", "response": to_send}, 200
    for manga in mangas:
        pages = repository.database.get_downloaded_pages(manga[0])
        if pages:
            to_send[manga[0]] = [
                get_correct_language(json.loads(manga[1]), json.loads(manga[2]), repository.settings),
                get_correct_language(json.loads(manga[3]), None, repository.settings)
            ]

    repository.sync_libraries(list(to_send.keys()))
    return {"status": "success", "response": to_send}


@server.route("/manga/library", methods=["GET"])
def library():
    return render_template("library.html", darktheme=repository.settings.darkTheme)


@server.route("/popular-new-titles", methods=["GET"])
def popular_new_titles():
    popular = asyncio.run(repository.popular_new_titles())
    new_popular = []
    for manga in popular.data:
        if not manga:
            continue
        new_manga = asdict(manga)
        attrs = new_manga["attributes"]
        attrs["title"] = get_correct_language(manga.attributes.title, manga.attributes.altTitles, repository.settings)
        attrs["description"] = get_correct_language(manga.attributes.description, None, repository.settings)
        for tag in attrs["tags"]:
            tag["attributes"]["name"] = get_correct_language(tag["attributes"]["name"], None, repository.settings)

        new_popular.append(new_manga)
    return jsonify(new_popular), 200


@server.route("/auth/check")
def check_auth():
    token = repository.credential_manager.token
    if repository.credential_manager.validate_token(token):
        status = "ok"
        code = 200
    else:
        status = "error"
        code = 401
    return {"status": status}, code


@server.route("/auth")
def authorize():
    back = request.args.get('from', "/")
    return render_template("auth.html", back_redirect=back, darktheme=repository.settings.darkTheme), 200


@server.route("/auth/set-credentials", methods=["POST"])
def set_credentials():
    try:
        data = request.get_json(force=True)
    except Exception:
        return {"status": "error", "response": "Endpoint requires json"}, 400
    credentials = credentials_from_json(data)
    repository.credential_manager.set_credentials(credentials)
    if repository.credential_manager.validate_token(repository.credential_manager.token):
        save_credentials(credentials)
        return {"status": "ok", "response": "credentials set"}, 200
    return {"status": "error", "response": "invalid credentials"}, 401


@server.route("/updates")
def updates():
    auth = check_auth()[0]
    if auth["status"] != "ok":
        return {"status": "error", "response": "Credentials are not set properly. Updates require them"}, 401
    return render_template("updates.html"), 200


@server.route("/updates/data")
def updates_data():
    limit = request.args.get('limit', 32)
    offset = request.args.get('offset', 0)
    _updates = repository.get_updates(limit, offset)
    if not _updates:
        repository.settings.logger.log(error, "couldn't get updates")
        return {"status": "error", "response": "no updates found"}, 404
    return jsonify(asdict(_updates)), 200


@server.route("/local-port")
def local_port():
    token = repository.credential_manager.token
    if not repository.credential_manager.validate_token(token):
        return {"status": "error", "response": "Invalid credentials"}, 403
    port = repository.connection.get_followed_manga()
    for manga in port.data:
        repository.downloader.queue.add_job(
            MangaDownloadJob(
                manga.id,
                repository.get_manga_attributes(manga.id),
                repository.get_chapter_list(manga.id),
                repository.database.get_manga_job(manga.id),
                repository.settings
            )
        )
    return {"status": "ok"}, 200


@server.route("/latest-updated-chapters")
def latest_updated_chapters():
    chapters_datatype = repository.get_latest_updated_chapters()
    chapters = []
    for chapter in chapters_datatype.data:
        new_chapter = asdict(chapter)
        attrs = new_chapter["attributes"]
        attrs["title"] = get_correct_language(chapter.attributes.title, None, repository.settings)
        for index, relationship in enumerate(chapter.relationships):
            if relationship.type != "manga":
                continue
            new_chapter["relationships"][index]["attributes"]["title"] = get_correct_language(
                new_chapter["relationships"][index]["attributes"]["title"],
                new_chapter["relationships"][index]["attributes"]["altTitles"],
                repository.settings
            )
        chapters.append(new_chapter)
    return chapters


@socketio.on('connect')
def handle_connect():
    # Send initial state to the client when they connect
    socketio.emit("update", repository.downloader.get_downloader_state())


if __name__ == "__main__":
    # thanks to socketio we can have sockets (much better downloader) but when our second thread is downloading
    # it is affecting the website because this now a coroutine
    # TODO: Try to fix that
    socketio.run(server, host="127.0.0.1", port=5000, debug=True)