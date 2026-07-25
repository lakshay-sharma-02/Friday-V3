#!/usr/bin/env python3
"""
Generate an image file using ImageMagick via Wand.
"""

from wand.image import Image
from wand.color import Color


def create_sample_image(output_path: str = "output.png") -> None:
    """
    Create a simple image file using ImageMagick.
    
    Args:
        output_path: Path where the image file will be saved.
    """
    with Image(width=200, height=200, background=Color("white")) as img:
        # Draw some content
        img.format = "png"
        img.save(filename=output_path)
        print(f"Image created: {output_path}")


if __name__ == "__main__":
    create_sample_image()
