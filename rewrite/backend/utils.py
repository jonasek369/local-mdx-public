from datetime import datetime
import io
import os
import time
import uuid
from enum import Enum

from PIL import Image


def perf_test(func):
    """
    A decorator to measure the execution time of a function.
    """

    def wrapper(*args, **kwargs):
        start_time = time.perf_counter()  # Record the start time
        result = func(*args, **kwargs)  # Call the function
        end_time = time.perf_counter()  # Record the end time
        execution_time = end_time - start_time  # Calculate the elapsed time
        print(f"Function '{func.__name__}' executed in {execution_time:.4f} seconds.")
        return result

    return wrapper


def is_valid_uuid(val):
    try:
        uuid.UUID(str(val))
        return True
    except ValueError:
        return False


def resize_image(image_data, resize_factor=4):
    try:
        image = Image.open(io.BytesIO(image_data))

        # Calculate new size based on the resize factor
        new_width = max(1, image.width // resize_factor)
        new_height = max(1, image.height // resize_factor)

        # Resize the image
        image = image.resize((new_width, new_height))

        # Save the resized image to a BytesIO buffer
        output_buffer = io.BytesIO()
        image.save(output_buffer, format="WEBP")

        # Get the byte data from the buffer
        resized_image_data = output_buffer.getvalue()

        return resized_image_data
    except Exception as e:
        print(f"An error occurred while resizing the image: {e}")
        return None


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
success = LogType.SUCCESSS
warning = LogType.WARNING
error = LogType.ERROR
critical = LogType.CRITICAL
traceback = LogType.TRACEBACK


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
