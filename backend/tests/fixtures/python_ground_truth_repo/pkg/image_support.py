try:
    from PIL import Image
except ImportError:
    Image = None


def image_module_available() -> bool:
    return Image is not None
