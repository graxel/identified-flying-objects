from picamera2 import Picamera2
import time
p = Picamera2()
p.configure(p.create_preview_configuration())
p.start()
time.sleep(2)
p.stop()
print('captured ok')