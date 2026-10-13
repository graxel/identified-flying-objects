from picamera2 import Picamera2

MAIN_SIZE = (4056, 3040)

SIZES = [
    (320, 240),
    (640, 480),
    (768, 600),
    (800, 600),
    (1024, 768),
    (1280, 960),
    (1600, 1200),
    (1920, 1440),
    (2048, 1536),
    (2560, 1920),
    (3200, 2400),
    (4032, 3040),
    (4056, 3040),
]

picam2 = Picamera2()

for requested in SIZES:
    try:
        config = picam2.create_preview_configuration(
            main={"size": MAIN_SIZE, "format": "RGB888"},
            lores={"size": requested, "format": "YUV420"},
        )
        picam2.align_configuration(config)

        actual = config["lores"]["size"]
        stride = config["lores"].get("stride")

        print(
            f"requested={requested} "
            f"actual={actual} "
            f"stride={stride}"
        )
    except Exception as e:
        print(f"requested={requested} FAILED: {e}")
