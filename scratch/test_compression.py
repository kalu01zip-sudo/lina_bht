import io
import os
from PIL import Image
from app.utils.image_utils import optimise_image

def create_large_image(path, size_mb=6.25):
    # Create a noisy image that is hard to compress
    width, height = 3000, 3000
    img = Image.new("RGB", (width, height), "white")
    # Add some noise
    from PIL import ImageDraw
    import random
    draw = ImageDraw.Draw(img)
    for _ in range(1000):
        x1 = random.randint(0, width)
        y1 = random.randint(0, height)
        x2 = random.randint(0, width)
        y2 = random.randint(0, height)
        draw.line((x1, y1, x2, y2), fill=(random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)), width=5)
    
    img.save(path, format="JPEG", quality=100)
    print(f"Created large image at {path}, size: {os.path.getsize(path)/1024/1024:.2f} MB")

def test_optimisation():
    test_path = "scratch/large_test_image.jpg"
    create_large_image(test_path)
    
    with open(test_path, "rb") as f:
        data = f.read()
    
    opt_bytes, opt_type = optimise_image(data, "image/jpeg", max_px=1024, quality=82)
    
    print(f"Optimised size: {len(opt_bytes)/1024/1024:.2f} MB")
    print(f"Optimised type: {opt_type}")
    
    # Verify size is under limit (3.5MB)
    assert len(opt_bytes) <= 3670016
    
    # Verify dimensions
    opt_img = Image.open(io.BytesIO(opt_bytes))
    print(f"Optimised dimensions: {opt_img.width}x{opt_img.height}")
    assert max(opt_img.width, opt_img.height) <= 1024

    os.remove(test_path)
    print("Test passed!")

if __name__ == "__main__":
    test_optimisation()
