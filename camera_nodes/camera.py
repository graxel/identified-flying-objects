# camera.py

from picamera2 import Picamera2
from libcamera import Transform


def set_up_camera(main_size, low_res_size):
    picam2 = Picamera2()

    # Configure dual streams via Broadcom hardware ISP
    # Main: Uncompressed 12MP RGB
    # Low Res: Downscaled Grayscale (YUV420)
    config = picam2.create_preview_configuration(
        main={"size": main_size, "format": "RGB888"},
        lores={"size": low_res_size, "format": "YUV420"},
        transform=Transform(hflip=1, vflip=1),
        raw=None,
        buffer_count=2,
        display="main",
        encode="main",
    )

    picam2.align_configuration(config)

    print("Final camera config:")
    for k, v in config.items():
        print(f"  {k}: {v}")
    for stream_name, stream_cfg in config.items():
        if hasattr(stream_cfg, "buffer_count"):
            print(f"Stream: {stream_name} | Buffer Count: {stream_cfg.buffer_count}")
        elif hasattr(stream_cfg, "size"):
            print(f"Stream: {stream_name} | Size: {stream_cfg.size}")
        else:
            print(f"Setting: {stream_name} = {stream_cfg}")

    picam2.configure(config)
    picam2.start()
    picam2.set_controls(
        {
            "AeEnable": False,
            "AwbEnable": False,
            # "ExposureTime": 10000,
            # "AnalogueGain": 1.0,
            # "ColourGains": (1.5, 1.5),
        }
    )
    return picam2
