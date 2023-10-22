import base64
import json
import os
import sys
import time
import uuid
from functools import wraps

try:
    import webview
except ImportError:
    class wv:
        def __init__(self):
            self.windows = []
            self.token = -1


    webview = wv()
from flask import Flask, jsonify, render_template, request, make_response

import app
from gzip import compress, decompress

gui_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'gui')  # development path

if not os.path.exists(gui_dir):  # frozen executable path
    gui_dir = os.path.join(os.getcwd(), 'gui')

print(gui_dir, "is static and template dir!")

server = Flask(__name__, static_folder=gui_dir, template_folder=gui_dir)
server.config['SEND_FILE_MAX_AGE_DEFAULT'] = 1  # disable caching


def is_valid_uuid(val):
    try:
        uuid.UUID(str(val))
        return True
    except ValueError:
        return False


def close_threads():
    app.Plogger.log(app.info, "Shutting down threads")
    app.DlProcessor.stop()
    app.alert_sys.stop()


@server.route('/')
def landing():
    """
    Render index.html. Initialization is performed asynchronously in initialize() function
    """
    app.initialize()
    visitor_login = render_template('index.html', token=webview.token, login=0)
    try:
        webview.windows[0].set_title("Lmdx")
    except IndexError:
        pass

    if request.cookies.get("session"):
        if app.session_manager.validate_session(request.cookies.get("session")):
            name = app.session_manager.get_session_name(request.cookies.get("session"))
            # expired or non existent session
            if name is None:
                request.cookies.pop("session")
                return visitor_login
            return render_template('index.html', token=webview.token, login=1, name=name)
    return visitor_login


@server.route('/init', methods=['POST'])
def initialize():
    """
    Perform heavy-lifting initialization asynchronously.
    :return:
    """
    can_start = app.initialize()

    if can_start:
        response = {
            'status': 'ok',
        }
    else:
        response = {'status': 'error'}

    return jsonify(response)


@server.route('/search/manga', methods=['POST'])
def search():
    app.initialize()
    data = request.json

    result = app.search_manga(data["query"])

    if result:
        response = {'status': 'ok', 'result': result}
    else:
        response = {'status': 'error'}

    return jsonify(response)


@server.route("/manga/<mangauuid>/info", methods=["GET"])
def get_manga_info(mangauuid):
    app.initialize()
    chapters_ids = app.connection.get_chapter_list(mangauuid)
    rowid = app.database.get_rowid(mangauuid)
    chapters, warnings = app.database.get_chapters(chapters_ids, rowid)
    for key in chapters.keys():
        del chapters[key]["pages"]
        try:
            volume = float(chapters[key]["volume"])
        except TypeError:
            volume = None
        chapter = float(chapters[key]["chapter"])
        del chapters[key]["volume"]
        del chapters[key]["chapter"]
        chapters[key]["v"] = volume
        chapters[key]["c"] = chapter
    response = make_response(compress(json.dumps({"chapters": chapters, "warning": warnings}).encode()))
    response.headers.set("Content-Encoding", "gzip")
    return response


@server.route("/chapter-links/<mangauuid>", methods=["GET"])
def chapter_links(mangauuid):
    app.initialize()
    rowid = app.database.get_rowid(mangauuid)
    chapters = app.database.get_chapters([{"id": mangauuid}], rowid)
    return jsonify(chapters)


@server.route("/page-image/<identifier>/<page>")
def get_chapter_image(identifier, page):
    app.initialize()
    image_binary = app.database.get_page(identifier, page)
    if image_binary is not None:
        response = make_response(image_binary)
        response.headers.set('Content-Type', 'image/jpeg')
        response.headers.set('Content-Disposition', 'inline', filename=f'{identifier}-{page}.png')
        return response
    return "Error: no image found"


@server.route("/manga/cover/<identifier>")
def get_cover_art(identifier):
    app.initialize()
    image_binary = app.database.get_cover(identifier, small=bool(request.args.get('small')))
    if image_binary is not None:
        response = make_response(compress(image_binary))
        response.headers.set('Content-Type', 'image/jpeg')
        response.headers.set('Content-Disposition', 'inline', filename=f'{identifier}.jpg')
        response.headers.set("Content-Encoding", "gzip")
        return response
    return "Error: no image found"


