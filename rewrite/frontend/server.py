try:
    import os
    from pathlib import Path

    # Ensure all files (database, settings, etc.) are created in the folder containing server.py
    os.chdir(Path(__file__).parent)
except Exception as e:
    print("Could not set dir to 'server.py' parent folder")
    exit(1)

import asyncio
import base64
import json
import time
from dataclasses import asdict

from rewrite.backend.connection import MangaDownloadJob
from rewrite.backend.repository import MangaRepository
from rewrite.backend.schemas import COVER_ART_MAX_SIZE, COVER_ART_256_SIZE, COVER_ART_512_SIZE
from rewrite.backend.settings import credentials_from_json, save_credentials, save_settings, clear_keyring
from rewrite.backend.utils import debug, info, error, get_correct_language, is_uuid4, critical, \
    settings_to_jsonable_dict, warning
from flask import Flask, jsonify, render_template, request, make_response, stream_with_context, Response, abort, \
    session, redirect
from werkzeug.security import check_password_hash, generate_password_hash
from datetime import timedelta

template_dir = os.path.join(os.getcwd(), 'gui')

server = Flask(__name__, static_folder=template_dir, template_folder=template_dir)

repository = MangaRepository()

if repository.settings.requireAuth:
    if repository.settings.authPassword is None:
        repository.settings.logger.log(critical, "Auth is enabled but no password set. Aborting!")
        exit(1)
    if len(repository.settings.authPassword) <= 6:
        repository.settings.logger.log(warning, "Auth is enabled but the password is short. This is not secure!")

    AUTH_HASH = generate_password_hash(repository.settings.authPassword)

server.permanent_session_lifetime = timedelta(hours=12)

