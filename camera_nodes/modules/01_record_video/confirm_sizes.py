from picamera2 import Picamera2


MAIN_SIZE = (3840, 2880)


SIZES = [
    (1920, 1440),  # 2:1
    (1280, 960),   # 3:1
    (960, 720),    # 4:1
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
        main_actual = config["main"]["size"]


        print(
            f"requested={requested} "
            f"actual={actual} "
            f"main={main_actual}"
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