@server.route("/read/next-prev/<chapteruuid>")
def chapter_next_previous(chapteruuid):
    app.initialize()
    try:
        next_prev = app.connection.get_next_and_prev(chapteruuid)
    except Exception as e:
        app.Plogger.log(app.erro, f"Could not get next and prev: {e}")
        next_prev = {}
    return next_prev


@server.route("/read/<chapteruuid>", methods=["GET"], defaults={"page": 1})
@server.route("/read/<chapteruuid>/<page>", methods=["GET"])
def read_manga(chapteruuid, page):
    try:
        page = int(page)
        if not app.database.get_page(chapteruuid, page):
            page = 1
    except ValueError:
        page = 1
    next_prev = chapter_next_previous(chapteruuid)
    chapter = app.try_to_get(next_prev, ["current", "chapter"])
    volume = app.try_to_get(next_prev, ["current", "volume"])
    try:
        webview.windows[0].set_title(f"Volume {volume} Chapter {chapter}")
    except IndexError:
        pass
    muuid = app.connection.get_manga_uuid_from_chapter(chapteruuid)
    rowid = app.database.get_rowid(muuid)
    session = request.cookies.get("session")
    if app.session_manager.validate_session(session):
        name = app.session_manager.get_session_name(session)
        data = app.database.get_user_data(name)
        if "read_manga" not in data:
            data["read_manga"] = {}
        if muuid not in data["read_manga"]:
            data["read_manga"][muuid] = []

        if chapteruuid not in data["read_manga"][muuid]:
            data["read_manga"][muuid].append(chapteruuid)
            app.database.set_user_data(name, data)
    return render_template("read.html", cuuid=chapteruuid,
                           pages=app.database.get_chapters([{"id": chapteruuid}], rowid)[0][chapteruuid]["pages"],
                           muuid=muuid,
                           page=page
                           )


@server.route("/user/data")
def user_data():
    session = request.cookies.get("session")
    if app.session_manager.validate_session(session):
        name = app.session_manager.get_session_name(session)
        return {"success": app.database.get_user_data(name)}
    else:
        return {"error": "invalid session"}


@server.route("/manga/<mangauuid>", methods=["GET"])
def server_manga(mangauuid):
    app.initialize()
    back = request.args.get('from')
    if back is None:
        back = "/"

    app.connection.set_manga_info(mangauuid)
    _, name, description, _, _ = app.database.get_info(mangauuid)
    if name is None:
        name = "Could not find name"
    if description is None:
        description = "Could not find description"
    try:
        if name is not None:
            webview.windows[0].set_title(name)
    except IndexError:
        pass
    return render_template("manga.html", muuid=mangauuid, name=name, description=description, back_redirect=back)


@server.route("/manga/download/status")
def download_status():
    app.initialize()

    status = app.DlProcessor.currently_working_on.copy()

    for idx, job in enumerate(status):
        njob = job
        njob["name"] = app.database.get_info(job["id"])[1]
        status[idx] = njob

    return {"status": status, "in_progress": app.DlProcessor.in_progress,
            "speed_mode": app.DlProcessor.mode}


@server.route("/manga/download/push-job", methods=["POST"])
def push_job():
    app.initialize()
    data = request.json
    if "id" not in data:
        return {"status": "error"}
    if not is_valid_uuid(data.get("id")):
        return {"status": "error"}
    return app.DlProcessor.push_queue({"id": data.get("id")})


@server.route("/manga/download/pop-job", methods=["POST"])
def pop_job():
    app.initialize()
    data = request.json
    if "index" not in data:
        return {"status": "error"}
    try:
        int(data.get("index"))
    except ValueError:
        return {"status": "error"}
    return app.DlProcessor.remove_queue(data)


@server.route("/manga/download/push-to-top", methods=["POST"])
def push_to_top():
    app.initialize()
    data = request.json
    if "index" not in data:
        return {"status": "error"}
    try:
        int(data.get("index"))
    except (ValueError, TypeError):
        return {"status": "error"}
    if 0 > int(data.get("index")) > len(app.DlProcessor.queue) - 1:
        return {"status": "error"}
    if len(app.DlProcessor.queue) == 1:
        return {"status": "success"}
    app.DlProcessor.queue.insert(0, app.DlProcessor.queue.pop(int(data.get("index"))))
    return {"status": "success"}


