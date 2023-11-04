import os.path
import time
from enum import Enum
from datetime import datetime


def colored(rgb, text):
    return "\033[38;2;{};{};{}m{} \033[38;2;255;255;255m".format(rgb[0], rgb[1], rgb[2],
                                                                 text)


class LogType(Enum):
    INFO = 1
    SUCCESSS = 2
    WARNING = 3
    ERROR = 4
    CRITICAL = 5
    TRACEBACK = 6


info = LogType.INFO
succ = LogType.SUCCESSS
warn = LogType.WARNING
erro = LogType.ERROR
crit = LogType.CRITICAL
trcb = LogType.TRACEBACK


class Logger:
    def __init__(self, ll: int = 1, file_logger: bool = False):
        self.log_level = ll
        self.file_logger = file_logger
        if self.file_logger:
            self.file_log = []
            self.file_start = time.time()
            if not os.path.isdir("logs"):
                os.mkdir("logs")
            self.file = open(f"logs\\{self.file_start}.log", "w")
        os.system("cls")

    def log(self, ll: LogType, text):
        if ll.value >= self.log_level:
            match ll:
                case LogType.INFO:
                    print(colored([0, 100, 255], "Info: " + text))
                case LogType.SUCCESSS:
                    print(colored([0, 255, 0], "Success: " + text))
                case LogType.WARNING:
                    print(colored([255, 255, 0], "Warning: " + text))
                case LogType.ERROR:
                    print(colored([255, 60, 60], "Error: " + text))
                case LogType.CRITICAL:
                    print(colored([255, 0, 0], "Critical: " + text))
                case LogType.TRACEBACK:
                    print(colored([255, 255, 255], "Traceback: " + text))
                case _:
                    raise Exception("Unknown log level")

        if self.file_logger:
            self.file_log.append((datetime.now(), ll, text))

    def __del__(self):
        if not self.file_logger:
            return
        with self.file as file:
            for log_event in self.file_log:
                file.write(f"{log_event[0]}:{log_event[1]}: {log_event[2]}\n")
