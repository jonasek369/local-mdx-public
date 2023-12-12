import sys

from server import server, app

import webview


def close_threads():
    app.Plogger.log(app.info, "Closing threads")
    app.discord_integration.stop()
    app.DlProcessor.stop()
    app.alert_sys.stop()
    app.cache.close()


if __name__ == '__main__':
    window = webview.create_window('Lmdx', server, width=1280, height=720, confirm_close=False, http_port=5000)
    window.events.closing += close_threads
    webview.start(debug=True, private_mode=False)
    sys.exit(0)
