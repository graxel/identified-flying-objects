from picamera2 import Picamera2


MAIN_SIZE = (4032, 3040)


SIZES = [
    (320, 240),
    (640, 480),
    (768, 600),
    (800, 600),
    (1008, 760),  # 1/4 x 1/4 main
    (1024, 768),
    (1280, 960),
    (1512, 1140),  # 3/8 x 3/8 main
    (1600, 1200),
    (2016, 1520),  # 1/2 x 1/2 main
    (3024, 2280),  # 3/4 x 3/4 main
    (4032, 3040),  # full main
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


        picam2.configure(config)
        stride = config["lores"].get("stride")
        picam2.stop()


        padding = stride - actual[0] if stride is not None else None


        print(
            f"  configured stride={stride} "
            f"y_row_padding={padding}"
        )
    except Exception as e:
        print(f"requested={requested} FAILED: {e}")
        