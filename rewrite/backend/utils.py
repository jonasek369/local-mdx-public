import io
import time
import uuid

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
