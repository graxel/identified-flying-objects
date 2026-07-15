# Camera Nodes

**Hardware:** Rasberry Pi AI Camera on a Raspberry Pi Zero 2W host

The main goal of each camera node is to capture images, identify "interesting" patches in those images, extract the patches and their locations, and send them to a central server for processing as fast as possible.

This code runs with three threads: the camera capture, processing, and network send. This allows much faster concurrent execution of the script and allows for full utilization of the camera's capture rate and the Zero's four-core CPU.

### Camera Thread
In the camera thread, the camera captures images and runs the ML model against them about 10 times per second. From this, three streams are derived: the full resolution color image, a medium resolution grayscale image, and an ML output tensor. The ML model is run on the camera hardware, so the full resolution image and ML output tensor are gotten directly from the camera.

To obtain the medium resolution grayscale image, the script utilizes an image processing chip onboard the Zero to convert the full-res image into a reduced-resolution grayscale image without putting any burden on the CPU.

To avoid unecessarily copying the two image streams into Python memory space, only their memory "address maps" are packaged into a output object along with the ML tensor. Finally, this output object is added to the frame queue.

Note that up to this point, the CPU has only operated the camera—it has done no data processing work, keeping the thread as light as possible.

### Processing Thread
With the camera output in hand, the next step is to extract interesting patches of pixels from the full-res image. In the processing thread, a frame object is pulled from the frame queue, and then three main tasks are executed: ML-driven patch extraction from the main image, frame differencing-driven patch extraction from the main image, and deriving ML model training data from the medium-res grayscale image.

The ML tensor is essentially a low resolution heatmap of what the ML model deems to be anomalous areas of the full-res image. This heatmap is processed with a ~~clustering~~ algorithm to reject noise and obtain bounding boxes for interesting areas. These bounding boxes are then used to extract only the interesting patches from the main image.

To be honest, I have some doubts about the performance my anomaly detection model will have in the camera. As a backup plan, frame-differencing is used to identify patches of the image with movement. The medium resolution grayscale image is added to a rolling frame differencing ~~buffer~~, and the same ~~clustering~~ algorithm is used to obtain bounding boxes for areas with movement. These bounding boxes are also used to extract interesting patches from the main image.

The ML chip on the camera does not take in the full resolution image as input, but rather a scaled down grayscale image. To be able to keep a record of what the ML model took as input, and possibly use it as training data for future models, the medium resolution image is scaled down to the ML input tensor resolution, 640x480.

With all the interesting patches extracted and the model input data recreated, this all gets packed into an object and added to the send queue.

### Sender Thread

currently working on this, I think I'll use UDP.