server.config.update(
    SECRET_KEY=os.urandom(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Lax",
)


@server.before_request
def require_auth():
    if repository.settings.requireAuth:
        if request.path in ("/login", "/login/check"):
            return

        if not session.get("authenticated"):
            abort(401)


@server.route("/login")
def login():
    if not repository.settings.requireAuth:
        return {"status": "error", "response": "Auth is not enabled"}
    return render_template("login.html", darktheme=repository.settings.darkTheme)


@server.route("/login/check", methods=["POST"])
def login_check():
    if not repository.settings.requireAuth:
        return {"status": "error", "response": "Auth is not enabled"}

    try:
        data = request.get_json(force=True)
    except Exception as e:
        return jsonify({"status": "error", "response": "Invalid JSON body"}), 400

    if "password" not in data:
        return jsonify({"status": "error", "response": "JSON body dose not have password"}), 400

    if not data.get("password") or not check_password_hash(AUTH_HASH, data.get("password")):
        return {"status": "error", "response": "bad password"}

    session["authenticated"] = True
    return {"status": "ok", "response": "Session authenticated"}


@server.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@server.route("/")
def landing():
    return render_template("index.html", darktheme=repository.settings.darkTheme)


@server.route('/search/manga', methods=['POST'])
def search():
    try:
        request_data = request.get_json(force=True)
    except Exception as e:
        repository.settings.logger.log(error, f"Caught exception while search! {e}")
        return jsonify({"status": "error", "response": "Invalid JSON body"}), 400

    search_term = request_data.get("searchTerm")
    if not search_term:
        return jsonify({"status": "error", "response": "Missing or empty 'searchTerm'"}), 400

    limit_arg = request.args.get("limit", 5)
    try:
        limit = int(limit_arg)
    except ValueError:
        return jsonify({"status": "error", "response": "Invalid limit"}), 400

    if limit > 50:
        return jsonify({"status": "error", "response": "Limit too high (max 50)"}), 400

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
        return jsonify({"status": "error", "response": "Internal server error"}), 500


@server.route("/manga/cover/<identifier>")
def get_cover_art(identifier):
    if not is_uuid4(identifier):
        return jsonify({"status": "error", "response": "Invalid identifier"}), 400

    try:
        size = int(request.args.get("size", COVER_ART_256_SIZE))
    except (TypeError, ValueError):
        size = COVER_ART_MAX_SIZE

    allowed_sizes = {COVER_ART_MAX_SIZE, COVER_ART_256_SIZE, COVER_ART_512_SIZE}
    if size not in allowed_sizes:
        size = COVER_ART_MAX_SIZE

    image_binary = repository.get_cover_art(identifier, size)
    if not image_binary:
        return jsonify({"error": "No image found"}), 404

    response = make_response(image_binary)
    response.headers.set("Content-Type", "image/png")
    response.headers.set("Content-Disposition", f"inline; filename={identifier}.png")
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response, 200


@server.route("/manga/<mangauuid>", methods=["GET"])
def server_manga(mangauuid):
    back = request.args.get('from', "/")

    if not is_uuid4(mangauuid):
        return {"status": "error", "message": "uuid is not a valid uuid4"}

    manga = repository.get_manga_attributes(mangauuid)
    if not manga:
        return jsonify({"status": "error", "response": "Manga could not be found"}), 404

    return render_template("manga.html",
                           muuid=mangauuid,
                           name=get_correct_language(manga.title, manga.altTitles, repository.settings),
                           description=get_correct_language(manga.description, None, repository.settings),
                           back_redirect=back,
                           darktheme=repository.settings.darkTheme), 200


@server.route("/manga/<mangauuid>/info", methods=["GET"])
def get_manga_info(mangauuid):
    if not is_uuid4(mangauuid):
        return {"status": "error", "response": "manga uuid is not valid uuid4"}, 400
    downloaded_pages = repository.get_downloaded_pages(mangauuid)
    downloaded_lookup = {i[0]: i[1:] for i in downloaded_pages}

    user_and_groups = repository.database.get_user_and_groups([dp[0] for dp in downloaded_pages])

    feed = repository.get_manga_feed(mangauuid, force_latest=True)
    if not feed:
        feed = repository.get_manga_feed(mangauuid, force_latest=False)
        if not feed:
            return {"status": "error", "message": "Could not get feed"}, 400

    chapters = []

    read_chapters = repository.database.get_manga_read_chapters(mangauuid)
    if not read_chapters:
        read_chapters = []

    for chapter in feed.data:
        lookup = downloaded_lookup.get(chapter.id, None)
        if lookup is None:
            continue
        chapters.append({
            "identifier": chapter.id,
            "title": lookup[0],
            "volume": lookup[1],
            "chapter": lookup[2],
            "user": user_and_groups[chapter.id]["user"],
            "scanlation_group": user_and_groups[chapter.id]["scanlation_group"],
            "read_status": True if chapter.id in read_chapters else False
        })
    return jsonify(chapters), 200


@server.route("/manga/read-status", methods=["POST"])
def read_status():
    try:
        data = request.get_json(force=True)
    except Exception:
        return {"status": "error", "response": "Endpoint requires json"}, 400
    if "cuuid" not in data or "read_state" not in data:
        return {"status": "error", "response": "Endpoint requires 'cuuid' and 'read_state'"}, 400
    if not is_uuid4(data["cuuid"]):
        return {"status": "error", "response": "'cuuid' must be valid uuid4"}, 400
    try:
        read_state = bool(data["read_state"])
    except ValueError:
        return {"status": "error", "response": "'read_state' should be bool or 0/1"}, 400

    change_all = bool(request.args.get('all', False))

    cuuid, muuid, *_ = repository.database.get_chapter_attribute_raw(data["cuuid"])
    if muuid is None:
        return {"status": "error", "response": "Database chapter attributes do not contain muuid"}, 400

    if change_all and not read_state:
        repository.database.remove_manga_read_records(muuid)
        return {"status": "ok", "response": "Changed all read statuses"}
    elif change_all and read_state:
        repository.database.add_manga_read_records(muuid)
        return {"status": "ok", "response": "Changed all read statuses"}

    contains_status = repository.database.get_chapter_read_status(cuuid)
    if contains_status is None and read_state is True:
        repository.database.add_read_record(muuid, cuuid)
    elif contains_status and read_state is False:
        repository.database.remove_read_record(muuid, cuuid)

    return {"status": "ok", "response": "Changed read state"}, 200


@server.route("/manga/<mangauuid>/attributes")
def manga_attributes(mangauuid):
    # TODO: Having this local only and getting the correct lanaguage might not work for everything and might need
    # TODO: To be reworked currently updates.html uses it
    attributes = repository.get_manga_attributes(mangauuid, True)
    attributes.title = get_correct_language(attributes.title, attributes.altTitles, repository.settings)
    if not attributes:
        return {"status": "error", "response": "Could not fetch attributes"}, 500
    return jsonify(asdict(attributes)), 200


@server.route("/manga/delete/<mangauuid>")
def delete_manga(mangauuid):
    if not repository.database.delete_manga(mangauuid):
        return {"status": "error", "response": "Could not delete manga"}
    return {"status": "ok"}, 200


@server.route("/page-image/<identifier>/<page>")
def get_chapter_image(identifier, page):
    if not page.isdigit():
        return {"status": "error", "response": "page is not an number"}, 400
    image_binary = repository.database.get_page(identifier, int(page))
    if image_binary is not None:
        response = make_response(image_binary)
        response.headers.set('Content-Type', 'image/png')
        response.headers.set('Content-Disposition', 'inline', filename=f'{identifier}-{page}.png')
        return response, 200
    return {"status": "error", "response": "no image found"}, 404


@server.route("/page-images/<identifier>/")
def get_chapter_images(identifier):
    image_binary = repository.database.get_pages(identifier)
    if image_binary is not None:
        data = {}
        start = time.perf_counter()
        for page, image in image_binary:
            encoded_image = base64.b64encode(image).decode("ascii")
            data[page] = encoded_image
        end = time.perf_counter()
        repository.settings.logger.log(debug, f"packing took {(end - start) * 1000}ms")
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
    muuid_attributes = None
    if ids.get("muuid", None) is not None:
        muuid_attributes = repository.get_manga_attributes(ids["muuid"])

    page_render = "NORMAL"

    for tag in muuid_attributes.tags:
        if tag.attributes.group == "format" and tag.attributes.name["en"] == "Long Strip":
            page_render = "LONG_STRIP"

    if not attributes:
        return {"status": "error", "response": "Data not in database"}

    if from_end:
        page = attributes.pages

    return render_template("read.html",
                           cuuid=chapteruuid,
                           pages=attributes.pages,
                           muuid=ids["muuid"],
                           chapter_no=attributes.chapter,
                           page=page,
                           page_render=page_render,
                           darktheme=repository.settings.darkTheme
                           ), 200


def push_job_from_data(data: dict):
    if "id" not in data:
        return {"status": "error", "response": "id not in JSON body"}, 400

    if not is_uuid4(data.get("id")):
        return {"status": "error", "response": "invalid id"}, 400

    if repository.downloader.currently_working_on and repository.downloader.currently_working_on["id"] == data.get(
            "id"):
        return {"status": "error", "response": "id is already in queue"}, 400

    repository.downloader.queue.put(
        MangaDownloadJob(
            data["id"],
            repository.get_manga_attributes(data["id"]),
            repository.get_chapter_list(data["id"]),
            repository.database.get_manga_job(data["id"]),
            repository.settings
        )
    )

    return {"status": "success"}, 200


@server.route("/manga/download/push-job", methods=["POST"])
def push_job():
    try:
        data = request.get_json(force=True)
    except Exception as e:
        repository.settings.logger.log(error, f"Caught exception while pushing job! {e}")
        return {"status": "error", "response": "Invalid JSON body"}, 400

    return push_job_from_data(data)


@server.route("/manga/download/pop-job", methods=["POST"])
def pop_job():
    try:
        data = request.get_json(force=True)
    except Exception:
        return {"status": "error", "response": "no JSON provided"}, 400
    if "identifier" not in data:
        return {"status": "error", "response": "Index not in JSON"}, 400
    if not is_uuid4(data["identifier"]):
        return {"status": "error", "response": "'identifier' is not valid uuid4"}

    repository.downloader.queue.remove_job(data["identifier"])

    return {"status": "success", "response": "popped job"}, 200


@server.route("/manga/download/contains", methods=["POST"])
def downloader_contains():
    try:
        data = request.get_json(force=True)
    except Exception as e:
        repository.settings.logger.log(error, f"Caught exception while checking downloader contain! {e}")
        return {"status": "error", "response": "Invalid JSON body"}, 400
    if "id" not in data:
        return {"status": "error", "response": "id not in JSON body"}, 400
    if not is_uuid4(data.get("id")):
        return {"status": "error", "response": "invalid id"}, 400

    muuid = data.get("id")

    dl_state = repository.downloader.get_downloader_state()

    if dl_state["currently_working_on"] is not None:
        contains = muuid in dl_state["queue"] or dl_state["currently_working_on"]["id"] == muuid
    else:
        contains = muuid in dl_state["queue"]

    return {"status": "success", "data": {"contains": contains}}, 200


@server.route("/manga/download/manager", methods=["GET"])
def download_manager():
    back = request.args.get('from', "/")
    return render_template("download-manager.html", darktheme=repository.settings.darkTheme, back=back), 200


@server.route("/manga/download/start", methods=["GET"])
def start_download():
    repository.downloader.start()
    return {"status": "success", "response": "started downloader"}, 200


@server.route("/manga/download/stop", methods=["GET"])
def stop_download():
    repository.downloader.stop()
    return {"status": "success", "response": "stopped downloader"}, 200


@server.route("/manga/download/get-state", methods=["GET"])
def get_state():
    status = repository.downloader.get_downloader_state()
    return status, 200


@server.route('/manga/download/stream')
def stream():
    def event_stream():
        last_stream = None
        while True:
            data = repository.downloader.get_downloader_state()
            stream_to_send = f"data: {json.dumps(data)}\n\n"
            if stream_to_send != last_stream:
                yield stream_to_send
                last_stream = stream_to_send
            time.sleep(0.25)

    return Response(
        stream_with_context(event_stream()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache"}
    )


@server.route("/manga/download/push-to-top", methods=["POST"])
def push_to_top():
    try:
        data = request.get_json(force=True)
    except Exception:
        return {"status": "error", "response": "no JSON provided"}, 400
    if "identifier" not in data:
        return {"status": "error", "response": "Index not in JSON"}, 400
    if not is_uuid4(data["identifier"]):
        return {"status": "error", "response": "'identifier' is not valid uuid4"}

    repository.downloader.queue.push_to_front(data["identifier"])

    return {"status": "success", "response": "pushed job to top"}, 200


@server.route("/manga/library/data", methods=["GET"])
def library_data():
    to_send = {}
    mangas = repository.database.all_manga_in_db()
    if not mangas:
        return {"status": "success", "response": to_send}, 200
    for manga in mangas:
        pages = repository.database.get_downloaded_chapters(manga[0])
        if pages:
            to_send[manga[0]] = [
                get_correct_language(json.loads(manga[1]), json.loads(manga[2]), repository.settings),
                get_correct_language(json.loads(manga[3]), None, repository.settings)
            ]

    repository.sync_libraries(list(to_send.keys()))
    return {"status": "success", "response": to_send}


@server.route("/manga/library/update")
def library_update():
    mangas = repository.database.all_manga_in_db()
    if not mangas:
        return {"status": "error", "response": "No downloaded manga"}

    for manga in mangas:
        push_job_from_data({"id": manga[0]})

    return {"status": "success", "response": "Mangas addded to downloaded queue"}, 200


@server.route("/manga/library", methods=["GET"])
def library():
    return render_template("library.html", darktheme=repository.settings.darkTheme)


@server.route("/popular-new-titles", methods=["GET"])
def popular_new_titles():
    popular = asyncio.run(repository.popular_new_titles())
    if popular is None:
        return {"status": "error", "message": "Could not fetch popular new titles"}, 400
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


@server.route("/auth/null-credentials")
def null_credentials():
    clear_keyring()
    return {"status": "ok", "response": "creadentials nulled"}, 200


@server.route("/updates")
def updates():
    auth = check_auth()[0]
    if auth["status"] != "ok":
        return {"status": "error", "response": "Credentials are not set properly. Updates require them"}, 401
    return render_template("updates.html", darktheme=repository.settings.darkTheme), 200


@server.route("/updates/data")
def updates_data():
    limit = request.args.get('limit', 32)
    offset = request.args.get('offset', 0)
    _updates = repository.get_updates(limit, offset)
    if not _updates:
        repository.settings.logger.log(error, "couldn't get updates")
        return {"status": "error", "response": "no updates found"}, 404
    return jsonify(asdict(_updates)), 200


@server.route("/latest-updated-chapters")
def latest_updated_chapters():
    chapters_datatype = repository.get_latest_updated_chapters()
    if chapters_datatype is None:
        return {"status": "error", "message": "Could not get latest updated chapters"}, 400
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


@server.route("/local-port")
def local_port():
    # test function that will "port" all mangas from saved to local
    token = repository.credential_manager.token
    if not repository.credential_manager.validate_token(token):
        return {"status": "error", "response": "Invalid credentials"}, 403
    port = repository.connection.get_followed_manga()
    for manga in port.data:
        repository.downloader.queue.put(
            MangaDownloadJob(
                manga.id,
                repository.get_manga_attributes(manga.id),
                repository.get_chapter_list(manga.id),
                repository.database.get_manga_job(manga.id),
                repository.settings
            )
        )

        time.sleep(5)
    return {"status": "ok"}, 200


@server.route("/config")
def config():
    return render_template("config.html")


@server.route("/config/data", methods=["GET", "POST"])
def config_data():
    if request.method == "GET":
        return jsonify(settings_to_jsonable_dict(repository.settings)), 200
    if request.method == "POST":
        try:
            request_data = request.get_json(force=True)
        except Exception as e:
            return {"status": "error", "response": "Json is invalid"}, 400

        repository.settings.cacheTokenToDisk = bool(request_data.get("cacheTokenToDisk", False))
        repository.settings.logLevel = max(min(7, int(request_data.get("logLevel", 1))), 1)
        repository.settings.logger.log_level = repository.settings.logLevel
        repository.settings.fileLogger = bool(request_data.get("fileLogger", False))
        repository.settings.logger.change_file_logger(repository.settings.fileLogger)
        repository.settings.darkTheme = bool(request_data.get("darkTheme", True))

        save_settings(repository.settings)
        return {"status": "ok", "response": "Settings saved"}, 200


if __name__ == "__main__":
    use_actual_server = True
    try:
        if not use_actual_server:
            server.run(threaded=True)
        else:
            from waitress import serve

            serve(server, host="0.0.0.0", port=5000, threads=os.cpu_count())
    finally:
        # Wakeup and exit thread
        repository.downloader.exit()  # signals the thread for exit
        repository.downloader.queue.put(
            None)  # this will wakeup the thread because it is most likely waiting for queue.pop()
