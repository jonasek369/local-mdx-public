import logging
import sys

from server import server, app

import webview

logger = logging.getLogger(__name__)


def close_threads():
    app.Plogger.log(app.info, "Closing threads")
    app.DlProcessor.stop()
    app.alert_sys.stop()


if __name__ == '__main__':
    window = webview.create_window('Test', server, width=1280, height=720, confirm_close=False, http_port=2767)
    window.events.closing += close_threads
    webview.start(debug=True, private_mode=False)
    sys.exit(0)