@server.route("/manga/download/start", methods=["GET"])
def start_download():
    app.initialize()
    app.DlProcessor.start()
    return {"status": "success"}


@server.route("/manga/download/stop", methods=["GET"])
def stop_download():
    app.initialize()
    app.DlProcessor.stop()
    return {"status": "success"}


@server.route("/manga/download/queue", methods=["GET"])
def get_queue():
    app.initialize()
    queue = app.DlProcessor.queue.copy()

    for idx, job in enumerate(queue):
        njob = job
        njob["name"] = app.database.get_info(job["id"])[1]
        queue[idx] = njob

    return {"queue": queue}


@server.route("/manga/download/speed", methods=["POST"])
def set_speed():
    data = request.json
    if data.get("speed") is None:
        return {"status": "error"}
    if data.get("speed").upper() not in ["FAST", "NORMAL", "SLOW"]:
        return {"status": "error"}
    app.DlProcessor.change_mode(data.get("speed").upper())
    return {"status": "ok"}


@server.route("/manga/download/manager", methods=["GET"])
def download_manager():
    app.initialize()
    return render_template("download-manager.html")


@server.route("/manga/library", methods=["GET"])
def library():
    app.initialize()
    return render_template("library.html")


@server.route("/manga/library/data", methods=["GET"])
def library_data():
    app.initialize()
    to_send = {}
    for identifier, name, desc in app.database.manga_in_db():
        to_send[identifier] = [name, desc]
    return {"status": "ok", "response": to_send}


@server.route("/updates/data")
def updates_data():
    app.initialize()
    return {"status": "ok", "response": app.database.get_updates()}


@server.route("/updates")
def update():
    app.initialize()
    return render_template("updates.html")


@server.route("/updates/settings")
def updates_settings():
    return render_template("updates-settings.html")


@server.route("/login")
def login():
    back = request.args.get('from')
    if back is None:
        back = "/"
    return render_template("login.html", back_redirect=back)


@server.route("/login/verify", methods=["POST"])
def login_verify():
    app.initialize()
    data = request.json
    if not data:
        return {"error": "invalid error"}
    if "username" not in data or "password" not in data:
        return {"error": "credentials are not fulfilled"}

    user_login = app.database.login_user(data["username"], data["password"])
    if user_login:
        session = json.dumps(app.session_manager.make_session(data["username"]))
        return {"success": "You were logged in", "session": session}
    return {"error": "invalid credentials"}


@server.route("/signup")
def signup():
    back = request.args.get('from')
    if back is None:
        back = "/"
    return render_template("sign-up.html", back_redirect=back)


@server.route("/signup/verify", methods=["POST"])
def signup_verify():
    app.initialize()
    data = request.json
    if not data:
        return {"error": "invalid error"}
    if "username" not in data or "password" not in data:
        return {"error": "credentials are not fulfilled"}

    if app.database.add_user(data["username"], data["password"]):
        session = json.dumps(app.session_manager.make_session(data["username"]))
        return {"success": "Your account was created", "session": session}
    return {"error": "Could not create an account"}


@server.route("/read-status/<muuid>/<cuuid>", methods=["GET"])
def read_status(muuid, cuuid):
    app.initialize()
    session = request.cookies.get("session")
    if app.session_manager.validate_session(session):
        name = app.session_manager.get_session_name(session)
        data = app.database.get_user_data(name)
        if "read_manga" not in data:
            data["read_manga"] = {}
        if muuid not in data["read_manga"]:
            data["read_manga"][muuid] = []
        if cuuid in data["read_manga"][muuid]:
            data["read_manga"][muuid].remove(cuuid)
        else:
            data["read_manga"][muuid].append(cuuid)
        app.database.set_user_data(name, data)
        return {"success": "status changed"}
    return {"error": "invalid session"}


if __name__ == "__main__":
    USE_SERVER = 1
    if not USE_SERVER:
        server.run()
    else:
        # testing performance on other devices
        from waitress import serve
        serve(server, listen="192.168.0.17:5000")