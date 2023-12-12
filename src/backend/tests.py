import sys
import traceback

from custom_logger import Logger, info, succ, warn, erro, trcb
from app import MangadexConnection, MangaDownloader, Database, AlertSystem

from hashlib import sha256

Plogger = Logger(1)


def no_error_run(label, func, output_check=None, **kwargs):
    try:
        print(kwargs)
        output = func(*kwargs)
        if output_check is None:
            Plogger.log(succ, f"{label} has passed")
        else:
            try:
                assert output_check(output)
                Plogger.log(succ, f"{label} has passed and passed output check")
            except AssertionError:
                Plogger.log(warn, f"{label} has failed output check. output: {output}")
    except Exception:
        Plogger.log(erro, f"{label} has failed with stacktrace: ")
        for tb in traceback.format_exc().splitlines():
            Plogger.log(trcb, tb)


class DatabaseTests:
    def __init__(self):
        self.database = Database()

    def run(self):
        # TODO: finish
        # Just a prototype of test suite
        name = "testsaccount"
        pwd = sha256("testpassword".encode()).hexdigest()

        no_error_run("create user", self.database.add_user, name=name, pwd_hash=pwd)
        no_error_run("login user", self.database.login_user, output_check=lambda x: x is True, name=name, pwd_hash=pwd)

